"""Coverage map, occlusion, next-best-view and mapping-during-tasks checks.

A-E run on the pure-Python robot with the same range scan /scan returns (fast,
deterministic). F-G run the real engine and the real planner over HTTP.
Ground truth is used only to CHECK the map, never to build it.

Run:  .venv\\Scripts\\python tests\\test_mapping.py
"""
import json
import math
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
from proc_util import kill_tree  # noqa: E402

sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT / "agent"))
import explore  # noqa: E402
from mapping import CELL, OCC, CoverageMap  # noqa: E402
from robot import Robot  # noqa: E402
from world import forward  # noqa: E402

fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({info})" if info and not cond else ""), flush=True)
    if not cond:
        fails.append(name)


def scan(r, n=91):
    """Exactly what the engine's /scan computes."""
    fx, fy = forward(r.heading)
    ox, oy = r.x + fx * r.r, r.y + fy * r.r
    return [r.world.ray(ox, oy, r.heading - 45 + 90 * i / (n - 1), ignore=r.held) for i in range(n)]


def run(gen):
    try:
        while True:
            next(gen)
    except StopIteration as s:
        return s.value


class SimBody:
    """Minimal robot-side body for explore.py on the pure-Python robot."""
    def __init__(self, level="level1", seed=0):
        self.r = Robot(level, seed)
        self.r.set_command("explore", "test")
        self.r.interactive = True
        self.cmap = CoverageMap()
        self.views = 0
        body = self

        class Cl:
            def scan(self, n=61):
                return scan(body.r, n)

            def turn(self, deg, speed=1.0):
                run(body.r.turn(deg, speed))
                return {"heading": round(body.r.heading, 1)}

            def move(self, d, speed=1.0):
                return run(body.r.move(d, speed))

            def arc(self, rad, deg, speed=1.0):
                return run(body.r.arc(rad, deg, speed))
        self.cl = Cl()

    def st(self):
        self.s = self.r.public_state()
        return self.s

    def see(self):
        s = self.st()
        self.views += 1
        self.cmap.integrate(s["x"], s["y"], s["heading"], scan(self.r))
        return []

    def turn(self, deg, speed=1.0):
        deg = (deg + 180) % 360 - 180
        while abs(deg) > 0.5:
            step = max(-180, min(180, deg))
            run(self.r.turn(step))
            deg -= step

    def face_point(self, p):
        s = self.st()
        self.turn(math.degrees(math.atan2(-(p[1] - s["y"]), p[0] - s["x"])) - s["heading"])

    def move(self, d, speed=1.0):
        return run(self.r.move(d))

    def say(self, text):
        pass

    def publish(self):
        pass


def gt_floor_mask(world, m):
    xs = -4.0 + (np.arange(m.occ.shape[0]) + 0.5) * CELL
    X, Y = np.meshgrid(xs, xs)
    inside = (np.abs(X) < world.half) & (np.abs(Y) < world.half)
    floor = inside.copy()
    for o in world.objects:
        floor &= ~np.vectorize(lambda a, b, o=o: o.dist_to_point(a, b) <= 0.0)(X, Y)
    return X, Y, inside, floor


