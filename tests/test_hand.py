"""Frame-by-frame check that the gripper never passes through objects.

Runs the pure-Python robot (no rendering). For each of the six level1 objects it
places the robot in front of it, runs pickup and drop one 1/60 s step at a time,
and on every step samples the claws and the palm: no sample point may lie inside
an object that the hand is not legitimately touching. It also checks the robot
body never overlaps an object and that the dropped object is left clear.

Run:  .venv\\Scripts\\python tests\\test_hand.py
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
from robot import Robot  # noqa: E402

CLAW_W, CLAW_L, CLAW_H = 0.026, 0.18, 0.34
PALM_D, PALM_H = 0.03, 0.14
fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({info})" if info and not cond else ""))
    if not cond:
        fails.append(name)


def hand_boxes(r):
    """Claw and palm boxes in the robot frame: (cx, cy, half_w, half_l, z0, z1)."""
    hx, hy = r.hand
    g, z = r.grip, r.hand_z
    depth = r._half_width(r.held) if r.held is not None else min(max(g - 0.006, 0.05), 0.22, max(0.05, hy - 0.30))
    boxes = []
    for side in (-1, 1):
        boxes.append((hx + side * (g + 0.013), hy, CLAW_W / 2, CLAW_L / 2, z + 0.02, z + 0.02 + CLAW_H))
    boxes.append((hx, hy - depth - 0.03, (2 * g + 0.05) / 2, PALM_D / 2, z + 0.02, z + 0.02 + PALM_H))
    return boxes


def penetrations(r, skip=None):
    """Sample each hand box; report objects whose footprint contains a sample at an
    overlapping height. `skip` = an object the claws may touch (the held one)."""
    hits = []
    for (cx, cy, hw, hl, z0, z1) in hand_boxes(r):
        pts = [(cx + hw * a * 0.95, cy + hl * b * 0.95) for a in (-1, -0.5, 0, 0.5, 1) for b in (-1, -0.5, 0, 0.5, 1)]
        for o in r.world.objects:
            if o is skip:
                continue
            o_z0 = r.hand_z if o.held else 0.0
            if z1 <= o_z0 or z0 >= o_z0 + o.h:
                continue
            for lx, ly in pts:
                wx, wy = r.local_to_world(lx, ly)
                if o.dist_to_point(wx, wy) <= 0.0:
                    hits.append(o.id)
                    break
    return hits


def run(gen, r, label, skip_fn):
    worst = []
    try:
        while True:
            next(gen)
            hits = penetrations(r, skip_fn())
            body = [o.id for o in r.world.solids(r.held) if o.dist_to_point(r.x, r.y) < r.r - 1e-6]
            if hits or body:
                worst.append((hits, body))
    except StopIteration as s:
        return s.value, worst


def main():
    base = Robot("level1", 0)
    for target in base.world.objects:
        r = Robot("level1", 0)
        r.set_command("test", "hand")
        o = next(x for x in r.world.objects if x.id == target.id)
        # stand 0.12 m in front of the object's surface, facing it, approaching from the room centre
        ang = math.atan2(o.y, o.x) if (o.x or o.y) else 0.0
        dist = r.r + 0.12 + o.r
        r.x, r.y = o.x - math.cos(ang) * -dist, o.y - math.sin(ang) * -dist
        r.heading = math.degrees(math.atan2(-(o.y - r.y), o.x - r.x))
        res, bad = run(r.pickup(), r, "pickup", lambda: r.held)
        check(f"{o.id} {o.color_name} {o.shape}: pickup ok", res == {"ok": True}, res)
        check(f"{o.id}: no claw/palm inside any object during pickup", not bad, bad[:3])
        if r.held is None:
            continue
        res, bad = run(r.drop(), r, "drop", lambda: r.held)
        check(f"{o.id}: drop ok", res.get("ok") is True, res)
        check(f"{o.id}: no claw/palm inside any object during drop", not bad, bad[:3])
        check(f"{o.id}: hand left the object clear", not penetrations(r), penetrations(r))
    print(f"\n{len(fails)} failed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
