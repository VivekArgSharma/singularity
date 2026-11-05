"""Circular motion, smoothness and cancellation, step by step on the pure-Python robot.

Run:  .venv\\Scripts\\python tests\\test_arc.py
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
from robot import DT, GRIP_STOWED, STOW_AHEAD, STOW_SIDE, Robot  # noqa: E402
from world import forward  # noqa: E402

fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({info})" if info and not cond else ""))
    if not cond:
        fails.append(name)


def fresh():
    r = Robot("level1", 0)
    r.set_command("test", "arc")
    return r


def place_tangent(r, cx, cy, radius, clockwise=True):
    """Put the robot on a circle round (cx, cy), heading tangent, centre on its right (cw)."""
    r.x, r.y = cx - radius, cy
    r.heading = -90.0 if clockwise else 90.0     # facing +y (cw: centre at +x = right) / -y (ccw)
    rx, ry = forward(r.heading + 90.0)
    side = 1 if clockwise else -1
    assert abs(r.x + side * rx * radius - cx) < 1e-9 and abs(r.y + side * ry * radius - cy) < 1e-9


def run(gen, r, cb=None):
    samples = []
    try:
        while True:
            next(gen)
            samples.append((r.x, r.y, r.heading))
            if cb:
                cb(len(samples))
    except StopIteration as s:
        return s.value, samples


def unwrap_angles(samples, cx, cy):
    total, prev = 0.0, math.atan2(samples[0][1] - cy, samples[0][0] - cx)
    for x, y, _ in samples[1:]:
        a = math.atan2(y - cy, x - cx)
        d = (a - prev + math.pi) % (2 * math.pi) - math.pi
        total += d
        prev = a
    return math.degrees(total)


def main():
    # 1. one loop round the blue sphere (o4 at 0.4, 1.6), radius 0.65
    r = fresh()
    o4 = next(o for o in r.world.objects if o.id == "o4")
    R = 0.65
    place_tangent(r, o4.x, o4.y, R)
    x0, y0, h0 = r.x, r.y, r.heading
    res, s = run(r.arc(R, 360), r)
    radii = [math.hypot(x - o4.x, y - o4.y) for x, y, _ in s]
    check("circle once: no collision", res["collided"] is False, res)
    check("circle once: radius constant to 1 mm", max(abs(d - R) for d in radii) < 1e-3,
          f"{min(radii):.4f}..{max(radii):.4f}")
    check("circle once: returns to the start pose", math.hypot(r.x - x0, r.y - y0) < 1e-3
          and abs(((r.heading - h0 + 180) % 360) - 180) < 0.1, (r.x, r.y, r.heading))
    check("circle once: 360 deg swept round the centre", abs(abs(unwrap_angles([(x0, y0, h0)] + s, o4.x, o4.y)) - 360) < 0.5)
    v = min(1.0, R * math.radians(90))
    check("circle once: speed 1 m/s capped by 90 deg/s", abs(len(s) * DT - 2 * math.pi * R / v) < 2 * DT,
          f"{len(s) * DT:.3f} s vs {2 * math.pi * R / v:.3f} s")
    check("circle once: path length = 2 pi r", abs(res["moved"] - 2 * math.pi * R) < 0.01, res)

    # 2. heading is tangent at every step (forward vector perpendicular to the radius)
    worst = 0.0
    for x, y, h in s:
        fx, fy = forward(h)
        worst = max(worst, abs(fx * (x - o4.x) + fy * (y - o4.y)) / R)
    check("heading tangent to the circle on every step", worst < 0.01, worst)

    # 3. smooth: equal steps, no in-place spins, no jumps
    steps = [math.hypot(s[i + 1][0] - s[i][0], s[i + 1][1] - s[i][1]) for i in range(len(s) - 1)]
    dh = [abs(((s[i + 1][2] - s[i][2] + 180) % 360) - 180) for i in range(len(s) - 1)]
    check("every step moves the same distance (no stop-start)", max(steps) - min(steps[:-1]) < 1e-6,
          f"{min(steps):.5f}..{max(steps):.5f}")
    check("never turns in place during the circle", min(steps[:-1]) > 0.01)
    check("heading rate never above 90 deg/s", max(dh) <= 90 * DT + 1e-6, max(dh) / DT)

    # 4. two loops: 720 deg, still on the circle
    r = fresh()
    place_tangent(r, o4.x, o4.y, R)
    x0, y0 = r.x, r.y
    res, s = run(r.arc(R, 720), r)
    radii = [math.hypot(x - o4.x, y - o4.y) for x, y, _ in s]
    swept = unwrap_angles([(x0, y0, 0)] + s, o4.x, o4.y)
    check("circle twice: 720 deg swept", abs(abs(swept) - 720) < 0.5, swept)
    check("circle twice: radius constant to 1 mm", max(abs(d - R) for d in radii) < 1e-3)
    check("circle twice: no collision", res["collided"] is False)

    # 5. anticlockwise (negative degrees, centre on the left)
    r = fresh()
    place_tangent(r, o4.x, o4.y, R, clockwise=False)
    res, s = run(r.arc(R, -360), r)
    radii = [math.hypot(x - o4.x, y - o4.y) for x, y, _ in s]
    check("anticlockwise loop: radius constant", max(abs(d - R) for d in radii) < 1e-3)

    # 6. something on the circle: stop, back off, Blocked, retries +1
    r = fresh()
    place_tangent(r, o4.x, o4.y, 0.75)
    o2 = next(o for o in r.world.objects if o.id == "o2")
    o2.x, o2.y = o4.x + 0.75, o4.y                   # sitting on the far side of the circle
    res, _ = run(r.arc(0.75, 360), r)
    check("arc into an object: collided", res["collided"] is True, res)
    check("arc into an object: Blocked, retries 1", r.state == "Blocked" and r.retries == 1, (r.state, r.retries))
    check("arc into an object: robot left clear", not r._pose_blocked(r.x, r.y, r.heading))

    # 7. cancellation at the next step: move, turn, arc
    for name, make in (("move", lambda r: r.move(3.0)), ("turn", lambda r: r.turn(170)),
                       ("arc", lambda r: r.arc(0.6, 360))):
        r = fresh()
        if name == "arc":
            place_tangent(r, o4.x, o4.y, 0.6)

        def cancel_at(n, r=r):
            if n == 30:
                r.cancel = True
        res, s = run(make(r), r, cancel_at)
        check(f"{name}: cancel stops within one step", len(s) <= 31, len(s))
        check(f"{name}: cancel reports interrupted, keeps the pose", res.get("interrupted") is True
              and (r.x, r.y, r.heading) == s[-1], res)

    # 8. cancel a pickup before the grasp: hand folds back, nothing held, no object moved
    r = fresh()
    o = next(x for x in r.world.objects if x.id == "o6")
    r.x, r.y, r.heading = o.x - (0.2 + 0.12 + o.r), o.y, 0.0
    before = (o.x, o.y)

    def cancel_pick(n, r=r):
        if n == 20:
            r.cancel = True
    res, _ = run(r.pickup(), r, cancel_pick)
    check("pickup cancelled before grasp: ok false, interrupted", res == {"ok": False, "interrupted": True}, res)
    check("pickup cancelled: nothing held, object not moved", r.held is None and (o.x, o.y) == before)
    check("pickup cancelled: hand folded back", abs(r.hand[0] - STOW_SIDE) < 1e-6 and abs(r.hand[1] - STOW_AHEAD) < 1e-6
          and abs(r.grip - GRIP_STOWED) < 1e-6 and r.hand_z == 0.0)

    # 9. cancel a drop before release: object stays in the hand
    r = fresh()
    o = next(x for x in r.world.objects if x.id == "o6")
    r.x, r.y, r.heading = o.x - (0.2 + 0.12 + o.r), o.y, 0.0
    run(r.pickup(), r)
    res, _ = run(r.drop(), r, lambda n, r=r: setattr(r, "cancel", True) if n == 2 else None)
    check("drop cancelled before release: still holding", r.held is o and res.get("interrupted") is True, res)
    check("drop cancelled: state back to Carry", r.state == "Carry", r.state)

    # 10. straight move is continuous at exactly 1 m/s
    r = fresh()
    r.heading = 90.0                                 # towards -y: open floor
    res, s = run(r.move(1.0), r)
    steps = [math.hypot(s[i + 1][0] - s[i][0], s[i + 1][1] - s[i][1]) for i in range(len(s) - 1)]
    check("move: 60 equal steps of 1/60 m", len(s) == 60 and max(abs(d - DT) for d in steps) < 1e-9, len(s))

    # 11. slow speed factor
    r = fresh()
    r.heading = 90.0
    res, s = run(r.move(0.5, speed=0.25), r)
    check("move slowly (0.25): 4x the steps, same distance", len(s) == 120 and abs(res["moved"] - 0.5) < 1e-9, len(s))

    print(f"\n{len(fails)} failed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