def pure_tests():
    # A. repeated viewpoints are recognised as redundant
    b = SimBody()
    b.see()
    s1 = b.cmap.history[-1]
    b.see()
    b.see()
    s2, s3 = b.cmap.history[-2], b.cmap.history[-1]
    check("A first view adds new floor", s1["new_m2"] > 3.0, s1)
    check("A same pose again adds nothing new", s2["new_m2"] == 0 and s3["new_m2"] == 0, (s2, s3))
    check("A same pose again is counted as 100% overlap", s2["overlap"] == 1.0 and s3["overlap"] == 1.0)
    check("A redundancy statistic reflects the repeats", b.cmap.stats()["redundant"] > 0.6, b.cmap.stats()["redundant"])

    # B. new orientations add the right regions, and the map agrees with the real room
    b = SimBody()
    b.r.x, b.r.y = 0.0, 0.3
    added = []
    for k in range(4):
        b.see()
        added.append(b.cmap.history[-1]["new_m2"])
        b.turn(90)
    check("B every quarter turn reveals new floor", all(a > 1.0 for a in added), added)
    X, Y, inside, floor = gt_floor_mask(b.r.world, b.cmap)
    seen = b.cmap.cov > 0
    check("B camera-seen cells are real floor (>= 99%)", (seen & floor).sum() / seen.sum() >= 0.99,
          round(float((seen & floor).sum() / seen.sum()), 4))
    occ = np.argwhere(b.cmap.occ == OCC)
    surf = []
    for j, i in occ:
        x, y = -4.0 + (i + 0.5) * CELL, -4.0 + (j + 0.5) * CELL
        d_wall = abs(b.r.world.half - max(abs(x), abs(y)))
        d_obj = min(o.dist_to_point(x, y) for o in b.r.world.objects)
        surf.append(min(d_wall, abs(d_obj)))
    check("B occupied cells lie on real surfaces (within 0.1 m)", max(surf) <= 0.1 + 1e-6, max(surf))

    # C. occlusion: the floor behind an object is not marked as seen
    b = SimBody()
    cube = next(o for o in b.r.world.objects if o.id == "o1")
    b.r.x, b.r.y, b.r.heading = cube.x - 1.0, cube.y, 0.0
    b.see()
    m = b.cmap
    behind = [(cube.x + cube.half + d, cube.y + dy) for d in (0.3, 0.6, 1.0) for dy in (-0.05, 0.05)]
    beside = [(cube.x + 0.6, cube.y + 0.75), (cube.x + 0.6, cube.y - 0.75)]
    jb = [m.idx(x, y) for x, y in behind]
    js = [m.idx(x, y) for x, y in beside]
    check("C floor right behind the cube is not seen", all(m.cov[j, i] == 0 for j, i in jb))
    check("C ... and is marked occluded", all(m.occl[j, i] for j, i in jb))
    check("C floor beside the cube is seen", all(m.cov[j, i] > 0 for j, i in js))
    check("C cube front face is occupied", m.occ[m.idx(cube.x - cube.half - 0.03, cube.y)] == OCC)

    # D. next-best-view: honest predictions, and less redundancy than the sweep it replaces
    b = SimBody()
    b.see()
    ratios = []
    for _ in range(12):
        s = b.st()
        walls = sum(v is not None for v in b.cmap.bounds.values())
        best = b.cmap.next_best_view(s["x"], s["y"], s["heading"])
        if best is None:
            break
        before = b.cmap.stats()["observed_m2"]
        explore.follow(b, best["route"], best["heading"], observe_legs=False)
        b.see()
        if walls == 4:
            ratios.append((b.cmap.stats()["observed_m2"] - before) / best["gain_m2"])
    check("D scored many candidates (positions x headings)", best is None or best["candidates"] >= 100)
    check("D once the walls are known, realised gain >= 50% of predicted (mean)",
          ratios and sum(ratios) / len(ratios) >= 0.5, [round(r, 2) for r in ratios])
    n, w = SimBody(), SimBody()
    explore.explore_nbv(n, goal=0.95, max_views=40)
    explore.explore_sweep(w, goal=0.95, max_views=40)
    sn, sw = n.cmap.stats(), w.cmap.stats()
    check("D NBV needs fewer viewpoints than the 45-degree sweep", sn["observations"] < sw["observations"],
          (sn["observations"], sw["observations"]))
    check("D NBV repeats less of what it already saw", sn["redundant"] < sw["redundant"], (sn["redundant"], sw["redundant"]))
    check("D NBV wastes fewer turns (turns that revealed < 0.1 m^2)", sn["wasted_turns"] <= sw["wasted_turns"],
          (sn["wasted_turns"], sw["wasted_turns"]))

    # E. full-room exploration reaches the coverage goal and stops by itself
    for level in ("level1", "level2"):
        b = SimBody(level)
        why = explore.explore_nbv(b, goal=0.95, max_views=40)
        X, Y, inside, floor = gt_floor_mask(b.r.world, b.cmap)
        gt = float(((b.cmap.cov > 0) & floor).sum() / floor.sum())
        check(f"E {level}: stops on its own ({why})", why in ("coverage", "no_gain"), why)
        check(f"E {level}: covers >= 90% of the real floor", gt >= 0.9, round(gt, 3))
        check(f"E {level}: no collisions", b.r.stats["collisions"] == 0, b.r.stats["collisions"])
        check(f"E {level}: estimate close to truth (within 6 points)", abs(b.cmap.stats()["coverage_est"] - gt) <= 0.06,
              (b.cmap.stats()["coverage_est"], round(gt, 3)))
    # persistence: the map survives a save/load (next task continues from it)
    p = ROOT / "outputs" / "test_runs" / "map_test.npz"
    p.parent.mkdir(parents=True, exist_ok=True)
    b.cmap.save(str(p))
    m2 = CoverageMap.load(str(p))
    check("E map saved and reloaded unchanged", (m2.cov == b.cmap.cov).all() and m2.n_obs == b.cmap.n_obs)


