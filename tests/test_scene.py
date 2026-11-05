"""Scene matches the agreed spec, and a carried object is visible in the robot camera.

Run:  .venv\\Scripts\\python tests\\test_scene.py
"""
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({info})" if info and not cond else ""))
    if not cond:
        fails.append(name)


def main():
    from sim import Engine
    e = Engine(headless=True)
    w, r = e.robot.world, e.robot
    check("room 6 x 6 m", abs(w.half * 2 - 6.0) < 1e-9, w.half)
    check("green zone 1.2 m at (2.0, 1.8)", tuple(w.zone_c) == (2.0, 1.8) and abs(w.zone_size - 1.2) < 1e-9,
          (w.zone_c, w.zone_size))
    check("robot starts at (-2.4, 0) heading 0, radius 0.2", (r.x, r.y, r.heading, r.r) == (-2.4, 0.0, 0.0, 0.2),
          (r.x, r.y, r.heading, r.r))
    want = {("red", "cube"), ("red", "sphere"), ("blue", "cube"), ("blue", "sphere"), ("yellow", "cylinder"), ("orange", "cone")}
    have = {(o.color_name, o.shape) for o in w.objects}
    check("six objects: red/blue cube, red/blue sphere, yellow cylinder, orange cone", have == want and len(w.objects) == 6, have)
    can = next(o for o in w.objects if o.shape == "cylinder")
    check("the cylinder has a lid ring (drink can)", bool(can.lid))
    check("cylinder is the tallest object", all(can.h >= o.h for o in w.objects))
    import json
    lvl = json.loads((Path(__file__).resolve().parents[1] / "engine" / "levels" / "level1.json").read_text())
    check("ceiling and walls in the level (1.2 m walls)", lvl["room"].get("ceiling") is True and lvl["room"]["wall_height"] > 0.5)
    ceil = e.views.scene.find("**/ceiling")
    check("ceiling node built in the scene", not ceil.isEmpty())

    # carried object visible at the bottom(-right) of the robot camera
    cube = next(o for o in w.objects if o.id == "o1")
    r.set_command("test", "scene")
    r.x, r.y = cube.x - (r.r + 0.12 + cube.r), cube.y
    r.heading = 0.0
    gen = r.pickup()
    try:
        while True:
            next(gen)
    except StopIteration as s:
        ok = s.value
    check("pickup in reach succeeds", ok == {"ok": True}, ok)
    e.views.sync(r)
    img = e.views.grab("look").astype(int)
    red = (img[..., 0] > 120) & (img[..., 1] < 70) & (img[..., 2] < 70)
    h, wd = red.shape
    bottom_right = red[h // 2:, wd // 2:].mean()
    centre = red[h // 4: 3 * h // 4, wd // 4: 3 * wd // 4].mean()
    check("carried cube visible in the bottom-right of the robot camera", bottom_right > 0.05, round(float(bottom_right), 3))
    check("... and the centre of the view stays clear", centre < bottom_right, (round(float(centre), 3), round(float(bottom_right), 3)))
    print(f"\n{len(fails)} failed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
