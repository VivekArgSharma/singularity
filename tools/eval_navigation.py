"""Navigation benchmark: existing grid planner vs A* vs Dijkstra (vs A* without smoothing),
same maps, same physics, same start/goal pairs. Pure Python (the engine's robot physics,
no rendering), maps built from the range scan only, clearance measured against ground truth.

    .venv\\Scripts\\python tools\\eval_navigation.py           # comparison + weight sweep
Writes outputs/eval/navigation.json and navigation.md.
"""
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "tools"))
import numpy as np  # noqa: E402

import explore  # noqa: E402
from nav_sim import SimBody  # noqa: E402
from navigate import navigate  # noqa: E402

SCENARIOS = [
    # name, level, start (x, y, heading), goal, final heading, hold object, switch-to goal
    ("open direct", "level1", (-2.4, 0.0, 0.0), (-0.4, 0.9), None, None, None),
    ("around obstacles", "level2", (-2.4, 0.0, 0.0), (2.0, 0.5), None, None, None),
    ("two possible routes", "level2", (-0.3, 1.0, 0.0), (1.9, 1.15), None, None, None),
    ("long diagonal", "level2", (2.2, -2.2, 90.0), (-2.2, 2.2), None, None, None),
    ("narrow passage", "narrow", (-2.4, 0.0, 0.0), (2.2, 0.0), None, None, None),
    ("carrying to the zone", "level1", None, (1.3, 1.2), 0.0, "o1", None),
    ("carrying, gap too narrow", "narrow", None, (2.2, 0.0), None, "o1", None),
    ("destination changed mid-way", "level2", (-2.4, 0.0, 0.0), (2.0, 0.5), None, None, (-0.5, -2.2)),
]
_maps = {}


def survey(level):
    if level not in _maps:
        b = SimBody(level)
        b.survey()
        _maps[level] = b.cmap
    return _maps[level]


class Switch(Exception):
    pass


def instrument(b, limit=None):
    """Count turns/arcs; optionally stop after `limit` actions (a new instruction arrives)."""
    log = {"turns": 0, "turn_deg": 0.0, "arcs": 0, "actions": 0}
    if not hasattr(b, "_orig"):
        b._orig = (b.cl.turn, b.cl.move, b.cl.arc)
    t, m, a = b._orig

    def tick():
        log["actions"] += 1
        if limit is not None and log["actions"] > limit:
            raise Switch()

    def turn(deg, speed=1.0):
        tick()
        if abs(deg) > 0.5:
            log["turns"] += 1
            log["turn_deg"] += abs(deg)
        return t(deg, speed)

    def move(d, speed=1.0):
        tick()
        return m(d, speed)

    def arc(r, deg, speed=1.0):
        tick()
        log["arcs"] += 1
        log["turn_deg"] += abs(deg)
        return a(r, deg, speed)
    b.cl.turn, b.cl.move, b.cl.arc = turn, move, arc
    return log


def drive(b, method, goal, final_heading, plans):
    if method == "grid":
        s = b.st()
        t0 = time.perf_counter()
        cl = 0.62 if s["holding"] else 0.48
        dist, _, _ = b.cmap.distances(s["x"], s["y"], cl)
        route = b.cmap.plan_path((s["x"], s["y"]), goal, cl)
        plans.append({"ms": (time.perf_counter() - t0) * 1000, "expanded": int(np.isfinite(dist).sum())})
        if route is None:
            return False
        explore.follow(b, route, final_heading, observe_legs=False)
        return True
    kw = {"astar": {}, "dijkstra": {"algo": "dijkstra"}, "astar_raw": {"smooth": False, "fillets": False}}[method]
    log = []
    ok = navigate(b, goal, final_heading=final_heading, observe=False, log=log, **kw)
    plans.extend({"ms": c["plan_ms"] or 0, "expanded": c["expanded"]} for c in log)
    return ok


