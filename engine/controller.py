"""Operator-side task control behind the dashboard (port 8081 only).

One task is active at a time. Every instruction typed in the dashboard is sorted
into one of these kinds:

  task      a new actionable instruction    -> pre-empts the current task at once
  refine    "slower", "make it three times" -> replaces the task with the same goal + the update
  status    "what are you doing?"           -> answered from live state; the task keeps running
  stop      "stop"                          -> current action stops, robot waits
  pause     "pause" / "wait"                -> execution frozen, task kept
  resume    "resume" / "continue"           -> paused task carries on
  reset     "reset the environment"         -> agent stopped, world reset

Pre-emption is layered so an old task can never act again:
  1. engine.preempt(new_id): the running action of the old task stops at the next
     1/60 s simulation step (move/turn/arc stop in place; a pickup that has not
     grasped yet lifts clear and folds back; a drop that has not released keeps
     the object), and queued requests of the old task are refused.
  2. every agent request carries X-Task-Id; the engine refuses any id that is not
     the active one (409 stale task), so a delayed AI reply cannot execute.
  3. the old agent process is terminated; its remaining output is ignored.
The robot is never teleported or reset by a new task: pose, heading and a held
object stay exactly as they are, and the new agent is told what it holds.
"""
import collections
import itertools
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = Path(os.environ.get("FETCH_RUNS_DIR", str(ROOT / "outputs" / "runs")))
MAP_FILE = RUNS / "map_state.npz"      # the robot's own map (agent memory), kept between tasks
CLIP_MAX_FRAMES = 20 * 30
LIVE = ("Starting", "Running", "Paused")

OBJECT_WORDS = r"cube|box|block|sphere|ball|cylinder|can|cone|zone|square|object|thing|wall|corner"
ACTION_START = (r"bring|fetch|take|carry|pick|grab|get|go|move|drive|head|circle|orbit|loop|"
                r"rotate|turn|spin|approach|stop in front|back|reverse|put|drop|place|deliver|find|"
                r"look|face|come|return|follow|navigate")
REFINE_WORDS = (r"slow(er|ly)?|fast(er)?|quick(er|ly)?|faster|bigger|wider|larger|smaller|tighter|"
                r"closer|farther|further|again|once more|twice|three times|thrice|\d+ times|"
                r"a bit|a little|more|less|carefully|gently")


def classify(text, has_live_task):
    t = re.sub(r"\s+", " ", text.lower()).strip(" .!")
    t = re.sub(r"^(ok(ay)?|hey|robot|please)[, ]+", "", t)
    if re.fullmatch(r"(please )?(stop|halt|freeze|abort|cancel( it| that| the task| everything)?)"
                    r"( now| please| moving| everything)?", t):
        return "stop"
    if re.fullmatch(r"(pause|hold on|hold it|wait)( a (moment|second|sec|bit))?( please)?", t):
        return "pause"
    if re.fullmatch(r"(resume|continue|carry on|go on|keep going|unpause)( please| the task)?", t):
        return "resume"
    if re.fullmatch(r"reset( the)?( environment| simulation| world| scene| everything)?( please)?", t):
        return "reset"
    body = re.sub(r"^(can|could|would|will) you (please )?", "", t)
    asks = text.strip().endswith("?") or re.match(
        r"(what|where|how|why|which|are you|is it|is the|did you|have you|do you|status|report)\b", t)
    if asks and not re.match(rf"({ACTION_START})\b", body):
        return "status"
    if has_live_task and re.search(rf"\b({REFINE_WORDS})\b", t) and not re.search(
            rf"\b({OBJECT_WORDS})s?\b", t) and not re.search(r"\binstead\b", t):
        return "refine"
    return "task"


