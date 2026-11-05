"""Missions (Brain Puzzle, Colour Sorting, Maze): config validation, seeded scenes, the elevation physics
(ramp/platform), private evaluators, and that nothing private reaches the agent port.

    .venv\\Scripts\\python tests\\test_missions.py
"""
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))

import missions as M  # noqa: E402
from robot import Robot  # noqa: E402
from world import forward  # noqa: E402

failed = 0


def check(name, ok, info=""):
    global failed
    print(("PASS " if ok else "FAIL ") + name + (f"  ({info})" if info and not ok else ""))
    failed += 0 if ok else 1


def run(gen):
    try:
        while True:
            next(gen)
    except StopIteration as s:
        return s.value


def mission_robot(name, seed=0, config=None):
    m = M.create(name, seed, config)
    cfg = m.scene()
    r = Robot()
    r.reset(name, seed, cfg=cfg, mission=m)
    r.set_command(m.instruction, "t")
    return r, m


def face(r, x, y):
    h = math.degrees(math.atan2(-(y - r.y), x - r.x))
    run(r.turn(((h - r.heading) + 180) % 360 - 180))


# ------------------------------------------------------------------ config + determinism
for name in M.REGISTRY:
    a, b = M.create(name, 3).scene(), M.create(name, 3).scene()
    check(f"{name}: same seed -> identical scene", json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True))
    c = M.create(name, 4).scene()
    check(f"{name}: different seed -> different scene", json.dumps(a, sort_keys=True) != json.dumps(c, sort_keys=True))
for name, bad in (("brain_puzzle", {"platform_height": 0.9}), ("brain_puzzle", {"ramp_length": 0.4}),
                  ("colour_sort", {"boxes": {"purple": 1}}), ("maze", {"cells": 9}), ("maze", {"wall_height": 0.3})):
    try:
        M.create(name, 0, bad)
        ok = False
    except M.ConfigError:
        ok = True
    check(f"{name}: impossible config {bad} rejected", ok)
try:
    M.create("pinball")
    ok = False
except M.ConfigError:
    ok = True
check("unknown mission rejected", ok)

# the fetch levels still shuffle with the seed (regression: a mission change once switched this off)
from world import World  # noqa: E402
for lv in ("level1", "obstacles"):
    p0 = [(o.x, o.y) for o in World(lv, 0).objects]
    p1 = [(o.x, o.y) for o in World(lv, 1).objects]
    check(f"{lv}: seed 1 moves the objects (seed 0 = the file layout)", p0 != p1)
    w1 = World(lv, 1)
    check(f"{lv}: seeded objects stay clear of obstacles", all(ob.dist_to_point(o.x, o.y) >= 0.5 for o in w1.objects
                                                               for ob in w1.obstacles))

# ------------------------------------------------------------------ Brain Puzzle physics
r, m = mission_robot("brain_puzzle", 0)
w = r.world
plat = w.platforms[0]
cube = next(o for o in w.objects if o.id == "cube")
ramp = next(o for o in w.objects if o.id == "ramp")
px, py, ph = plat.x, plat.y, plat.top
check("cube starts on the platform top", abs(cube.z - ph) < 1e-9 and plat.dist_to_point(cube.x, cube.y) == 0)

# drive straight at the platform's west side: the riser stops the wheels
r.x, r.y, r.heading = px - 1.0, py, 0.0
res = run(r.move(1.0))
check("cannot drive up the platform edge (no ramp)", res["collided"] and r.z == 0.0, res)
r.state, r.retries = "Locate", 0
# from the floor the hand cannot reach the elevated cube
r.x, r.y = px - plat.hx - 0.25, py
face(r, cube.x, cube.y)
res = run(r.pickup())
check("elevated cube not reachable from the floor", res["ok"] is False and r.held is None)

# place the ramp (test only) against the west edge, rising east, 5 cm gap (< 15 cm lip)
gap = 0.05
ramp.yaw, ramp.x, ramp.y = 0.0, px - plat.hx - gap - ramp.hx, py
check("support: ramp low end is floor level, top near platform height",
      w.support_height(ramp.x - ramp.hx + 0.01, py) < 0.02 and abs(w.support_height(ramp.x + ramp.hx - 0.01, py) - ph) < 0.02)
r.x, r.y, r.heading = ramp.x - ramp.hx - 0.4, py, 0.0
res = run(r.move(ramp.sx + 0.4 + gap + 0.12))
check("drives up a correctly placed ramp onto the platform", abs(r.z - ph) < 1e-6 and not res["collided"],
      f"z={r.z:.3f} {res}")
# side entry onto the ramp's high part is a wall
r2, _ = mission_robot("brain_puzzle", 0)
rp2 = next(o for o in r2.world.objects if o.id == "ramp")
rp2.yaw, rp2.x, rp2.y = 0.0, ramp.x, ramp.y
r2.x, r2.y, r2.heading = rp2.x + 0.3, py + rp2.hy + 0.45, 90.0       # beside the high part, facing it (-y)
res = run(r2.move(0.5))
check("cannot climb onto the ramp from the side where it is high", res["collided"], res)

face(r, cube.x, cube.y)
res = run(r.pickup())
check("on the platform the cube is in reach and picked up", res["ok"] and r.held is cube)
run(r.turn(180))
z_top = r.z
res = run(r.move(ramp.sx + 0.3))
check("drives back down the ramp carrying the cube", r.z == 0.0 and z_top > 0.25 and not res["collided"],
      f"z={r.z:.3f} {res}")
