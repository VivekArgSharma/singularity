"""Live pre-emption tests: real engine + controller + agent processes, over HTTP,
the same way the dashboard drives them. Uses the local (camera-only) brain so it
runs without an API key.

Run:  .venv\\Scripts\\python tests\\test_interrupt.py
"""
import json
import os
import math
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
from proc_util import kill_tree  # noqa: E402

AP, OP = 8150, 8151
AGENT, ADMIN = f"http://127.0.0.1:{AP}", f"http://127.0.0.1:{OP}"
RUNS = ROOT / "outputs" / "test_runs"
fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({info})" if info and not cond else ""), flush=True)
    if not cond:
        fails.append(name)


def http(base, method, path, body=None, headers=None):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                 method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return e.code, {"error": raw[:80].decode(errors="replace")}


def status():
    return http(ADMIN, "GET", "/admin/status")[1]


def say(text):
    return http(ADMIN, "POST", "/admin/instruct", {"text": text})[1]


def wait(pred, timeout=60, step=0.05):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = status()
        if pred(s):
            return s
        time.sleep(step)
    return None


def doing(op, tid):
    return lambda s: (s["engine"]["action"] or "").startswith(op) and s["engine"]["task_id"] == tid


def finished(tid):
    def f(s):
        t = next((h for h in s["history"] if h["id"] == tid), None)
        return t is not None and t["status"] not in ("Starting", "Running", "Paused")
    return f


def task(s, tid):
    return next(h for h in s["history"] if h["id"] == tid)


def actions(tid):
    p = RUNS / tid / "actions.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def running_agent_tasks():
    """Task ids of agent processes still alive (Windows only; None elsewhere)."""
    if sys.platform != "win32":
        return None
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                          "ForEach-Object { $_.CommandLine }"], capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if "run.py" in line and "--task-id" in line]


def reset():
    http(ADMIN, "POST", "/admin/reset", {"level": "level1", "seed": 0})


def same_pose(a, b, tol=1e-3):
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"]) < tol and abs(a["heading"] - b["heading"]) < 0.2


def handover(name, old, new):
    """Old task interrupted mid-action; new task starts from exactly where it stopped."""
    a_old, a_new = actions(old), actions(new)
    s = status()
    check(f"{name}: old task marked Interrupted", task(s, old)["status"] == "Interrupted", task(s, old)["status"])
    started = task(s, new)["started"]
    over = [a for a in a_old if a["ts"] > started + 0.05 and not a["interrupted"]]
    check(f"{name}: no old action kept running after the new instruction", bool(a_old) and not over, over[:1])
    if a_old and a_new:
        check(f"{name}: new task starts from the actual pose (no reset/teleport)",
              same_pose(a_old[-1]["pose_after"], a_new[0]["pose_before"]),
              (a_old[-1]["pose_after"], a_new[0]["pose_before"]))
    started = task(s, new)["started"]
    late = [a for a in a_old if a["t0"] and a["t0"] > started]
    check(f"{name}: no action of the old task started after the new instruction", not late, late[:1])


