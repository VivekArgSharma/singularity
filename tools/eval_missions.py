"""End-to-end mission runs through the real engine + dashboard controller + local planner.

    .venv\\Scripts\\python tools\\eval_missions.py [--missions maze,colour_sort,brain_puzzle] [--seeds 0,1,2] [--label base]

One headless engine per mission (in parallel), seeds in sequence. The outcome comes from the engine's
private mission evaluator (simulator state), never from what the agent says.
Results -> outputs/eval/missions_<label>.md / .json
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
from proc_util import kill_tree  # noqa: E402


def worker(mission, seeds, port, runs_dir, out, timeout):
    runs_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "FETCH_RUNS_DIR": str(runs_dir)}
    eng = subprocess.Popen([sys.executable, str(ROOT / "engine" / "sim.py"), "--headless", "--port", str(port),
                            "--admin-port", str(port + 1), "--brain", "local"], cwd=str(ROOT), env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    A = f"http://127.0.0.1:{port + 1}"

    def call(m, p, b=None):
        req = urllib.request.Request(A + p, data=json.dumps(b).encode() if b is not None else None, method=m,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as f:
            return json.loads(f.read())
    try:
        for _ in range(160):
            try:
                call("GET", "/admin/status")
                break
            except OSError:
                time.sleep(0.25)
        for seed in seeds:
            t0 = time.time()
            r = call("POST", "/admin/mission", {"mission": mission, "seed": seed})
            tid = r["task"]["task_id"]
            while time.time() - t0 < timeout:
                s = call("GET", "/admin/status")
                t = next(h for h in s["history"] if h["id"] == tid)
                if t["status"] not in ("Starting", "Running"):
                    break
                time.sleep(1.0)
            s = call("GET", "/admin/status")
            t = next(h for h in s["history"] if h["id"] == tid)
            ms, rb = s.get("mission") or {}, s["robot"]
            row = {"mission": mission, "seed": seed, "status": t["status"], "outcome": ms.get("outcome") or rb["outcome"],
                   "result": t.get("result"), "actions": t.get("actions"), "sim_s": rb["sim_seconds"],
                   "collisions": rb["collisions"], "path_m": rb["path_length"], "wall_s": round(time.time() - t0, 1),
                   "evaluator": {k: v for k, v in ms.items() if k not in ("events",)}, "events": ms.get("events")}
            out.append(row)
            print(f"{mission:13} seed {seed} | {str(row['outcome']):22} | {row['actions']} actions | {row['sim_s']:7.1f} s "
                  f"| coll {row['collisions']} | {str(row['result'])[:110]}", flush=True)
            if t["status"] in ("Starting", "Running"):
                call("POST", "/admin/stop", {})
    finally:
        kill_tree(eng)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--missions", default="maze,colour_sort,brain_puzzle")
    ap.add_argument("--seeds", default="0,1,2")
    ap.add_argument("--label", default="base")
    ap.add_argument("--timeout", type=float, default=900)
    ap.add_argument("--port", type=int, default=8600)
    a = ap.parse_args()
    seeds = [int(x) for x in a.seeds.split(",")]
    rows = []
    runs = ROOT / "outputs" / "eval_runs" / f"missions_{a.label}"
    ts = [threading.Thread(target=worker, args=(m, seeds, a.port + 2 * i, runs / m, rows, a.timeout))
          for i, m in enumerate(a.missions.split(","))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    lines = [f"# Missions, local planner ({a.label})\n",
             "| mission | seed | outcome | actions | sim s | collisions | path m | result |", "|---|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: (r["mission"], r["seed"])):
        lines.append(f"| {r['mission']} | {r['seed']} | {r['outcome']} | {r['actions']} | {r['sim_s']} | {r['collisions']} "
                     f"| {r['path_m']} | {str(r['result'])[:160]} |")
    by = {}
    for r in rows:
        by.setdefault(r["mission"], []).append(r["outcome"] == "pass")
    lines.append("\n" + ", ".join(f"{m}: {sum(v)}/{len(v)} pass" for m, v in sorted(by.items())))
    d = ROOT / "outputs" / "eval"
    (d / f"missions_{a.label}.json").write_text(json.dumps(rows, indent=2))
    (d / f"missions_{a.label}.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
