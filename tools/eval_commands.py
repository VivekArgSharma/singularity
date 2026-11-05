"""The six problem-statement commands x seeds 0, 1, 2 (18 runs), through the real engine + dashboard
controller, exactly as an operator would run them.

    .venv\\Scripts\\python tools\\eval_commands.py [--label base] [--speed 1.0] [--level level1] [--brain local]

Each seed gets its own headless engine (3 in parallel). Per run: outcome, actions, sim time, collisions,
driven distance, straight-line directness, clip compliance. "Bring me something to drink" is graded by hand
under the agreed contract (needs_manual); the operator rule recorded here is: a can delivered = pass.
Results -> outputs/eval/commands_<label>.md / .json
"""
import argparse
import json
import math
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

COMMANDS = ["Bring the red cube to the green square.", "Bring the round red thing.", "Bring the tallest object.",
            "Bring the blue sphere.", "Bring the cube that is farthest from the green square.",
            "Bring me something to drink."]


def ideal_distance(level, seed, cmd):
    """Straight-line lower bound: start -> nearest correct object -> zone centre (ignores reach and obstacles)."""
    from grader import resolve_targets
    from world import World
    w = World(level, seed)
    t = resolve_targets(cmd, w.objects, w.zone_c)
    objs = [o for o in w.objects if (t is None and o.shape == "cylinder") or (t and o.id in t)]
    if not objs:
        return None
    o = min(objs, key=lambda o: math.dist(w.start, (o.x, o.y)))
    return math.dist(w.start, (o.x, o.y)) + math.dist((o.x, o.y), w.zone_c)


def worker(seed, port, args, runs_dir, out):
    runs_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "FETCH_RUNS_DIR": str(runs_dir), "FETCH_MOVE_SPEED": str(args.speed)}
    eng = subprocess.Popen([sys.executable, str(ROOT / "engine" / "sim.py"), "--headless", "--port", str(port),
                            "--admin-port", str(port + 1), "--brain", args.brain, "--level", args.level],
                           cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        for cmd in [c for c in COMMANDS if not args.only or args.only.lower() in c.lower()]:
            call("POST", "/admin/reset", {"level": args.level, "seed": seed})
            t0 = time.time()
            tid = call("POST", "/admin/instruct", {"text": cmd})["task_id"]
            api_errors = 0
            while time.time() - t0 < 400:
                try:
                    s = call("GET", "/admin/status")
                except OSError:
                    api_errors += 1
                    time.sleep(1)
                    continue
                t = next(h for h in s["history"] if h["id"] == tid)
                if t["status"] not in ("Starting", "Running"):
                    break
                time.sleep(0.5)
            for _ in range(360):                      # the clip (and a 20 s time-lapse if longer) is encoded after
                rj = runs_dir / tid / "result.json"
                if rj.exists():
                    j = json.loads(rj.read_text())
                    if j.get("clip_seconds") and (j.get("clip_seconds") <= 20.5 or j.get("clip20")):
                        break
                time.sleep(0.5)
            res = call("GET", "/admin/result")
            rjson = json.loads(rj.read_text()) if rj.exists() else {}
            meta = json.loads((runs_dir / tid / "meta.json").read_text()) if (runs_dir / tid / "meta.json").exists() else {}
            outcome = res["outcome"]
            judged = outcome or "fail (nothing delivered)"
            if outcome == "needs_manual":
                d = res.get("delivered") or {}
                judged = "pass (manual: can delivered)" if d.get("shape") == "cylinder" else "fail (manual)"
            ideal = ideal_distance(args.level, seed, cmd)
            secs = rjson.get("clip_seconds")
            out.append({"seed": seed, "command": cmd, "status": t["status"], "outcome": outcome, "judged": judged,
                        "result": t["result"], "actions": res["actions"], "sim_s": res["sim_seconds"],
                        "collisions": res["collisions"], "path_m": res["path_length"],
                        "directness": round(ideal / res["path_length"], 2) if ideal and res["path_length"] else None,
                        "delivered": res.get("delivered"), "wall_s": round(time.time() - t0, 1),
                        "clip_s": secs, "clip_ok": bool(rjson.get("clip_rule_10_20s") or rjson.get("clip20")),
                        "video_px": f"{meta.get('width')}x{meta.get('height')}", "api_errors": api_errors})
            print(f"seed {seed} | {cmd:58} | {judged:28} | {res['actions']:3d} actions | {res['sim_seconds']:6.1f} s "
                  f"| coll {res['collisions']}", flush=True)
    finally:
        kill_tree(eng)


def report(rows, args):
    ok = [r for r in rows if r["judged"].startswith("pass")]
    n = len(rows)
    agg = {"runs": n, "passed": len(ok), "success_rate": round(len(ok) / max(1, n), 3),
           "mean_actions_success": round(sum(r["actions"] for r in ok) / max(1, len(ok)), 1),
           "mean_sim_s_success": round(sum(r["sim_s"] for r in ok) / max(1, len(ok)), 1),
           "mean_directness_success": round(sum(r["directness"] or 0 for r in ok) / max(1, len(ok)), 2),
           "collisions_total": sum(r["collisions"] for r in rows),
           "clip_compliant": sum(r["clip_ok"] for r in rows), "api_errors": sum(r["api_errors"] for r in rows),
           "cost_usd": 0.0 if args.brain == "local" else None, "speed_m_s": args.speed, "level": args.level,
           "brain": args.brain}
    lines = [f"# Command set: 6 commands x seeds 0-2 ({args.label}; brain {args.brain}, {args.speed} m/s, {args.level})\n",
             "| " + " | ".join(f"{k}" for k in agg) + " |", "|" + "---|" * len(agg),
             "| " + " | ".join(str(v) for v in agg.values()) + " |\n",
             "| seed | command | outcome | actions | sim s | collisions | path m | directness | clip s | clip ok | result |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: (r["seed"], COMMANDS.index(r["command"]))):
        lines.append(f"| {r['seed']} | {r['command']} | {r['judged']} | {r['actions']} | {r['sim_s']} | {r['collisions']} "
                     f"| {r['path_m']} | {r['directness']} | {r['clip_s']} | {r['clip_ok']} | {r['result']} |")
    lines.append("\ndirectness = straight-line lower bound (start -> object -> zone centre) / driven distance; "
                 "1.0 is unreachable in practice (reach distance, turning, obstacles).")
    return agg, "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="base")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--level", default="level1")
    ap.add_argument("--brain", default="local")
    ap.add_argument("--seeds", default="0,1,2")
    ap.add_argument("--port", type=int, default=8300)
    ap.add_argument("--only", default="", help="run only commands containing this text")
    a = ap.parse_args()
    runs_dir = ROOT / "outputs" / "eval_runs" / a.label
    runs_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    ts = [threading.Thread(target=worker, args=(int(s), a.port + 2 * i, a, runs_dir / f"seed{s}", rows))
          for i, s in enumerate(a.seeds.split(","))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    agg, md = report(rows, a)
    d = ROOT / "outputs" / "eval"
    (d / f"commands_{a.label}.json").write_text(json.dumps({"summary": agg, "runs": rows}, indent=2))
    (d / f"commands_{a.label}.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main()
