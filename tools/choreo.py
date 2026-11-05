"""Scripted multi-step showcase for the engine (operator tool, NOT the agent).

It reads object positions from the level file and plans paths itself, so it must
never be used as, or mixed into, the Claude agent: in a real run the agent knows
nothing about object positions and works from the camera alone.

Needs the engine in sandbox mode:
    .venv\\Scripts\\python engine\\sim.py --sandbox --debug-frames
    .venv\\Scripts\\python tools\\choreo.py
Writes clips/<run_id>/captions.json (step text per frame) for make_multiview.py.
"""
import heapq
import json
import math
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
from robot import (CARRY_AHEAD, CARRY_SIDE, HAND_R, STOW_AHEAD, STOW_SIDE)  # noqa: E402
from world import World  # noqa: E402

AGENT = "http://127.0.0.1:" + os.environ.get("AGENT_PORT", "8080")
ADMIN = "http://127.0.0.1:" + os.environ.get("ADMIN_PORT", "8081")
RUN = os.environ.get("RUN_ID", "choreo")
ROBOT_R, MARGIN, STEP = 0.2, 0.07, 0.2


def call(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=120) as resp:
        return json.loads(resp.read())


def fwd(h):
    r = math.radians(h)
    return math.cos(r), -math.sin(r)


def offset(x, y, h, right, ahead):
    fx, fy = fwd(h)
    rx, ry = fwd(h + 90)
    return x + fx * ahead + rx * right, y + fy * ahead + ry * right


def wrap(a):
    return (a + 180) % 360 - 180


