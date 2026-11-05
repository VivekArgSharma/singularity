"""Fetch robot engine (Panda3D).

Run:
  .venv\\Scripts\\python engine\\sim.py              window at 30 fps
  .venv\\Scripts\\python engine\\sim.py --headless   offscreen (tests)

Agent API on 127.0.0.1:8080, operator API on 127.0.0.1:8081.
"""
import argparse
import collections
import json
import math
import os
import queue
import sys
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image  # noqa: E402

FPS = 60              # window frame rate
CLIP_FPS = 30         # clip frame rate (one clip frame every 2 sim steps)
SUBSTEPS = 1           # 1 x 1/60 s per rendered frame -> real time at 60 fps
ACTIONS = {"move", "turn", "arc", "pickup", "drop"}
EXCLUSIVE = {"reset", "command", "begin_task", "end_task", "mission"}   # wait until no action is running
TIMEOUT = 120.0          # s of un-paused waiting before an HTTP call gives up


class Engine:
    def __init__(self, headless=False, level="level1", seed=0, debug_frames=False, sandbox=False):
        from panda3d.core import loadPrcFileData
        for line in ("textures-power-2 none", "sync-video false", "audio-library-name null",
                     "window-title Fetch Robot", "win-size 1400 800", "framebuffer-multisample 1",
                     "multisamples 4"):
            loadPrcFileData("", line)
        if headless:
            loadPrcFileData("", "window-type offscreen")
        else:
            loadPrcFileData("", "clock-mode limited")
            loadPrcFileData("", f"clock-frame-rate {FPS}")
        from direct.showbase.ShowBase import ShowBase
        from robot import Robot
        from views import Views

        self.base = ShowBase()
        self.views = Views(self.base, headless)
        self.robot = Robot(level, seed)
        self.views.build(self.robot.world)
        self.views.sync(self.robot)

        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.busy = False
        self.active = None
        self.last_action = "-"
        self.sim_steps = 0
        self.debug_frames = debug_frames
        self.sandbox = sandbox
        # task ownership: every action belongs to the task that was active when it was sent.
        # A newer task (operator dashboard) cancels it at the next 1/60 s step.
        self.task_id = None
        self.paused = False
        self.waiting = collections.deque()
        self.action_log = collections.deque(maxlen=60)
        self.task_actions = collections.Counter()
        self.headless = headless
        self.trail = []
        self.controller = None
        self.captures = []                        # (job, view) waiting for this frame's render pass
        self.tick_n = 0
        self._acc = 0.0                           # wall time owed to the simulation (window mode)
        from recorder import Recorder
        self.recorder = Recorder(self, debug_frames)
        if not headless:                          # manual driving from the operator window
            for key, op, args in (("arrow_up", "move", {"distance": 0.25}), ("arrow_down", "move", {"distance": -0.25}),
                                  ("arrow_left", "turn", {"degrees": -15.0}), ("arrow_right", "turn", {"degrees": 15.0}),
                                  ("p", "pickup", {}), ("o", "drop", {})):
                self.base.accept(key, self._manual, [op, args])
                self.base.accept(key + "-repeat", self._manual, [op, args])
        from perf import Perf
        self.perf = Perf()
        self.base.taskMgr.add(self._tick, "engine-tick")
        # time Panda's own render pass (igLoop, sort 50) from two tiny tasks around it
        self.base.taskMgr.add(self._render_t0, "perf-render-start", sort=49)
        self.base.taskMgr.add(self._render_t1, "perf-render-end", sort=51)
        self._rt0 = None

    def _render_t0(self, task):
        self._rt0 = time.perf_counter()
        return task.cont

    def _render_t1(self, task):
        if self._rt0 is not None:
            self.perf.total["render"] += time.perf_counter() - self._rt0
            self.perf.count["render"] += 1
        return task.cont

    # ------------------------------------------------------------ threading
    def call(self, op, task=None, **args):
        """Called from HTTP threads. Returns (status, result).

        `task` is the optional X-Task-Id of the caller. A request from a task that is no
        longer active is refused (409 stale task), so a delayed reply from an old agent can
        never act on the world after a newer task has taken over."""
        with self.lock:
            if task is not None and task != self.task_id:
                return 409, "stale task"
            if op in ACTIONS:
                if self.busy:
                    return 409, "busy"
                self.busy = True
            owner = task if task is not None else self.task_id
        job = {"op": op, "args": args, "task": owner, "done": threading.Event(),
               "status": 200, "result": None}
        self.q.put(job)
        waited = 0.0
        while not job["done"].wait(0.25):
            if not self.paused:
                waited += 0.25
            if waited > TIMEOUT:
                return 504, "engine timeout"
        res = job["result"]
        if job["status"] == 200 and op in ("look", "view") and hasattr(res, "shape"):
            import io
            from PIL import Image
            buf = io.BytesIO()
            Image.fromarray(res).save(buf, format="JPEG", quality=90)
            res = buf.getvalue()
        return job["status"], res

    def _manual(self, op, args):
        """Keyboard driving (window only). Ignored while a dashboard task or any action runs."""
        ctl = self.controller
        if ctl is not None and ctl.current is not None and ctl.current["status"] in ("Starting", "Running", "Paused"):
            return
        with self.lock:
            if self.busy or self.active is not None or self.waiting:
                return
            self.busy = True
        r = self.robot
        if r.state == "Idle":
            r.set_command("manual driving", "manual")
            r.interactive = True
        elif r.state == "Done":
            r.continue_task("manual driving")
            r.interactive = True
        self.q.put({"op": op, "args": dict(args), "task": self.task_id, "done": threading.Event(),
                    "status": 200, "result": None})

    # ------------------------------------------------- task control (any thread)
    def preempt(self, task_id):
        """Make task_id the active task. A running action of an older task stops at
        the next simulation step; queued actions of older tasks are refused."""
        with self.lock:
            self.task_id = task_id
            self.paused = False

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def snapshot(self):
        """Read-only status for the dashboard (operator port only)."""
        r = self.robot
        active = self.active[0] if self.active else None
        return {
            "robot": {"state": r.state, "x": round(r.x, 3), "y": round(r.y, 3),
                      "heading": round(r.heading, 1), "holding": r.held is not None,
                      "retries": r.retries, "collisions": r.stats["collisions"],
                      "path_length": round(r.stats["path_length"], 2),
                      "sim_seconds": round(r.stats["sim_seconds"], 2), "outcome": r.outcome},
            "engine": {"task_id": self.task_id, "paused": self.paused,
                       "action": (f"{active['op']} {_fmt_args(active['args'])}".strip() if active else None),
                       "last_action": self.last_action, "sim_steps": self.sim_steps,
                       "fps": round(self.base.clock.getAverageFrameRate(), 1),
                       "recording": ({"run_id": self.rec["run_id"], "frames": self.rec["n"]}
                                     if self.rec else None)},
            "actions": list(self.action_log)[-25:],
            "mission": r.mission.status() if r.mission is not None else None,
            "trail": [(round(x, 2), round(y, 2)) for x, y in self.trail[::max(1, len(self.trail) // 400)]],
        }

    def _finish_job(self, job, result, status=200):
        job["status"], job["result"] = status, result
        if job["op"] in ACTIONS:
            if status == 200 and "before" in job:
                self._log_action(job, result)
            with self.lock:
                self.busy = False
        job["done"].set()

    def _sync(self):
        """Copy the simulation onto the scene graph. A clip frame asked for at an earlier step is
        rendered first, so every clip frame shows exactly the pose logged for it."""
        p = self.recorder.pending
        if p is not None and p["step"] != self.sim_steps:
            self.recorder.flush()
        self.views.sync(self.robot)

    def _collect_captures(self):
        self.recorder.collect()
        done, keep = [], []
        for c in self.captures:
            (done if c[2] < self.tick_n else keep).append(c)
        self.captures = keep
        for job, name, _ in done:
            arr = self.views.read(name)
            self.views.release(name)
            self._finish_job(job, arr)            # JPEG encoding happens on the HTTP thread

    def _pose(self):
        r = self.robot
        return {"x": round(r.x, 3), "y": round(r.y, 3), "heading": round(r.heading, 1),
                "holding": r.held is not None}

    def _log_action(self, job, result):
        r = self.robot
        entry = {"ts": round(time.time(), 3), "t0": job.get("t0"), "task_id": job["task"], "command": r.command,
                 "tool": job["op"], "args": job["args"], "result": result,
                 "pose_before": job["before"], "pose_after": self._pose(),
                 "collided": bool(result.get("collided")) if isinstance(result, dict) else False,
                 "interrupted": bool(result.get("interrupted")) if isinstance(result, dict) else False,
                 "frames": [job["frame0"], self.rec["n"] if self.rec else None],
                 "sim_seconds": round(r.stats["sim_seconds"], 2),
                 "state": r.state, "outcome": r.outcome}
        self.action_log.append(entry)
        self.task_actions[job["task"]] += 1
        if self.rec is not None:
            with open(self.rec["dir"] / "actions.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")

    # ------------------------------------------------------------ main loop
    def _run_op(self, job):
        try:
            return getattr(self, "op_" + job["op"])(**job["args"]), None
        except Exception as e:  # noqa: BLE001
            return None, f"{type(e).__name__}: {e}"

    def _tick(self, task):
        self.perf.tick()
        self.tick_n += 1
        with self.perf.section("capture"):
            self._collect_captures()
        # observations and operator reads run at once, even while an action is moving
        while True:
            try:
                job = self.q.get_nowait()
            except queue.Empty:
                break
            if job["op"] in ACTIONS or job["op"] in EXCLUSIVE:
                self.waiting.append(job)
                continue
            if job["op"] == "perf":
                self._finish_job(job, self._op_perf(**job["args"]))
                continue
            name = "look" if job["op"] == "look" else job["args"].get("name") if job["op"] == "view" else None
            if name in ("look", "chase", "top"):
                with self.perf.section("op:" + job["op"] + ("" if job["op"] == "look" else ":" + name)):
                    self._sync()
                    self.views.activate(name)
                    self.captures.append((job, name, self.tick_n))
                continue
            with self.perf.section("op:" + (job["op"] if job["op"] != "view" else "view:" + job["args"].get("name", ""))):
                out, err = self._run_op(job)
            self._finish_job(job, out) if err is None else self._finish_job(job, err, 500)
        # an action whose task is no longer the active one is cancelled at this step
        if self.active is not None:
            self.robot.cancel = self.active[0]["task"] != self.task_id
        if self.active is None and self.waiting:
            job = self.waiting[0]
            if not (self.paused and job["op"] in ACTIONS):
                self.waiting.popleft()
                if job["op"] in ACTIONS and job["task"] != self.task_id:
                    self._finish_job(job, "stale task", 409)      # queued before the new task took over
                else:
                    if job["op"] in ACTIONS:
                        self.robot.cancel = False
                        self._acc = 0.0
                        job["before"], job["frame0"] = self._pose(), (self.rec["n"] if self.rec else None)
                        job["t0"] = round(time.time(), 3)
                    out, err = self._run_op(job)
                    if err is not None:
                        self._finish_job(job, err, 500)
                    elif isinstance(out, types.GeneratorType):
                        self.active = (job, out)
                        self.last_action = f"{job['op']} {_fmt_args(job['args'])}".strip()
                    else:
                        self._finish_job(job, out)
        if self.active is not None and not (self.paused and not self.robot.cancel):
            job, gen = self.active
            finished = None
            k = 0
            for _ in range(self._substeps()):
                try:
                    with self.perf.section("sim"):
                        next(gen)
                except StopIteration as stop:
                    finished = stop.value
                    break
                self.sim_steps += 1
                k += 1
                if self.sim_steps % (FPS // CLIP_FPS) == 0:      # clip stays at true speed
                    with self.perf.section("record"):
                        self._sync()
                        self._record_frame()
            with self.perf.section("sync"):
                self._sync()
            if finished is not None and self.recorder.last_step != self.sim_steps:
                with self.perf.section("record"):
                    self._record_frame()                  # the end pose, once
            self.perf.stepped(k, (self.robot.x, self.robot.y) if job["op"] in ("move", "arc") else None)
            self._add_trail()
            if finished is not None:
                self.active = None
                self.robot.cancel = False
                self.robot.after_action()                 # mission evaluator: exit reached, limits ...
                self._finish_job(job, finished)
                if self.robot.state == "Done" and not (self.sandbox or self.robot.interactive):
                    self._stop_recording()
        else:
            self.perf.stepped(0)
        with self.perf.section("hud"):
            self.views.set_hud(self._hud_text())
        return task.cont

    def _op_perf(self, reset=False):
        out = self.perf.summary()
        out["recording"] = self.rec is not None
        if reset:
            self.perf.reset()
        return out

    def _substeps(self):
        """Window mode: simulation time follows wall time. Every step is still exactly 1/60 s
        (deterministic); only how many steps run before a frame is drawn varies: 1 at 60 fps,
        3 at 20 fps. Without this the robot moved in slow motion whenever frames were slow.
        Headless: always 1 step per frame (deterministic, faster than real time)."""
        if self.headless:
            return 1
        self._acc += min(self.base.clock.getDt(), 0.1)
        n = int(self._acc * FPS + 0.5)
        n = max(0, min(6, n))
        self._acc -= n / FPS
        if abs(self._acc) > 2.0 / FPS:            # never build up a debt (window dragged, breakpoint)
            self._acc = 0.0
        return n

    def _add_trail(self):
        r = self.robot
        if not self.trail or math.hypot(r.x - self.trail[-1][0], r.y - self.trail[-1][1]) > 0.04:
            self.trail.append((r.x, r.y))
            if len(self.trail) > 3000:
                self.trail = self.trail[-3000:]
            self.views.set_trail(self.trail)

    def _hud_text(self):
        r = self.robot
        fps = self.base.clock.getAverageFrameRate()
        flag = "   PAUSED" if self.paused else ""
        d = r.depth()
        compass = "E SE S SW W NW N NE".split()[int(((r.heading % 360) + 22.5) // 45) % 8]
        return (f"state: {r.state}{flag}   pose: ({r.x:.2f}, {r.y:.2f})  heading {r.heading:.0f} ({compass})   "
                f"depth {'-' if d is None else f'{d:.2f} m'}   {fps:.0f} fps\n"
                f"holding: {r.held is not None}   retries: {r.retries}   last: {self.last_action}\n"
                f"command: {r.command or '-'}   outcome: {r.outcome or '-'}   "
                f"keys: arrows drive, P pick up, O drop, 1/2 view")

    # ------------------------------------------------------------ recording (recorder.py)
    @property
    def rec(self):
        return self.recorder.rec

    def _start_recording(self, run_id, command, root=None):
        self.recorder.start(run_id, command, root)

    def _record_frame(self):
        self.recorder.frame()

    def _stop_recording(self):
        return self.recorder.stop()

    # ---------------------------------------------------------- agent ops
    def op_state(self):
        return self.robot.public_state()

    def op_look(self):
        self._sync()
        return self.views.look_jpeg()

    def op_depth(self):
        return {"depth": self.robot.depth()}

    def op_scan(self, n=61):
        """Range scan: n horizontal rays from the camera position spread evenly across the
        camera's 90 deg field of view (left to right). Distances only, like /depth."""
        r = self.robot
        from world import forward
        fx, fy = forward(r.heading)
        ox, oy = r.x + fx * r.r, r.y + fy * r.r
        n = max(3, min(181, int(n)))
        ranges = []
        z = r.z
        for i in range(n):
            b = -45.0 + 90.0 * i / (n - 1)
            d = r.world.ray(ox, oy, r.heading + b, ignore=r.held, z=z)
            ranges.append(None if d is None else round(d, 3))
        return {"ranges": ranges, "fov": 90.0}

    def op_move(self, distance, speed=1.0):
        return self.robot.move(distance, speed)

    def op_turn(self, degrees, speed=1.0):
        return self.robot.turn(degrees, speed)

    def op_arc(self, radius, degrees, speed=1.0):
        return self.robot.arc(radius, degrees, speed)

    def op_pickup(self):
        return self.robot.pickup()

    def op_drop(self):
        return self.robot.drop()

    # ------------------------------------------------------- operator ops
    def op_reset(self, level="level1", seed=0):
        self._stop_recording()
        self.robot.reset(level, seed)
        self.trail = []
        self.views.build(self.robot.world)
        self.views.sync(self.robot)
        self.last_action = "-"
        return {"ok": True, "state": self.robot.state}

    def op_mission(self, name, seed=0, config=None):
        """Operator: build a mission scene (Brain Puzzle, Colour Sorting, Maze) from its seed."""
        import missions
        self._stop_recording()
        m = missions.create(name, seed, config)
        cfg = m.scene()
        self.robot.reset(name, seed, cfg=cfg, mission=m)
        m.state = "ready"
        self.trail = []
        self.views.build(self.robot.world)
        self.views.sync(self.robot)
        self.last_action = "-"
        return {"ok": True, "mission": name, "seed": seed, "instruction": m.instruction, "title": m.title}

    def op_command(self, text, run_id, keep_scene=False):
        if keep_scene and self.sandbox and self.robot.state != "Idle":
            # sandbox demos only: next command on the same scene, recording continues
            self.robot.continue_task(text)
            if self.rec is None:
                self._start_recording(run_id, text)
            return {"ok": True, "state": self.robot.state}
        if self.robot.state != "Idle":
            return {"ok": False, "error": "not idle; reset first"}
        self.robot.set_command(text, run_id)
        self._start_recording(run_id, text)
        return {"ok": True, "state": self.robot.state}

    def op_begin_task(self, task_id, text, root):
        """Operator dashboard: make `text` the robot's current task on the same scene.
        Pose, heading and anything held are kept exactly as they are."""
        prev = self._stop_recording()
        r = self.robot
        if r.state == "Idle":
            r.set_command(text, task_id)
        else:
            r.continue_task(text)
            r.run_id = task_id
        r.interactive = True
        r.cancel = False
        self.last_action = "-"
        self._start_recording(task_id, text, root)
        return {"ok": True, "state": r.state, "holding": r.held is not None, "prev": prev,
                "path_length": round(r.stats["path_length"], 3)}

    def op_end_task(self, run_id=None):
        if run_id is not None and (self.rec is None or self.rec["run_id"] != run_id):
            return {"ok": True, "rec": None}            # that task's recording already ended
        return {"ok": True, "rec": self._stop_recording()}

    def op_record_stop(self):
        self._stop_recording()
        return {"ok": True}

    def op_giveup(self):
        self.robot.give_up()
        self._stop_recording()
        return self.robot.result()

    def op_verdict(self, passed):
        return {"outcome": self.robot.verdict(passed)}

    def op_result(self):
        res = self.robot.result()
        res["fps"] = round(self.base.clock.getAverageFrameRate(), 1)
        res["frames"] = self.rec["n"] if self.rec else None
        return res

    def op_view(self, name):
        """People-only render (chase, top) or the robot view. Operator port only."""
        self._sync()
        if name == "window":
            return self.views.window_jpeg()
        return self.views.view_jpeg(name)


def _fmt_args(args):
    return ", ".join(f"{k} {v:+.2f}" if isinstance(v, (int, float)) else f"{k} {v}"
                     for k, v in args.items())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--level", default="level1")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--admin-port", type=int, default=int(os.environ.get("PORT", "8081")))   # hosts set $PORT
    ap.add_argument("--debug-frames", action="store_true",
                    help="also save chase and top-down frames for every clip frame")
    ap.add_argument("--sandbox", action="store_true",
                    help="demo mode: allow a next command on the same scene after Done")
    ap.add_argument("--brain", default=None, choices=("auto", "claude", "local"),
                    help="dashboard agent: claude (needs ANTHROPIC_API_KEY), local (camera-only "
                         "colour planner, free), auto = claude if a key is set, else local")
    a = ap.parse_args()
    from envfile import load_env
    loaded = load_env()                           # .env: ANTHROPIC_API_KEY, FETCH_BRAIN, AGENT_MODEL ...
    if loaded:
        print("settings from .env: " + ", ".join(k for k in loaded), flush=True)   # names only, never values
    if a.brain is None:                           # the toggle lives in .env: USE_CLAUDE_API=true|false
        use = os.environ.get("USE_CLAUDE_API", "").strip().lower()
        if use in ("1", "true", "yes", "on"):
            a.brain = "claude"
        elif use in ("0", "false", "no", "off"):
            a.brain = "local"
        else:
            a.brain = os.environ.get("FETCH_BRAIN", "auto").strip().lower() or "auto"
            if a.brain not in ("auto", "claude", "local"):
                a.brain = "auto"
        if a.brain == "claude" and not os.environ.get("ANTHROPIC_API_KEY"):
            print("USE_CLAUDE_API=true but ANTHROPIC_API_KEY is empty in .env: running the free local planner", flush=True)
    from api import make_admin_app, make_agent_app, serve
    from controller import Controller
    eng = Engine(headless=a.headless, level=a.level, seed=a.seed, debug_frames=a.debug_frames, sandbox=a.sandbox)
    ctl = Controller(eng, agent_port=a.port, brain=a.brain)
    eng.controller = ctl
    serve(make_agent_app(eng), a.port)                        # agent API: always this machine only
    serve(make_admin_app(eng, ctl), a.admin_port, host=os.environ.get("CONSOLE_BIND", "127.0.0.1"))
    print(f"engine ready: agent http://127.0.0.1:{a.port}  dashboard http://127.0.0.1:{a.admin_port}/"
          f"  (brain: {ctl.brain})", flush=True)
    eng.base.run()


if __name__ == "__main__":
    main()
