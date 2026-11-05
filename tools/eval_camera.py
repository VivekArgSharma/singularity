"""Camera height experiment (the problem statement fixes 0.5 m; other heights are only tested
here, to see whether asking the mentors for a change would be worth it).

For each height: render the robot camera from known poses around every object and score the
camera-only perception against ground truth (detected, right shape, position error), then run
real fetch tasks through the engine and the local planner.

    .venv\\Scripts\\python tools\\eval_camera.py          -> outputs/eval/camera_height.md
"""
import json
import math
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEIGHTS = (0.5, 0.38, 0.28)
COMMANDS = ("Bring the red cube to the green square.", "Bring the round red thing to the green square.",
            "Bring me something to drink.", "Bring the blue cube to the green square.")


def worker_perception():
    sys.path.insert(0, str(ROOT / "engine"))
    sys.path.insert(0, str(ROOT / "agent"))
    from sim import Engine
    import perception as P
    from world import forward
    e = Engine(headless=True)
    r, w = e.robot, e.robot.world
    rows = []
    for o in w.objects:
        away = math.atan2(-o.y, -o.x)                         # stand on the room-centre side of the object
        for d in (0.3, 0.5, 0.8, 1.2, 1.8, 2.5):
            for b in (-30.0, -15.0, 0.0, 15.0, 30.0):
                dist = d + o.r + r.r
                x, y = o.x + math.cos(away) * dist, o.y + math.sin(away) * dist
                if abs(x) > w.half - 0.25 or abs(y) > w.half - 0.25 or w.circle_blocked(x, y, r.r + 0.02):
                    continue
                to_obj = math.degrees(math.atan2(-(o.y - y), o.x - x))
                r.x, r.y, r.heading = x, y, to_obj - b          # object at bearing +b (right positive)
                e.views.sync(r)
                import io
                from PIL import Image
                buf = io.BytesIO()
                Image.fromarray(e.views.grab("look")).save(buf, format="JPEG", quality=90)
                dets = [dd for dd in P.detect(buf.getvalue()) if dd["color"] == o.color_name]
                fx, fy = forward(r.heading)
                cam = (x + fx * r.r, y + fy * r.r)
                true_b = math.degrees(math.atan2(-(o.y - cam[1]), o.x - cam[0])) - r.heading
                true_b = (true_b + 180) % 360 - 180
                hit = [dd for dd in dets if abs(dd["bearing"] - true_b) < 10]
                rec = {"obj": f"{o.color_name} {o.shape}", "d": d, "b": b, "detected": bool(hit)}
                if hit:
                    h = max(hit, key=lambda dd: dd["area"])
                    rec["shape_ok"] = h["shape"] == o.shape
                    rec["shape_known"] = h["shape"] is not None
                    if "centre_rel" in h:
                        wx, wy = P.to_world(h["centre_rel"], x, y, r.heading)
                        rec["pos_err"] = round(math.hypot(wx - o.x, wy - o.y), 3)
                rows.append(rec)
    print(json.dumps({"perception": rows}))