r.after_action()
zx, zy = w.zone_c
face(r, zx, zy)
run(r.move(math.dist((r.x, r.y), (zx, zy)) - 0.6))
res = run(r.drop())
check("delivery without carrying the ramp there is rejected (ramp was placed by hand)",
      r.outcome == "fail_invalid_route" and m.outcome == "fail_invalid_route", (r.outcome, m.events))

# a ramp left too far from the edge (gap > lip): the top ends at a drop, the robot stops before it
r3, _ = mission_robot("brain_puzzle", 0)
rp3 = next(o for o in r3.world.objects if o.id == "ramp")
pl3 = r3.world.platforms[0]
rp3.yaw, rp3.x, rp3.y = 0.0, pl3.x - pl3.hx - 0.30 - rp3.hx, pl3.y
r3.x, r3.y, r3.heading = rp3.x - rp3.hx - 0.4, pl3.y, 0.0
res = run(r3.move(2.0))
check("misplaced ramp (30 cm gap > 15 cm lip): stops at the end of the lip, never reaches the platform",
      res["collided"] and pl3.dist_to_point(r3.x, r3.y) > 0.1 and r3.z > 0.2, f"z={r3.z:.3f}")

# carrying the real ramp: picked up, carried, dropped where it is a valid placement -> stair_placed
r4, m4 = mission_robot("brain_puzzle", 1)
rp4 = next(o for o in r4.world.objects if o.id == "ramp")
pl4 = r4.world.platforms[0]
# stand facing the ramp's long side from the south-west so the claws close across its width
rp4.yaw = 0.0
r4.x, r4.y, r4.heading = rp4.x, rp4.y + rp4.hy + 0.45, 90.0
face(r4, rp4.x, rp4.y)
res = run(r4.pickup())
check("the ramp can be picked up with the claws", res["ok"] and r4.held is rp4, res)
r4.after_action()
check("evaluator saw the ramp being carried", "stair_carried" in {e["event"] for e in m4.events})

# ------------------------------------------------------------------ Colour Sorting evaluator
r, m = mission_robot("colour_sort", 2)
w = r.world
cols = sorted(o.color_name for o in w.objects)
check("colour sort: five boxes, one red/blue/green, two yellow", cols == ["blue", "green", "red", "yellow", "yellow"], cols)
check("colour sort: boxes do not overlap and avoid the squares",
      all(math.dist((a.x, a.y), (b.x, b.y)) >= 0.79 for a in w.objects for b in w.objects if a is not b)
      and all(w.zone_at(o.x, o.y) is None for o in w.objects))
for o in list(w.objects):                                      # deliver each by hand: drop into the right square
    z = next(zz for zz in w.zones if zz["color_name"] == o.color_name)
    r.held, o.held = o, True
    r.x, r.y, r.heading = z["center"][0], z["center"][1], 0.0
    r.hand = [0.0, 0.0]
    out = run(r.drop())
check("colour sort: all five (both yellow) in matching squares -> pass", r.outcome == "pass" and m.status()["delivered"] == 5,
      (r.outcome, m.status()))
r, m = mission_robot("colour_sort", 2)
o = r.world.objects[0]
wrong = next(zz for zz in r.world.zones if zz["color_name"] != o.color_name)
r.held, o.held = o, True
r.x, r.y, r.hand = wrong["center"][0], wrong["center"][1], [0.0, 0.0]
run(r.drop())
check("colour sort: a box in the wrong square is not counted", m.status()["delivered"] == 0 and m.status()["wrong_drops"] == 1
      and r.outcome is None)

# ------------------------------------------------------------------ Maze
r, m = mission_robot("maze", 0)
w = r.world
check("maze: connected, exit on the border, start != exit", m.route_len >= 3 and m.exit_cell != m.start_cell)
check("maze: walls taller than the camera", all(o.h > 0.5 for o in w.obstacles))
fx, fy = forward(r.heading)
check("maze: the robot starts facing an open corridor", (r.depth() or 0) > 0.6, r.depth())
# drive the true shortest route (test only, ground truth) and check the evaluator fires on the exit tile
cells = {m.start_cell: None}
q = [m.start_cell]
while q:
    a = q.pop(0)
    for e in m.open_edges:
        if a in e:
            b = next(iter(e - {a}))
            if b not in cells:
                cells[b] = a
                q.append(b)
path = [m.exit_cell]
while cells[path[-1]] is not None:
    path.append(cells[path[-1]])
path.reverse()
half, cell = 3.0, m.cell
for c in path[1:]:
    x, y = -half + cell * (c[0] + 0.5), -half + cell * (c[1] + 0.5)
    face(r, x, y)
    res = run(r.move(math.dist((r.x, r.y), (x, y))))
    r.after_action()
    if res["collided"]:
        break
check("maze: following the open corridors reaches the exit -> pass", r.outcome == "pass", (r.outcome, path))
r, m = mission_robot("maze", 0)
for _ in range(3):
    run(r.move(5.0))
    r.state = "Locate" if r.state == "Blocked" else r.state
check("maze: walls block (drive into one)", r.stats["collisions"] >= 1)

# ------------------------------------------------------------------ nothing private on the agent port
r, m = mission_robot("brain_puzzle", 0)
pub = json.dumps(r.public_state())
check("agent /state: only the PS keys + z (no mission, ids, coordinates of objects)",
      set(json.loads(pub)) == {"name", "x", "y", "heading", "holding", "retries", "z"}, pub)
r, m = mission_robot("maze", 0)
check("maze: agent /state has no exit or map", set(r.public_state()) == {"name", "x", "y", "heading", "holding", "retries"})

print(f"\n{failed} failed")
sys.exit(1 if failed else 0)