class Choreo:
    def __init__(self):
        self.world = World("level1", 0)               # ground truth for the operator script only
        self.obj = {o.id: o for o in self.world.objects}
        self.holding = None
        self.captions = []

    # ---------------------------------------------------------- engine I/O
    def state(self):
        return call(AGENT, "GET", "/state")

    def caption(self, text):
        frames = call(ADMIN, "GET", "/admin/result").get("frames") or 0
        self.captions.append({"frame": frames, "text": text})
        print(f"\n== {text}  (frame {frames})")

    def turn(self, deg):
        if abs(deg) > 0.05:
            r = call(AGENT, "POST", "/turn", {"degrees": deg})
            print(f"   turn {deg:+.1f} -> {r}")

    def move(self, d):
        if abs(d) > 0.005:
            r = call(AGENT, "POST", "/move", {"distance": d})
            print(f"   move {d:.2f} -> {r}")
            if r["collided"]:
                raise RuntimeError(f"collided on a planned leg: {r}")

    # ------------------------------------------------------- free space checks
    def obstacles(self, skip=()):
        return [o for o in self.world.objects if o.id not in skip and o is not self.holding]

    def pose_free(self, x, y, h, skip=()):
        lim = self.world.half
        obs = self.obstacles(skip)
        circles = [(x, y, ROBOT_R + MARGIN)]
        if self.holding is not None:
            cx, cy = offset(x, y, h, CARRY_SIDE, CARRY_AHEAD)
            circles.append((cx, cy, self.holding.carry_r() + 0.02 + MARGIN))
        else:
            hx, hy = offset(x, y, h, STOW_SIDE, STOW_AHEAD)
            circles.append((hx, hy, HAND_R + MARGIN))
        for cx, cy, r in circles:
            if abs(cx) > lim - r or abs(cy) > lim - r:
                return False
            if any(o.dist_to_point(cx, cy) < r for o in obs):
                return False
        return True

    def leg_free(self, a, b, skip=()):
        h = math.degrees(math.atan2(-(b[1] - a[1]), b[0] - a[0]))
        n = max(1, int(math.hypot(b[0] - a[0], b[1] - a[1]) / 0.04))
        return all(self.pose_free(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n, h, skip)
                   for i in range(n + 1))

    def turn_plan(self, x, y, h0, h1, skip=()):
        """Signed turn from h0 to h1 whose sweep stays free (short way first), or None."""
        d = wrap(h1 - h0)
        for delta in (d, d - 360 if d > 0 else d + 360):
            n = max(1, int(abs(delta) / 4))
            if all(self.pose_free(x, y, h0 + delta * i / n, skip) for i in range(n + 1)):
                return delta
        return None

    # ------------------------------------------------------------- planning
    def astar(self, start, goal, skip=()):
        lim = self.world.half - ROBOT_R - MARGIN
        node = lambda p: (round(p[0] / STEP), round(p[1] / STEP))  # noqa: E731
        pos = lambda n: (n[0] * STEP, n[1] * STEP)                 # noqa: E731
        obs = self.obstacles(skip)
        clear = ROBOT_R + MARGIN + (0.25 if self.holding is not None else 0.1)

        def free(n):
            x, y = pos(n)
            return abs(x) <= lim and abs(y) <= lim and all(o.dist_to_point(x, y) >= clear for o in obs)

        s, g = node(start), node(goal)
        openq, came, cost = [(0, s)], {s: None}, {s: 0.0}
        while openq:
            _, cur = heapq.heappop(openq)
            if cur == g:
                break
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    nb = (cur[0] + dx, cur[1] + dy)
                    if (dx or dy) and (free(nb) or nb == g):
                        c = cost[cur] + math.hypot(dx, dy)
                        if c < cost.get(nb, 1e9):
                            cost[nb], came[nb] = c, cur
                            heapq.heappush(openq, (c + math.dist(nb, g), nb))
        if g not in came:
            return None
        path, n = [], g
        while n is not None:
            path.append(pos(n))
            n = came[n]
        path.reverse()
        path[0], path[-1] = start, goal
        return path

    def go_to(self, goal, skip=()):
        """Drive to goal along a planned, verified path (each leg and each turn checked)."""
        s = self.state()
        here = (s["x"], s["y"])
        if math.dist(here, goal) < 0.01:
            return
        path = self.astar(here, goal, skip) or [here, goal]
        i = 0
        while i < len(path) - 1:
            s = self.state()
            here = (s["x"], s["y"])
            j = len(path) - 1                          # farthest waypoint reachable in one clear leg
            while j > i + 1 and not self.leg_free(here, path[j], skip):
                j -= 1
            tgt = path[j]
            h = math.degrees(math.atan2(-(tgt[1] - here[1]), tgt[0] - here[0]))
            delta = self.turn_plan(here[0], here[1], s["heading"], h, skip)
            if delta is None:
                delta = wrap(h - s["heading"])
            self.turn(delta)
            self.move(math.dist(here, tgt))
            i = j

    def approach_pose(self, target, ahead, right, skip):
        """A robot pose (x, y, heading) that puts the point (right, ahead) on target."""
        best = None
        s = self.state()
        for k in range(24):
            h = k * 15.0
            fx, fy = fwd(h)
            rx, ry = fwd(h + 90)
            x, y = target[0] - fx * ahead - rx * right, target[1] - fy * ahead - ry * right
            pre = (x - fx * 0.35, y - fy * 0.35)
            if not (self.pose_free(x, y, h, skip) and self.pose_free(pre[0], pre[1], h, skip)
                    and self.leg_free(pre, (x, y), skip)):
                continue
            d = math.dist((s["x"], s["y"]), pre)
            if best is None or d < best[0]:
                best = (d, x, y, h, pre)
        if best is None:
            raise RuntimeError(f"no free approach to {target}")
        return best[1:]

    def finish_heading(self, h, skip):
        s = self.state()
        delta = self.turn_plan(s["x"], s["y"], s["heading"], h, skip)
        self.turn(wrap(h - s["heading"]) if delta is None else delta)

    # --------------------------------------------------------------- tasks
    def pick(self, oid):
        o = self.obj[oid]
        stand = o.r + ROBOT_R + 0.10                    # robot front 0.10 m from the object's surface
        x, y, h, pre = self.approach_pose((o.x, o.y), stand, 0.0, skip=(oid,))
        self.go_to(pre, skip=(oid,))
        self.finish_heading(h, skip=(oid,))
        s = self.state()
        self.move(math.dist((s["x"], s["y"]), (x, y)))
        r = call(AGENT, "POST", "/pickup")
        print(f"   pickup -> {r}")
        if not r["ok"]:
            raise RuntimeError(f"pickup failed for {oid}")
        self.holding = o

    def place(self, target):
        x, y, h, pre = self.approach_pose(target, CARRY_AHEAD, CARRY_SIDE, skip=())
        self.go_to(pre)
        self.finish_heading(h, skip=())
        s = self.state()
        self.move(math.dist((s["x"], s["y"]), (x, y)))
        r = call(AGENT, "POST", "/drop")
        print(f"   drop -> {r}")
        s = self.state()
        o = self.holding
        o.x, o.y = offset(s["x"], s["y"], s["heading"], CARRY_SIDE, CARRY_AHEAD)
        o.yaw = 0.0
        self.holding = None
        # back away a little so the next leg starts clear of what was just placed
        s = self.state()
        bx, by = offset(s["x"], s["y"], s["heading"], 0, -0.3)
        if self.pose_free(bx, by, s["heading"]):
            self.move(-0.3)
        return r

    def orbit(self, oid, radius, loops):
        o = self.obj[oid]
        s = self.state()
        ang = math.atan2(s["y"] - o.y, s["x"] - o.x)
        start = (o.x + radius * math.cos(ang), o.y + radius * math.sin(ang))
        self.go_to(start, skip=())
        # one continuous circle: the object exactly 90 deg to the right, then /arc
        # (the old version drove a 16-sided polygon: turn-stop-move-stop-turn, which looked jerky)
        s = self.state()
        to_c = math.degrees(math.atan2(-(o.y - s["y"]), o.x - s["x"]))
        self.finish_heading(to_c - 90.0, skip=())
        r = call(AGENT, "POST", "/arc", {"radius": radius, "degrees": 360.0 * loops})
        print(f"   arc r {radius} x {loops} -> {r}")
        if r["collided"]:
            raise RuntimeError(f"collided on the orbit: {r}")

    def command(self, text):
        call(ADMIN, "POST", "/admin/command", {"text": text, "run_id": RUN, "keep_scene": True})