def main():
    eng = subprocess.Popen([sys.executable, str(ROOT / "engine" / "sim.py"), "--headless", "--port", str(AP),
                            "--admin-port", str(OP), "--brain", "local"], cwd=str(ROOT),
                           env={**os.environ, "FETCH_RUNS_DIR": str(ROOT / "outputs" / "test_runs")},
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(120):
            try:
                status()
                break
            except OSError:
                time.sleep(0.25)
        run_all()
    finally:
        kill_tree(eng)
    print(f"\n{len(fails)} failed")
    sys.exit(1 if fails else 0)


def run_all():
    # 1. new command while moving
    reset()
    t1 = say("Move to the green zone.")["task_id"]
    check("1 moving: a move is running", wait(doing("move", t1), 30) is not None)
    t2 = say("Rotate 90 degrees.")["task_id"]
    s = wait(finished(t2), 30)
    handover("1 moving", t1, t2)
    check("1 moving: new task completed", s is not None and task(s, t2)["status"] == "Completed")

    # 2. new command while turning
    t1 = say("Rotate 180 degrees.")["task_id"]
    check("2 turning: a turn is running", wait(doing("turn", t1), 30) is not None)
    time.sleep(0.4)
    t2 = say("Move back 0.5 m.")["task_id"]
    s = wait(finished(t2), 30)
    handover("2 turning", t1, t2)
    mv = [a for a in actions(t2) if a["tool"] == "move"]
    check("2 turning: new task did the move back", bool(mv) and abs(mv[0]["result"]["moved"] + 0.5) < 1e-6, mv[:1])

    # 3. new command while circling
    reset()
    t1 = say("Circle around the blue sphere twice.")["task_id"]
    check("3 circling: an arc is running", wait(doing("arc", t1), 60) is not None)
    time.sleep(1.0)
    t2 = say("Rotate 90 degrees.")["task_id"]
    s = wait(finished(t2), 30)
    handover("3 circling", t1, t2)
    arc = [a for a in actions(t1) if a["tool"] == "arc"]
    check("3 circling: arc stopped part-way round", bool(arc) and 0 < arc[-1]["result"]["moved"] < 3.5, arc[-1:])

    # 4. new command while picking up
    reset()
    t1 = say("Pick up the orange cone.")["task_id"]
    check("4 picking: pickup is running", wait(doing("pickup", t1), 60, 0.02) is not None)
    t2 = say("Move back 0.5 m.")["task_id"]
    s = wait(finished(t2), 30)
    a = actions(t1)[-1]
    check("4 picking: pickup interrupted before the grasp", a["tool"] == "pickup" and a["result"] ==
          {"ok": False, "interrupted": True}, a["result"])
    check("4 picking: hand empty afterwards, state consistent", s["robot"]["holding"] is False)
    check("4 picking: new task started from the same pose",
          same_pose(a["pose_after"], actions(t2)[0]["pose_before"]))

    # 5. new command while carrying
    reset()
    t1 = say("Pick up the red sphere.")["task_id"]
    s = wait(finished(t1), 60)
    check("5 carrying: picked up the red sphere", s is not None and task(s, t1)["status"] == "Completed"
          and s["robot"]["holding"], task(s, t1)["result"] if s else None)
    t2 = say("Move to the green zone.")["task_id"]
    check("5 carrying: moving while holding", wait(doing("move", t2), 30) is not None)
    t3 = say("Circle around the blue sphere once.")["task_id"]
    s = wait(finished(t3), 90)
    handover("5 carrying", t2, t3)
    ctx = task(status(), t3).get("context") or ""
    check("5 carrying: object still held after the interruption", actions(t3)[0]["pose_before"]["holding"] is True)
    check("5 carrying: new agent told it is holding an object", "holding an object" in ctx)
    arcs = [a for a in actions(t3) if a["tool"] == "arc"]
    check("5 carrying: circles anticlockwise so the load stays outside", bool(arcs) and all(a["args"]["degrees"] < 0 for a in arcs), arcs[:1])
    check("5 carrying: still holding at the end", s is not None and s["robot"]["holding"])

    # 6. four consecutive instructions: only the latest runs
    reset()
    ids = []
    for text in ("Go towards the red cube.", "Actually, go around the blue sphere.",
                 "No, pick up the orange cone.", "Move to the green zone."):
        ids.append(say(text)["task_id"])
        time.sleep(1.2)
    s = wait(finished(ids[-1]), 60)
    check("6 consecutive: last instruction is the active/completed task",
          s is not None and s["task"]["id"] == ids[-1] and task(s, ids[-1])["status"] == "Completed",
          task(s, ids[-1]) if s else None)
    check("6 consecutive: the three earlier tasks were interrupted",
          all(task(s, t)["status"] == "Interrupted" for t in ids[:-1]), [task(s, t)["status"] for t in ids[:-1]])
    for old, new in zip(ids, ids[1:]):
        late = [a for a in actions(old) if a["t0"] and a["t0"] > task(s, new)["started"]]
        check(f"6 consecutive: nothing from '{task(s, old)['command'][:24]}' ran after it was replaced", not late)

    # 6b. replaced agents are really gone (on Windows the venv launcher has a child process)
    time.sleep(1.0)
    alive = running_agent_tasks()
    check("6 consecutive: no process of a replaced task is still running",
          alive is None or not any(t in alive for t in ids[:-1]), alive)

    # 7. stale requests are refused at the engine, even sent by hand
    code, body = http(AGENT, "POST", "/move", {"distance": 0.5}, {"X-Task-Id": ids[0]})
    before = status()["robot"]
    check("7 stale: request with an old task id refused (409)", code == 409 and "stale" in body.get("error", ""), (code, body))
    check("7 stale: robot did not move", same_pose(before, status()["robot"]))
    code, _ = http(AGENT, "GET", "/look", headers={"X-Task-Id": ids[1]})
    check("7 stale: observations refused too for an old task", code == 409)
    code, _ = http(AGENT, "POST", "/admin/reset", {})
    check("7 isolation: agent port has no operator endpoints", code == 404)

    # 8. status questions do not cancel the task
    reset()
    t1 = say("Circle around the yellow cylinder twice.")["task_id"]
    check("8 status: circling", wait(doing("arc", t1), 60) is not None)
    r1, r2 = say("What are you doing?"), say("How far have you travelled?")
    s = status()
    check("8 status: questions classified as status", r1["kind"] == "status" and r2["kind"] == "status")
    check("8 status: answer names the task", "yellow cylinder" in r1["reply"], r1["reply"])
    check("8 status: distance answer in metres", " m " in r2["reply"], r2["reply"])
    check("8 status: same task still running", s["task"]["id"] == t1 and s["task"]["status"] == "Running")
    s = wait(finished(t1), 90)
    check("8 status: original task completed", s is not None and task(s, t1)["status"] == "Completed",
          task(s, t1) if s else None)
    # the orbit's own count (route curves on the way to the circle are arcs too, so summing every arc overcounts)
    res = (task(s, t1) or {}).get("result") or ""
    m = __import__("re").search(r"\((\d+) degrees\)", res)
    sw = float(m.group(1)) if m else 0.0
    check("8 status: full 720 degrees despite the questions", abs(abs(sw) - 720) < 1.0, res)

    # 9. pause / resume / stop
    reset()
    t1 = say("Move to the green zone.")["task_id"]
    wait(doing("move", t1), 30)
    say("pause")
    p0 = status()["robot"]
    time.sleep(1.0)
    p1 = status()["robot"]
    check("9 pause: robot frozen while paused", same_pose(p0, p1), (p0, p1))
    check("9 pause: mode paused, task kept", status()["mode"] == "paused" and status()["task"]["id"] == t1)
    say("resume")
    time.sleep(0.5)
    check("9 resume: robot moving again", not same_pose(p1, status()["robot"]))
    say("stop")
    time.sleep(0.3)
    p2 = status()["robot"]
    time.sleep(0.8)
    s = status()
    check("9 stop: robot stopped, task Stopped, waiting", same_pose(p2, s["robot"]) and
          task(s, t1)["status"] == "Stopped" and s["mode"] == "waiting", (task(s, t1)["status"], s["mode"]))

    # 10. refinement keeps the goal
    reset()
    t1 = say("Approach the red sphere.")["task_id"]
    wait(doing("move", t1), 30)
    r = say("slower")
    s = status()
    check("10 refine: 'slower' updates the task", r["kind"] == "refine" and "red sphere" in s["task"]["command"]
          and task(s, t1)["status"] == "Refined", r)
    s = wait(finished(r["task_id"]), 60)
    mv = [a for a in actions(r["task_id"]) if a["tool"] == "move"]
    check("10 refine: moves now use the slow speed", bool(mv) and all(a["args"]["speed"] < 1 for a in mv), mv[:1])

    # 11. outputs: video, logs, command, result
    t = r["task_id"]
    d = RUNS / t
    for _ in range(180):                           # encoding runs in the background; result.json comes after it
        if (d / f"{t}.mp4").exists() and (d / "result.json").exists():
            break
        time.sleep(0.5)
    vids = [d / f"{t}.mp4"] if (d / f"{t}.mp4").exists() else []
    check("11 outputs: MP4 written for the run", bool(vids), list(d.iterdir()))
    if vids:
        import imageio.v2 as imageio
        with imageio.get_reader(vids[0], format="FFMPEG") as rd:
            meta = rd.get_meta_data()
        check("11 outputs: video is 1440x1080", tuple(meta["size"]) == (1440, 1080), meta["size"])
    log = actions(t)
    keys = {"ts", "t0", "task_id", "command", "tool", "args", "result", "pose_before", "pose_after",
            "collided", "frames", "outcome"}
    check("11 outputs: every action log line has the required fields", log and all(keys <= set(e) for e in log))
    check("11 outputs: command.txt and result.json present", (d / "command.txt").exists() and (d / "result.json").exists())
    # problem-statement log: one JSONL line per step with step, timestamp, tool, args, result, frame_path;
    # every image the agent saw saved as PNG next to it
    steps = [json.loads(x) for x in (d / "steps.jsonl").read_text(encoding="utf-8").splitlines()] if (d / "steps.jsonl").exists() else []
    need = {"step", "timestamp", "tool", "args", "result", "frame_path"}
    check("11 outputs: steps.jsonl has every PS field on every line", bool(steps) and all(need <= set(x) for x in steps))
    looks = [x for x in steps if x["tool"] == "look"]
    check("11 outputs: every look has its PNG next to the log", bool(looks) and all(
        x["frame_path"] and x["frame_path"].endswith(".png") and (d / x["frame_path"]).exists() for x in looks))
    check("11 outputs: steps are numbered 1, 2, 3 ...", [x["step"] for x in steps] == list(range(1, len(steps) + 1)))


if __name__ == "__main__":
    main()