def worker_tasks(port):
    env = {**os.environ}
    eng = subprocess.Popen([sys.executable, str(ROOT / "engine" / "sim.py"), "--headless", "--port", str(port),
                            "--admin-port", str(port + 1), "--brain", "local"], cwd=str(ROOT), env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sys.path.insert(0, str(ROOT / "engine"))
    from proc_util import kill_tree
    A = f"http://127.0.0.1:{port + 1}"

    def call(m, p, b=None):
        req = urllib.request.Request(A + p, data=json.dumps(b).encode() if b is not None else None, method=m,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as f:
            return json.loads(f.read())
    out = []
    try:
        for _ in range(120):
            try:
                call("GET", "/admin/status")
                break
            except OSError:
                time.sleep(0.25)
        for cmd in COMMANDS:
            call("POST", "/admin/reset", {"level": "level1", "seed": 0})
            tid = call("POST", "/admin/instruct", {"text": cmd})["task_id"]
            t0 = time.time()
            while time.time() - t0 < 240:
                s = call("GET", "/admin/status")
                t = next(h for h in s["history"] if h["id"] == tid)
                if t["status"] not in ("Starting", "Running"):
                    break
                time.sleep(0.5)
            out.append({"command": cmd, "status": t["status"], "outcome": s["robot"]["outcome"],
                        "sim_s": s["robot"]["sim_seconds"], "collisions": s["robot"]["collisions"],
                        "actions": t["actions"], "result": t["result"]})
    finally:
        kill_tree(eng)
    print(json.dumps({"tasks": out}))


def summarise(h, rows, tasks):
    def stats(sel):
        det = [r for r in sel if r["detected"]]
        errs = sorted(r["pos_err"] for r in det if "pos_err" in r)
        known = [r for r in det if r.get("shape_known")]
        return {"poses": len(sel), "detected": round(len(det) / max(1, len(sel)), 3),
                "shape_correct": round(sum(r["shape_ok"] for r in known) / max(1, len(known)), 3),
                "pos_err_median_m": errs[len(errs) // 2] if errs else None,
                "pos_err_mean_m": round(sum(errs) / len(errs), 3) if errs else None,
                "located": round(len(errs) / max(1, len(sel)), 3)}
    near = stats([r for r in rows if r["d"] <= 0.5])
    mid = stats([r for r in rows if 0.5 < r["d"] < 1.8])
    far = stats([r for r in rows if r["d"] >= 1.8])
    allr = stats(rows)
    passed = sum(1 for t in tasks if t["outcome"] in ("pass", "needs_manual") and t["status"] == "Completed")
    return {"height_m": h, **{f"all_{k}": v for k, v in allr.items()},
            "near_detected": near["detected"], "near_located": near["located"],
            "mid_pos_err_m": mid["pos_err_median_m"], "far_pos_err_m": far["pos_err_median_m"],
            "far_detected": far["detected"], "tasks_done": f"{passed}/{len(tasks)}",
            "task_sim_s": round(sum(t["sim_s"] for t in tasks) / len(tasks), 1),
            "task_collisions": sum(t["collisions"] for t in tasks)}


def main():
    if "--perception" in sys.argv:
        return worker_perception()
    if "--tasks" in sys.argv:
        return worker_tasks(int(sys.argv[sys.argv.index("--tasks") + 1]))
    res, detail = [], {}
    for n, h in enumerate(HEIGHTS):
        env = {**os.environ, "FETCH_CAM_H": str(h), "FETCH_RUNS_DIR": str(ROOT / "outputs" / "test_runs")}
        p = subprocess.run([sys.executable, __file__, "--perception"], env=env, capture_output=True, text=True, cwd=str(ROOT))
        rows = json.loads(p.stdout.strip().splitlines()[-1])["perception"]
        t = subprocess.run([sys.executable, __file__, "--tasks", str(8210 + 2 * n)], env=env, capture_output=True,
                           text=True, cwd=str(ROOT))
        tasks = json.loads(t.stdout.strip().splitlines()[-1])["tasks"]
        res.append(summarise(h, rows, tasks))
        detail[str(h)] = {"tasks": tasks}
        print(res[-1], flush=True)
        for x in tasks:
            print("   ", x["command"], "->", x["status"], x["outcome"], x["result"], flush=True)
    keys = list(res[0].keys())
    md = ("# Camera height experiment (measured)\n\n0.5 m is the problem-statement value; the others are tests only. "
          "Perception scored against ground truth over the same poses (every object, 0.3-2.5 m from the robot front, "
          "bearings -30..+30 deg); tasks run through the real engine and the local planner.\n\n"
          "| " + " | ".join(keys) + " |\n|" + "---|" * len(keys) + "\n"
          + "".join("| " + " | ".join(str(r[k]) for k in keys) + " |\n" for r in res))
    d = ROOT / "outputs" / "eval"
    d.mkdir(parents=True, exist_ok=True)
    (d / "camera_height.md").write_text(md)
    (d / "camera_height.json").write_text(json.dumps({"summary": res, "detail": detail}, indent=2))
    print(md)


if __name__ == "__main__":
    main()
