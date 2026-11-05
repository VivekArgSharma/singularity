"""Stretch-goal demo on the obstacle level, driven through the operator API like a person at the dashboard.

    .venv\\Scripts\\python tools\\stretch_demo.py                 # own headless engine, prints a pass/fail table
    .venv\\Scripts\\python tools\\stretch_demo.py --live          # drive the visible engine already running on :8081
    .venv\\Scripts\\python tools\\stretch_demo.py --repeat 3      # repeatability check

Sequence (obstacle level, seed 0):
  1 "Explore the room."                                   next-best-view exploration, map coverage
  2 "Bring the red cube to the green square."             A* around the walls ...
    ... replaced after 6 s by "Actually, bring the blue sphere to the green square instead."
  3 "Where are you?" while 2 runs                         a status question must NOT cancel the task
  4 "Circle around the orange cone twice."                true circular /arc orbit, 720 deg
  5 "Bring the tallest object."                           compares camera height estimates of all objects
Each step is checked against the engine's own outcome / task status.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))


def make_call(admin):
    def call(m, p, b=None):
        req = urllib.request.Request(admin + p, data=json.dumps(b).encode() if b is not None else None, method=m,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as f:
            return json.loads(f.read())
    return call


def wait(call, tid, timeout=400):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = call("GET", "/admin/status")
        t = next((h for h in s["history"] if h["id"] == tid), None)
        if t and t["status"] not in ("Starting", "Running"):
            return t, s
        time.sleep(0.5)
    return t, s


def progress(call, tid, actions, timeout=120):
    """Wait until the task has made `actions` robot actions (deterministic point in the task)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = call("GET", "/admin/status")
        t = next((h for h in s["history"] if h["id"] == tid), None)
        if t is None or t["status"] not in ("Starting", "Running") or (t.get("actions") or 0) >= actions:
            return
        time.sleep(0.05)


def demo(call):
    rows = []
    call("POST", "/admin/reset", {"level": "obstacles", "seed": 0})

    t = call("POST", "/admin/instruct", {"text": "Explore the room."})
    task, s = wait(call, t["task_id"])
    rows.append(("explore", task["status"] == "Completed", task["result"]))

    t = call("POST", "/admin/instruct", {"text": "Bring the red cube to the green square."})
    progress(call, t["task_id"], 4)            # by task progress, not wall-clock time: repeatable under load
    q = call("POST", "/admin/instruct", {"text": "Where are you?"})
    s = call("GET", "/admin/status")
    still = next(h for h in s["history"] if h["id"] == t["task_id"])["status"] in ("Starting", "Running")
    rows.append(("status question does not cancel", still and q.get("kind") == "status", q.get("reply", "")[:90]))
    progress(call, t["task_id"], 9)
    t2 = call("POST", "/admin/instruct", {"text": "Actually, bring the blue sphere to the green square instead."})
    old, _ = wait(call, t["task_id"], 30)
    task, s = wait(call, t2["task_id"])
    rows.append(("replaced mid-run", old["status"] in ("Interrupted", "Replaced", "Superseded", "Cancelled") or
                 old.get("superseded_by") == t2["task_id"], f"old task: {old['status']}"))
    rows.append(("replacement fetch", s["robot"]["outcome"] == "pass", task["result"]))

    t = call("POST", "/admin/instruct", {"text": "Circle around the orange cone twice."})
    task, s = wait(call, t["task_id"])
    rows.append(("orbit 720 deg", task["status"] == "Completed" and "720" in (task["result"] or ""), task["result"]))

    t = call("POST", "/admin/instruct", {"text": "Bring the tallest object."})
    task, s = wait(call, t["task_id"])
    rows.append(("tallest object", s["robot"]["outcome"] == "pass", task["result"]))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--port", type=int, default=8700)
    a = ap.parse_args()
    eng = None
    admin = "http://127.0.0.1:8081"
    if not a.live:
        from proc_util import kill_tree
        env = {**os.environ, "FETCH_RUNS_DIR": str(ROOT / "outputs" / "eval_runs" / "stretch")}
        eng = subprocess.Popen([sys.executable, str(ROOT / "engine" / "sim.py"), "--headless", "--port", str(a.port),
                                "--admin-port", str(a.port + 1), "--brain", "local"], cwd=str(ROOT), env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        admin = f"http://127.0.0.1:{a.port + 1}"
    call = make_call(admin)
    try:
        for _ in range(160):
            try:
                call("GET", "/admin/status")
                break
            except OSError:
                time.sleep(0.25)
        allrows = []
        for k in range(a.repeat):
            rows = demo(call)
            allrows.append(rows)
            print(f"\nrun {k + 1}")
            for name, ok, info in rows:
                print(f"  {'PASS' if ok else 'FAIL'} {name:34} {str(info)[:110]}")
    finally:
        if eng is not None:
            kill_tree(eng)
    n = sum(ok for rows in allrows for _, ok, _ in rows)
    tot = sum(len(rows) for rows in allrows)
    print(f"\n{n}/{tot} steps passed over {a.repeat} run(s)")
    (ROOT / "outputs" / "eval" / "stretch_demo.json").write_text(json.dumps(
        [[{"step": s, "ok": ok, "info": info} for s, ok, info in rows] for rows in allrows], indent=2))
    sys.exit(0 if n == tot else 1)


if __name__ == "__main__":
    main()