def main():
    for _ in range(80):
        try:
            call(AGENT, "GET", "/state")
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    c = Choreo()
    call(ADMIN, "POST", "/admin/reset", {"level": "level1", "seed": 0})
    call(ADMIN, "POST", "/admin/command", {"text": "Bring the red sphere to the green square.", "run_id": RUN})
    t0 = time.time()

    c.caption("1. Bring the red sphere to the green square")
    c.pick("o2")
    zx, zy = c.world.zone_c
    c.place((zx, zy))

    c.command("Circle the red cube twice.")
    c.caption("2. Circle the red cube twice")
    c.orbit("o1", 0.72, 2)

    c.command("Bring the red sphere from the green square to a corner.")
    c.caption("3. Bring the red sphere from the green square to a corner")
    corner = (-2.45, -2.45)
    c.pick("o2")
    c.place(corner)

    spots = [(-1.92, -2.48), (-2.48, -1.92), (-1.40, -2.48), (-2.48, -1.40), (-1.92, -1.92)]
    order = [("o1", "red cube"), ("o3", "blue cube"), ("o4", "blue sphere"),
             ("o5", "yellow can"), ("o6", "orange cone")]
    for n, ((oid, name), spot) in enumerate(zip(order, spots), 1):
        c.command(f"Place the {name} next to the red sphere.")   # each drop ends a task
        c.caption(f"4.{n} Place the {name} next to the red sphere")
        c.pick(oid)
        c.place(spot)

    c.caption("Done: all six objects gathered in the corner")
    res = call(ADMIN, "GET", "/admin/result")
    call(ADMIN, "POST", "/admin/record_stop")
    (ROOT / "clips" / RUN / "captions.json").write_text(json.dumps(c.captions, indent=2))
    print(f"\nresult {res}\nwall time {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