class Controller:
    def __init__(self, engine, agent_port=8080, brain="auto"):
        self.eng = engine
        self.agent_url = f"http://127.0.0.1:{agent_port}"
        key = bool(os.environ.get("ANTHROPIC_API_KEY"))
        self.key_set = key
        self.brain = ("claude" if key else "local") if brain == "auto" else brain
        if self.brain == "claude" and not key:
            self.brain = "local"                  # asked for Claude but no key: never start a run that cannot work
        self.model = os.environ.get("AGENT_MODEL", "claude-haiku-5-5")
        self.runs_dir = RUNS
        self.lock = threading.RLock()
        self.start_lock = threading.Lock()
        self.tasks = []
        self.current = None
        self.proc = None
        self.interrupted = None
        self.last_instruction = None
        self.events = collections.deque(maxlen=120)
        self.map_snap = None
        self._seq = itertools.count(1)

    def set_brain(self, brain):
        """Operator toggle (console Settings): 'local' (free planner) or 'claude' (vision agent, needs the key).
        Takes effect from the next task; a running task keeps its agent."""
        self.key_set = bool(os.environ.get("ANTHROPIC_API_KEY"))
        if brain not in ("local", "claude"):
            return {"ok": False, "error": "brain must be local or claude"}
        if brain == "claude" and not self.key_set:
            return {"ok": False, "error": "no ANTHROPIC_API_KEY: put it in .env and restart the engine"}
        self.brain = brain
        self._event("system", f"Agent switched to {'Claude (' + self.model + ')' if brain == 'claude' else 'the free local planner'}.")
        return {"ok": True, "brain": self.brain, "model": self.model}

    # --------------------------------------------------------------- input
    def submit(self, text):
        live = self.current is not None and self.current["status"] in LIVE
        kind = classify(text, live)
        with self.lock:
            self.last_instruction = {"text": text, "kind": kind, "ts": time.time()}
        self._event("operator", f"[{kind}] {text}")
        if kind == "status":
            reply = self.answer(text)
            self._event("reply", reply)
            return {"kind": kind, "reply": reply, "task_id": self.eng.task_id}
        if kind == "stop":
            return self.stop()
        if kind == "pause":
            return self.pause()
        if kind == "resume":
            return self.resume()
        if kind == "reset":
            self.stop_agent("reset")
            self.clear_map()
            self.eng.call("reset", level="level1", seed=0)
            return {"kind": kind, "reply": "Environment reset to level1."}
        if kind == "refine" and live:
            base = self.current.get("goal") or self.current["command"]
            return self.start_task(f"{base} (update: {text})", kind="refine", goal=base, said=text)
        return self.start_task(text)

    # ---------------------------------------------------------- lifecycle
    def _new_id(self):
        return f"{datetime.now():%Y%m%d-%H%M%S}-{next(self._seq):03d}"

    def start_task(self, command, kind="task", goal=None, said=None):
        with self.start_lock:
            tid = self._new_id()
            with self.lock:
                old = self.current
                self.eng.preempt(tid)                 # 1. old action cancels at the next step
                self._kill_agent()                    # 3. old agent process gone
                if old is not None and old["status"] in LIVE:
                    old["status"] = "Refined" if kind == "refine" else "Interrupted"
                    old["ended"] = time.time()
                    old["superseded_by"] = tid
                    self.interrupted = {"id": old["id"], "command": old["command"],
                                        "status": old["status"], "by": tid,
                                        "action": self.eng.last_action}
                task = {"id": tid, "command": command, "said": said or command, "kind": kind,
                        "goal": goal or command, "status": "Starting", "brain": self.brain,
                        "started": time.time(), "ended": None, "result": None,
                        "thought": None, "agent_action": None, "superseded_by": None,
                        "replaced": old["id"] if old is not None and old.get("superseded_by") == tid else None,
                        "video": None, "clip20": None, "clip_seconds": None, "frames": 0,
                        "events": collections.deque(maxlen=60)}
                self.current = task
                self.tasks.append(task)
            # waits only until the cancelled action has ended safely (<= ~0.7 s for a pickup)
            st, res = self.eng.call("begin_task", task_id=tid, text=command, root=str(RUNS))
            if st != 200:
                task["status"] = "Failed"
                task["result"] = f"engine refused the task: {res}"
                return {"kind": kind, "task_id": tid, "reply": task["result"]}
            task["path0"] = res["path_length"]
            task["holding_at_start"] = res["holding"]
            if res.get("prev") and old is not None:
                self._encode_async(old, res["prev"])
            run_dir = RUNS / tid
            (run_dir / "command.txt").write_text(command + "\n", encoding="utf-8")
            context = self._context(old, res["holding"])
            task["context"] = context
            with self.lock:
                if self.current is not task:          # a newer instruction arrived meanwhile
                    return {"kind": kind, "task_id": tid, "reply": "superseded"}
                self._spawn(task, context)
                task["status"] = "Paused" if self.eng.paused else "Running"
        reply = (f"Now doing: {command}" if kind == "task" else f"Updated the task: {command}")
        if old is not None and old.get("superseded_by") == tid:
            reply += f"  (interrupted: {old['command']})"
        self._event("system", reply)
        return {"kind": kind, "task_id": tid, "reply": reply}

    def _context(self, old, holding):
        lines = []
        if old is not None and old.get("superseded_by"):
            lines.append(f"The operator replaced the previous instruction (\"{old['command']}\") "
                         f"with this one. Do not continue the previous instruction.")
        if holding:
            src = f" during the previous instruction (\"{old['command']}\")" if old is not None else ""
            lines.append(f"You are currently holding an object that you picked up{src}. "
                         "It is still in your hand. Decide what to do with it for the new instruction.")
        return " ".join(lines)

    def stop(self):
        with self.start_lock:
            cur = self.current
            self.eng.preempt(f"stop-{self._new_id()}")
            with self.lock:
                self._kill_agent()
                if cur is not None and cur["status"] in LIVE:
                    cur["status"] = "Stopped"
                    cur["ended"] = time.time()
            self._end_recording(cur)
        self._event("system", "Stopped. Robot is waiting for an instruction.")
        return {"kind": "stop", "reply": "Stopped. Waiting for the next instruction."}

    def stop_agent(self, reason):
        cur = self.current
        self.stop()
        if cur is not None and cur["status"] == "Stopped":
            cur["result"] = f"stopped ({reason})"

    def pause(self):
        self.eng.pause()
        if self.current is not None and self.current["status"] == "Running":
            self.current["status"] = "Paused"
        self._event("system", "Paused.")
        return {"kind": "pause", "reply": "Paused. Say resume to continue."}

    def resume(self):
        self.eng.resume()
        if self.current is not None and self.current["status"] == "Paused":
            self.current["status"] = "Running"
        self._event("system", "Resumed.")
        return {"kind": "resume", "reply": "Resumed."}

    # -------------------------------------------------------------- agent
    def interpret(self, task):
        """Operator's words -> the standard command the robot runs (agent/interpret.py, pure text).
        The free planner runs the standard command; the Claude agent gets the operator's own words."""
        try:
            sys.path.insert(0, str(ROOT / "agent"))
            from interpret import interpret
            r = interpret(task["command"], holding=bool(self.eng.robot.held is not None))
        except Exception as e:  # noqa: BLE001  (never block a task on the interpreter)
            r = {"canonical": task["command"], "changed": False, "note": f"interpreter error: {e}"}
        task["understood"] = r["canonical"]
        task["understanding"] = {k: r.get(k) for k in ("action", "object", "destination", "note")}
        if r.get("changed"):
            self._event("understood", f"Understood as: {r['canonical']}")
        elif r.get("note"):
            self._event("understood", r["note"])

    def _spawn(self, task, context):
        run_dir = RUNS / task["id"]
        if task.get("kind") == "task" and "understood" not in task:
            self.interpret(task)
        run_cmd = task.get("understood") if self.brain == "local" else task["command"]
        cmd = [sys.executable, "-u", str(ROOT / "agent" / "run.py"), "--url", self.agent_url,
               "--task-id", task["id"], "--command", run_cmd or task["command"], "--brain", self.brain,
               "--context", context, "--map-file", str(MAP_FILE), "--log-dir", str(run_dir)]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        err = open(run_dir / "agent_stderr.log", "w", encoding="utf-8")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True,
                                encoding="utf-8", creationflags=flags, cwd=str(ROOT))
        self.proc = (proc, task, err)
        threading.Thread(target=self._read, args=(proc, task, err), daemon=True).start()

    def _kill_agent(self):
        if self.proc is not None:
            proc, _, _ = self.proc
            if proc.poll() is None:
                from proc_util import kill_tree         # the venv launcher + the real interpreter
                threading.Thread(target=kill_tree, args=(proc,), daemon=True).start()
            self.proc = None

    def _read(self, proc, task, err):
        log = RUNS / task["id"] / "agent.jsonl"
        with open(log, "a", encoding="utf-8") as f:
            for line in proc.stdout:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("task_id") != task["id"]:
                    continue
                if ev.get("type") == "map":           # live map for the dashboard; not part of the log
                    if task["status"] in LIVE:
                        self.map_snap = ev["map"]
                        task["map"] = True
                    continue
                ev["ts"] = round(time.time(), 3)
                f.write(json.dumps(ev) + "\n")
                f.flush()
                with self.lock:
                    if task["status"] not in LIVE:      # superseded: ignore whatever it still says
                        continue
                    task["events"].append(ev)
                    kind = ev.get("type")
                    if kind == "thinking":
                        task["thought"] = ev.get("text")
                    elif kind == "action":
                        task["agent_action"] = f"{ev.get('tool')} {json.dumps(ev.get('args', {}))}"
                    elif kind == "done":
                        task["result"] = ev.get("summary")
                        task["status"] = "Completed" if ev.get("ok") else "Failed"
                        task["ended"] = time.time()
                    text = (json.dumps(ev.get("result")) if kind == "result" else
                            ev.get("text") or ev.get("summary") or task["agent_action"] or "")
                    self._event(kind, f"{ev.get('tool')} -> {text}" if kind == "result" else text)
        proc.wait()
        err.close()
        with self.lock:
            if task["status"] in LIVE:
                task["status"] = "Failed"
                task["ended"] = time.time()
                tail = (RUNS / task["id"] / "agent_stderr.log").read_text(encoding="utf-8")[-400:]
                task["result"] = task["result"] or f"agent exited (code {proc.returncode}) {tail}".strip()
            finished = self.current is task and task["status"] in ("Completed", "Failed")
        if finished:
            self._end_recording(task)

    # ------------------------------------------------------------ outputs
    def _end_recording(self, task):
        st, res = self.eng.call("end_task", run_id=task["id"] if task is not None else None)
        if task is not None and st == 200 and res.get("rec"):
            self._encode_async(task, res["rec"])
        elif task is not None:
            self._write_result(task)

    def _encode_async(self, task, rec):
        threading.Thread(target=self._encode, args=(task, rec), daemon=True).start()

    def _encode(self, task, rec):
        sys.path.insert(0, str(ROOT / "tools"))
        import make_clip
        task["frames"] = rec.get("frames", 0)
        if task["frames"]:
            task["video"] = "encoding"
            try:
                make_clip.make_clip(task["id"], RUNS)
                task["video"] = f"{task['id']}.mp4"
                n = max(task["frames"], make_clip.MIN_FRAMES)
                task["clip_seconds"] = round(n / 30, 2)
                if task["frames"] > CLIP_MAX_FRAMES:
                    make_clip.make_clip(task["id"], RUNS, speedup=task["frames"] / CLIP_MAX_FRAMES)
                    task["clip20"] = f"{task['id']}_clip20s.mp4"
            except Exception as e:  # noqa: BLE001
                task["video"] = None
                task["video_error"] = f"{type(e).__name__}: {e}"
        self._write_result(task)

    def _write_result(self, task):
        d = RUNS / task["id"]
        d.mkdir(parents=True, exist_ok=True)
        out = {k: v for k, v in task.items() if k not in ("events", "map")}
        if task.get("map") and self.map_snap is not None:
            snap = {k: v for k, v in self.map_snap.items() if k != "grid"}
            snap["gt"] = self.gt_coverage()
            (d / "map.json").write_text(json.dumps(snap, indent=2, default=str), encoding="utf-8")
        out["actions"] = self.eng.task_actions.get(task["id"], 0)
        out["clip_rule_10_20s"] = (None if not task.get("clip_seconds")
                                   else 10 <= task["clip_seconds"] <= 20)
        (d / "result.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")

    # ---------------------------------------------------------------- map
    def clear_map(self):
        self.map_snap = None
        try:
            MAP_FILE.unlink()
        except FileNotFoundError:
            pass

    def gt_coverage(self):
        """Operator-only check of the robot's map against the real room (never sent to the agent)."""
        snap = self.map_snap
        if not snap:
            return None
        import numpy as np
        w = self.eng.robot.world
        n, cell, half = snap["n"], snap["cell"], snap["half"]
        g = np.frombuffer(snap["grid"].encode(), dtype=np.uint8).reshape(n, n)
        xs = -half + (np.arange(n) + 0.5) * cell
        X, Y = np.meshgrid(xs, xs)
        floor = (np.abs(X) < w.half) & (np.abs(Y) < w.half)
        for o in w.objects:
            if not o.held:
                floor &= ~np.vectorize(lambda a, b, o=o: o.dist_to_point(a, b) <= 0.0)(X, Y)
        seen = np.isin(g, (ord("f"), ord("r"), ord("i")))     # "i" = seen floor drawn as too tight for the body
        wrong = seen & ~floor & (np.abs(X) < w.half) & (np.abs(Y) < w.half)
        return {"coverage": round(float((seen & floor).sum() / floor.sum()), 3),
                "floor_m2": round(float(floor.sum() * cell * cell), 2),
                "seen_inside_objects_m2": round(float(wrong.sum() * cell * cell), 2)}

    def map_status(self):
        return {"map": self.map_snap, "gt": self.gt_coverage()}

    def list_runs(self):
        RUNS.mkdir(parents=True, exist_ok=True)
        out = []
        for d in sorted(RUNS.iterdir(), reverse=True)[:30]:
            if d.is_dir():
                mp4 = [p.name for p in d.glob("*.mp4") if not p.name.startswith(".")]
                cmd = (d / "command.txt").read_text(encoding="utf-8").strip() if (d / "command.txt").exists() else ""
                out.append({"run_id": d.name, "command": cmd, "videos": sorted(mp4),
                            "frames": len(list((d / "frames").glob("*.jpg"))) if (d / "frames").exists() else 0})
        return {"dir": str(RUNS), "runs": out}

    # ------------------------------------------------------------- status
    def answer(self, text):
        snap = self.eng.snapshot()
        r, e = snap["robot"], snap["engine"]
        cur = self.current
        t = text.lower()
        doing = (f"Working on \"{cur['command']}\" ({cur['status'].lower()})" if cur and cur["status"] in LIVE
                 else "Not working on anything; waiting for an instruction")
        if e["action"]:
            doing += f"; right now: {e['action']}"
        travelled = r["path_length"] - (cur.get("path0", 0) if cur else 0)
        parts = []
        if re.search(r"far|distance|travel", t):
            parts.append(f"I have travelled {travelled:.2f} m on this task "
                         f"({r['path_length']:.2f} m since the last reset).")
        if re.search(r"where|position|pose|heading", t):
            parts.append(f"I am at ({r['x']:.2f}, {r['y']:.2f}) facing {r['heading']:.0f} degrees.")
        if re.search(r"hold|carry|carrying|in your hand|gripper", t):
            parts.append("I am holding an object." if r["holding"] else "My hand is empty.")
        if re.search(r"doing|status|task|busy|up to|working", t) or not parts:
            parts.insert(0, doing + ".")
        return " ".join(parts)

    def _event(self, kind, text):
        self.events.append({"ts": round(time.time(), 2), "type": kind, "text": str(text)[:300]})

    def status(self):
        snap = self.eng.snapshot()
        cur = self.current
        mode = ("paused" if self.eng.paused else
                "executing" if cur is not None and cur["status"] == "Running" else "waiting")

        def brief(t):
            if t is None:
                return None
            b = {k: v for k, v in t.items() if k != "events"}
            b["actions"] = self.eng.task_actions.get(t["id"], 0)
            return b
        task = brief(cur)
        if task is not None:
            task["events"] = list(cur["events"])[-12:]
            task["travelled"] = round(snap["robot"]["path_length"] - cur.get("path0", 0), 2)
        return {"mode": mode, "brain": self.brain, "key_set": self.key_set, "model": self.model,
                "task": task, "last_instruction": self.last_instruction,
                "interrupted": self.interrupted,
                "history": [brief(t) for t in self.tasks[-10:]][::-1],
                "feed": list(self.events)[-40:], "runs_dir": str(RUNS), **snap}