def run(scn, method, weights=None):
    name, level, start, goal, fh, hold, switch = scn
    b = SimBody(level)
    b.cmap = survey(level)
    if weights:
        import planner
        old = dict(planner.WEIGHTS)
        planner.WEIGHTS.update(weights)
    try:
        if hold:
            b.hold(hold)
        else:
            b.r.x, b.r.y, b.r.heading = start
        p0, t0, b.steps = b.r.stats["path_length"], b.r.stats["sim_seconds"], 0
        b.min_body = b.min_app = 9.0
        plans = []
        log = instrument(b, limit=3 if switch else None)
        target = goal
        try:
            ok = drive(b, method, goal, fh, plans)
        except Switch:
            target = switch
            log_limit = instrument(b)                    # the rest of the run is unlimited
            ok = drive(b, method, switch, None, plans)
            for k in ("turns", "turn_deg", "arcs", "actions"):
                log[k] += log_limit[k]
        fin = math.hypot(b.r.x - target[0], b.r.y - target[1])
        success = bool(ok) and fin <= 0.15 and b.r.state != "Done"
    finally:
        if weights:
            planner.WEIGHTS.clear()
            planner.WEIGHTS.update(old)
    return {"scenario": name, "method": method, "success": success, "final_error_m": round(fin, 3),
            "path_m": round(b.r.stats["path_length"] - p0, 2), "sim_s": round(b.r.stats["sim_seconds"] - t0, 2),
            "plan_ms": round(sum(p["ms"] for p in plans), 1), "expanded": sum(p["expanded"] for p in plans),
            "plans": len(plans), "turns": log["turns"], "arcs": log["arcs"], "turn_deg": round(log["turn_deg"]),
            "direction_changes": log["turns"] + log["arcs"], "collisions": b.r.stats["collisions"],
            "min_clear_body_m": round(b.min_body, 3) if b.min_body < 9 else None,
            "min_clear_hand_m": round(b.min_app, 3) if b.min_app < 9 else None}


def table(rows, keys):
    head = "| " + " | ".join(keys) + " |\n|" + "---|" * len(keys) + "\n"
    return head + "".join("| " + " | ".join(str(r.get(k)) for k in keys) + " |\n" for r in rows)


def summary(rows, methods):
    out = []
    for m in methods:
        rs = [r for r in rows if r["method"] == m]
        ok = [r for r in rs if r["success"]]
        mean = lambda k, src=ok: round(sum(r[k] for r in src) / len(src), 2) if src else None  # noqa: E731
        out.append({"method": m, "success": f"{len(ok)}/{len(rs)}", "path_m": mean("path_m"), "sim_s": mean("sim_s"),
                    "plan_ms": mean("plan_ms", rs), "expanded": mean("expanded", rs), "turns": mean("turns"),
                    "arcs": mean("arcs"), "turn_deg": mean("turn_deg"), "direction_changes": mean("direction_changes"),
                    "collisions": sum(r["collisions"] for r in rs),
                    "min_clear_body_m": min((r["min_clear_body_m"] for r in rs if r["min_clear_body_m"] is not None), default=None),
                    "min_clear_hand_m": min((r["min_clear_hand_m"] for r in rs if r["min_clear_hand_m"] is not None), default=None)})
    return out


def main():
    methods = ("grid", "astar", "dijkstra", "astar_raw")
    rows = []
    for scn in SCENARIOS:
        for m in methods:
            r = run(scn, m)
            rows.append(r)
            print(r, flush=True)
    agg = summary(rows, methods)
    # weight sweep for the A* cost function (turn penalty x risk weight)
    sweep = []
    for tf in (0.0, 0.25, 1.0):
        for rk in (0.0, 2.0, 6.0):
            rs = [run(scn, "astar", {"turn_fixed": tf, "risk": rk}) for scn in SCENARIOS]
            s = summary(rs, ("astar",))[0]
            sweep.append({"turn_fixed": tf, "risk": rk, **{k: s[k] for k in ("success", "path_m", "sim_s", "turns",
                                                                              "turn_deg", "collisions", "min_clear_body_m",
                                                                              "min_clear_hand_m")}})
            print(sweep[-1], flush=True)
    keys = ["scenario", "method", "success", "path_m", "sim_s", "plan_ms", "expanded", "plans", "turns", "arcs",
            "turn_deg", "collisions", "min_clear_body_m", "min_clear_hand_m", "final_error_m"]
    md = ("# Navigation benchmark (measured)\n\nSame maps (built from the range scan), same physics (engine robot.py), "
          "same start/goal pairs. grid = the earlier 2-D Dijkstra planner with turn-move legs; astar / dijkstra = "
          "heading-aware lattice search + footprint-checked smoothing + arc corners; astar_raw = the lattice path "
          "driven cell by cell. Clearance is measured against the real geometry on every 1/60 s step.\n\n## Per run\n\n"
          + table(rows, keys) + "\n## Mean over successful runs\n\n"
          + table(agg, ["method", "success", "path_m", "sim_s", "plan_ms", "expanded", "turns", "arcs", "turn_deg",
                        "direction_changes", "collisions", "min_clear_body_m", "min_clear_hand_m"])
          + "\n## A* weight sweep (all scenarios)\n\n"
          + table(sweep, ["turn_fixed", "risk", "success", "path_m", "sim_s", "turns", "turn_deg", "collisions",
                          "min_clear_body_m", "min_clear_hand_m"]))
    d = ROOT / "outputs" / "eval"
    d.mkdir(parents=True, exist_ok=True)
    (d / "navigation.json").write_text(json.dumps({"runs": rows, "summary": agg, "sweep": sweep}, indent=2))
    (d / "navigation.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main()
