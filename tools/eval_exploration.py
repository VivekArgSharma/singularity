"""Measure exploration and search strategies in the real engine (operator tool).

The robot side only ever sees /state, /look, /scan, /depth. Ground truth (room size,
object footprints) is used HERE, on the operator side, to score how much of the real
floor each strategy's observations actually covered.

    .venv\\Scripts\\python tools\\eval_exploration.py            # full comparison
    .venv\\Scripts\\python tools\\eval_exploration.py --quick    # one scenario

Writes outputs/eval/exploration.json and exploration.md.
"""
import argparse
import json
import math
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
from proc_util import kill_tree  # noqa: E402

sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "engine"))
import explore  # noqa: E402
from client import Client  # noqa: E402
from local_brain import LocalBrain  # noqa: E402
from mapping import CAM_NEAR, MAX_RANGE  # noqa: E402
from world import World, forward  # noqa: E402

AP, OP = 8190, 8191
AGENT, ADMIN = f"http://127.0.0.1:{AP}", f"http://127.0.0.1:{OP}"
GT_CELL = 0.05


def admin(method, path, body=None):
    req = urllib.request.Request(ADMIN + path, data=json.dumps(body).encode() if body is not None else None,
                                 method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


class GroundTruth:
    """Real visible floor per observation, from the engine's own geometry (operator only)."""
    def __init__(self, level, seed):
        self.w = World(level, seed)
        n = int(round(2 * self.w.half / GT_CELL))
        xs = -self.w.half + (np.arange(n) + 0.5) * GT_CELL
        self.X, self.Y = np.meshgrid(xs, xs)
        floor = np.ones_like(self.X, bool)
        for o in self.w.objects:
            floor &= ~np.vectorize(lambda a, b, o=o: o.dist_to_point(a, b) <= 0.0)(self.X, self.Y)
        self.floor = floor
        self.n = n
        self.seen = np.zeros_like(floor)

    def view(self, x, y, heading, r=0.2):
        fx, fy = forward(heading)
        ox, oy = x + fx * r, y + fy * r
        cells = []
        for k in range(181):
            b = -45 + 90 * k / 180
            d = self.w.ray(ox, oy, heading + b)
            d = 10.0 if d is None else d
            ds = np.arange(CAM_NEAR, min(d, MAX_RANGE), GT_CELL / 2)
            dx, dy = forward(heading + b)
            i = ((ox + dx * ds + self.w.half) / GT_CELL).astype(int)
            j = ((oy + dy * ds + self.w.half) / GT_CELL).astype(int)
            ok = (i >= 0) & (i < self.n) & (j >= 0) & (j < self.n)
            cells.append(j[ok] * self.n + i[ok])
        c = np.unique(np.concatenate(cells)) if cells else np.zeros(0, int)
        c = c[self.floor.ravel()[c]]
        before = self.seen.ravel()[c]
        new, rep = int((~before).sum()), int(before.sum())
        self.seen.ravel()[c] = True
        return new * GT_CELL ** 2, rep * GT_CELL ** 2

    def coverage(self):
        return float((self.seen & self.floor).sum() / self.floor.sum())


def instrumented(level, seed, mapping=True, command="explore"):
    """A LocalBrain whose every observation and turn is scored against ground truth."""
    admin("POST", "/admin/reset", {"level": level, "seed": seed})
    admin("POST", "/admin/command", {"text": command, "run_id": "eval"})
    admin("POST", "/admin/record_stop")          # no video needed for measurements
    gt = GroundTruth(level, seed)
    log = {"obs": [], "turns": [], "pending_turn": 0.0}
    cl = Client(AGENT, None, lambda *a, **k: None)
    b = LocalBrain(cl, lambda *a, **k: None, command, mapping=mapping)
    raw_turn, raw_see = cl.turn, b.see

    def turn(deg, speed=1.0):
        r = raw_turn(deg, speed)
        log["turns"].append(abs(deg))
        log["pending_turn"] += abs(deg)
        return r

    def see():
        dets = raw_see()
        s = b.s
        new, rep = gt.view(s["x"], s["y"], s["heading"])
        res = admin("GET", "/admin/result")
        log["obs"].append({"x": s["x"], "y": s["y"], "h": s["heading"], "new": new, "rep": rep,
                           "cov": gt.coverage(), "t": res["sim_seconds"], "path": res["path_length"],
                           "turned_before": log["pending_turn"]})
        log["pending_turn"] = 0.0
        return dets

    cl.turn, b.see = turn, see
    return b, gt, log


def summarise(log, gt, why, target=0.9):
    res = admin("GET", "/admin/result")
    obs = log["obs"]
    views = sum(o["new"] + o["rep"] for o in obs) or 1e-9
    reach = next((o["t"] for o in obs if o["cov"] >= target), None)
    wasted = sum(1 for o in obs if o["turned_before"] > 0 and o["new"] < 0.1)
    return {"coverage": round(gt.coverage(), 3), "distance_m": res["path_length"], "viewpoints": len(obs),
            "redundant": round(sum(o["rep"] for o in obs) / views, 3), "turns": len(log["turns"]),
            "turn_deg": round(sum(log["turns"]), 0), "wasted_turns": wasted, "collisions": res["collisions"],
            "sim_seconds": res["sim_seconds"], f"t_to_{int(target * 100)}pct": reach, "stopped": why}


def run_exploration(scenarios, strategies, max_views):
    out = []
    for level, seed in scenarios:
        for name in strategies:
            b, gt, log = instrumented(level, seed)
            t0 = time.time()
            try:
                why = explore.STRATEGIES[name](b, goal=0.95, max_views=max_views)
            except Exception as e:  # noqa: BLE001  (e.g. fail_collision)
                why = f"error: {e}"
            row = {"level": level, "seed": seed, "strategy": name, **summarise(log, gt, why),
                   "wall_s": round(time.time() - t0, 1)}
            print(row, flush=True)
            out.append(row)
    return out


def run_search(scenarios, targets):
    """Find one object: old 45-degree sweep (no map) vs map-guided search."""
    out = []
    for level, seed in scenarios:
        for colour, shape in targets:
            for mode in ("sweep", "map"):
                b, gt, log = instrumented(level, seed, mapping=(mode == "map"), command=f"find the {colour} {shape}")
                tgt = {"color": colour, "shape": shape, "name": f"{colour} {shape}"}
                try:
                    det = b.find(tgt, sweeps=12)
                    ok = det is not None
                except Exception:  # noqa: BLE001
                    ok = False
                res = admin("GET", "/admin/result")
                row = {"level": level, "seed": seed, "target": f"{colour} {shape}", "mode": mode, "found": ok,
                       "sim_seconds": res["sim_seconds"], "distance_m": res["path_length"],
                       "turns": len(log["turns"]), "turn_deg": round(sum(log["turns"]), 0),
                       "views": len(log["obs"]), "coverage": round(gt.coverage(), 3)}
                print(row, flush=True)
                out.append(row)
    return out


def table(rows, keys):
    head = "| " + " | ".join(keys) + " |\n|" + "---|" * len(keys) + "\n"
    return head + "".join("| " + " | ".join(str(r.get(k)) for k in keys) + " |\n" for r in rows)


def mean(rows, key):
    v = [r[key] for r in rows if r.get(key) is not None]
    return round(sum(v) / len(v), 3) if v else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--strategies", default="sweep,frontier,nbv")
    ap.add_argument("--no-search", action="store_true")
    ap.add_argument("--out", default="exploration")
    a = ap.parse_args()
    eng = subprocess.Popen([sys.executable, str(ROOT / "engine" / "sim.py"), "--headless", "--port", str(AP),
                            "--admin-port", str(OP)], cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(120):
            try:
                admin("GET", "/admin/result")
                break
            except OSError:
                time.sleep(0.25)
        scen = [("level1", 0)] if a.quick else [("level1", 0), ("level1", 1), ("level1", 2), ("level2", 0)]
        strategies = a.strategies.split(",")
        rows = run_exploration(scen, strategies, max_views=40)
        targets = [("red", "cube"), ("blue", "sphere"), ("yellow", "cylinder"), ("orange", "cone")]
        search = [] if a.no_search else run_search(scen[:1] if a.quick else scen[:3], targets[:2] if a.quick else targets)
    finally:
        kill_tree(eng)
    keys = ["level", "seed", "strategy", "coverage", "t_to_90pct", "sim_seconds", "distance_m", "viewpoints",
            "redundant", "turns", "turn_deg", "wasted_turns", "collisions", "stopped"]
    agg = []
    for s in strategies:
        rs = [r for r in rows if r["strategy"] == s]
        agg.append({"strategy": s, **{k: mean(rs, k) for k in keys[3:-1]},
                    "reached_90pct": f"{sum(1 for r in rs if r['t_to_90pct'] is not None)}/{len(rs)}"})
    sagg = []
    for m in ("sweep", "map"):
        rs = [r for r in search if r["mode"] == m]
        sagg.append({"mode": m, "found": f"{sum(r['found'] for r in rs)}/{len(rs)}",
                     **{k: mean(rs, k) for k in ("sim_seconds", "distance_m", "turns", "turn_deg", "views")}})
    d = ROOT / "outputs" / "eval"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{a.out}.json").write_text(json.dumps({"exploration": rows, "summary": agg, "search": search,
                                                    "search_summary": sagg}, indent=2))
    md = ("# Exploration comparison (measured)\n\nCoverage = share of the real floor (6 x 6 m minus object "
          "footprints) the camera actually saw, scored with ground truth on the operator side.\n\n## Per run\n\n"
          + table(rows, keys) + "\n## Mean over scenarios\n\n" + table(agg, ["strategy", "reached_90pct"] + keys[3:-1])
          + "\n## Find one object\n\n" + table(search, ["level", "seed", "target", "mode", "found", "sim_seconds",
                                                          "distance_m", "turns", "turn_deg", "views"])
          + "\n" + table(sagg, ["mode", "found", "sim_seconds", "distance_m", "turns", "turn_deg", "views"]))
    (d / f"{a.out}.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main()