# ------------------------------------------------------------ engine (F, G)
AP, OP = 8195, 8196
ADMIN = f"http://127.0.0.1:{OP}"


def http(method, path, body=None):
    req = urllib.request.Request(ADMIN + path, data=json.dumps(body).encode() if body is not None else None,
                                 method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def wait_task(tid, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = http("GET", "/admin/status")
        t = next((h for h in s["history"] if h["id"] == tid), None)
        if t and t["status"] not in ("Starting", "Running", "Paused"):
            return t
        time.sleep(0.3)


def engine_tests():
    runs = ROOT / "outputs" / "test_runs"
    eng = subprocess.Popen([sys.executable, str(ROOT / "engine" / "sim.py"), "--headless", "--port", str(AP),
                            "--admin-port", str(OP), "--brain", "local"], cwd=str(ROOT),
                           env={**os.environ, "FETCH_RUNS_DIR": str(runs)},
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(120):
            try:
                http("GET", "/admin/status")
                break
            except OSError:
                time.sleep(0.25)
        req = urllib.request.Request(f"http://127.0.0.1:{AP}/scan?n=31")
        with urllib.request.urlopen(req) as r:
            sc = json.loads(r.read())
        check("/scan returns ranges only (31 rays, fov 90)", set(sc) == {"ranges", "fov"} and len(sc["ranges"]) == 31)

        # G. mapping during a normal fetch: built from the task's own looks, no extra actions
        http("POST", "/admin/reset", {"level": "level1", "seed": 0})
        tid = http("POST", "/admin/instruct", {"text": "Bring the red cube to the green square."})["task_id"]
        t = wait_task(tid)
        mp = http("GET", "/admin/map")
        st = mp["map"]["stats"] if mp["map"] else {}
        acts = [json.loads(l) for l in (runs / tid / "actions.jsonl").read_text().splitlines()]
        tools = {a["tool"] for a in acts}
        check("G fetch still completes with mapping on", t and t["status"] == "Completed", t and t["result"])
        check("G map was built from the fetch's own observations", st.get("observations", 0) >= 3 and st.get("observed_m2", 0) > 8, st)
        check("G no separate mapping actions (only move/turn/pickup/drop)", tools <= {"move", "turn", "pickup", "drop"}, tools)
        check("G operator ground-truth check agrees (>= 99% of seen cells are real floor)",
              mp["gt"]["seen_inside_objects_m2"] <= 0.05 * max(st.get("observed_m2", 1), 1), mp["gt"])
        n_before = st.get("observations", 0)

        # F. task-specific search reuses the map and does not explore the whole room
        tid = http("POST", "/admin/instruct", {"text": "Find the blue sphere."})["task_id"]
        t = wait_task(tid)
        mp = http("GET", "/admin/map")
        st2 = mp["map"]["stats"]
        check("F find completes", t and t["status"] == "Completed", t and t["result"])
        check("F the map carried over from the previous task", st2["observations"] > n_before, (n_before, st2["observations"]))
        check("F search stopped once found (<= 6 new views)", st2["observations"] - n_before <= 6, st2["observations"] - n_before)

        tid = http("POST", "/admin/instruct", {"text": "Explore the entire room and map everything you can see."})["task_id"]
        t = wait_task(tid, 180)
        mp = http("GET", "/admin/map")
        check("E (live) explore command completes", t and t["status"] == "Completed", t and t["result"])
        check("E (live) operator check: >= 90% of the real floor seen", mp["gt"]["coverage"] >= 0.9, mp["gt"])
        http("POST", "/admin/reset", {"level": "level1", "seed": 0})
        check("reset clears the robot's map", http("GET", "/admin/map")["map"] is None)
    finally:
        kill_tree(eng)


def main():
    pure_tests()
    engine_tests()
    print(f"\n{len(fails)} failed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
