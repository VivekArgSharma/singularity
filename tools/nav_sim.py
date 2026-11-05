"""Pure-Python test body for navigation: the engine's own robot physics (robot.py) without
rendering, a map built only from the range scan, and ground-truth clearance measured on
every 1/60 s step (operator-side measurement; the planner never sees it)."""
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT / "agent"))
from mapping import CoverageMap  # noqa: E402
from robot import CARRY_AHEAD, CARRY_SIDE, HAND_R, STOW_AHEAD, STOW_SIDE, Robot  # noqa: E402
from world import forward  # noqa: E402


def scan(r, n=91):
    fx, fy = forward(r.heading)
    ox, oy = r.x + fx * r.r, r.y + fy * r.r
    return [r.world.ray(ox, oy, r.heading - 45 + 90 * i / (n - 1), ignore=r.held) for i in range(n)]


def clearance(r):
    """(body, appendage) distance to the nearest real surface, metres (negative = overlap)."""
    w = r.world
    solids = w.solids(r.held)

    def surf(x, y):
        d = w.half - max(abs(x), abs(y))
        return min([d] + [o.dist_to_point(x, y) for o in solids])
    body = surf(r.x, r.y) - r.r
    if r.held is not None:
        side, ahead, rad = CARRY_SIDE, CARRY_AHEAD, r.held.carry_r()
    else:
        side, ahead, rad = STOW_SIDE, STOW_AHEAD, HAND_R
    ax, ay = r.local_to_world(side, ahead)
    return body, surf(ax, ay) - rad


class SimBody:
    def __init__(self, level="level1", seed=0):
        self.r = Robot(level, seed)
        self.r.set_command("navigate", "nav")
        self.r.interactive = True
        self.cmap = CoverageMap()
        self.carry_r = 0.23
        self.steps = 0
        self.min_body = self.min_app = 9.0
        self.messages = []
        body = self

        class Cl:
            def scan(self, n=91):
                return scan(body.r, n)

            def turn(self, deg, speed=1.0):
                body._run(body.r.turn(deg, speed))
                return {"heading": round(body.r.heading, 1)}

            def move(self, d, speed=1.0):
                return body._run(body.r.move(d, speed))

            def arc(self, rad, deg, speed=1.0):
                return body._run(body.r.arc(rad, deg, speed))
        self.cl = Cl()

    def _run(self, gen):
        try:
            while True:
                next(gen)
                self.steps += 1
                b, a = clearance(self.r)
                self.min_body, self.min_app = min(self.min_body, b), min(self.min_app, a)
        except StopIteration as s:
            return s.value

    # LocalBrain-like interface used by navigate.py / explore.py
    def st(self):
        self.s = self.r.public_state()
        return self.s

    def see(self):
        s = self.st()
        self.cmap.integrate(s["x"], s["y"], s["heading"], scan(self.r))
        return []

    def turn(self, deg, speed=1.0):
        deg = (deg + 180) % 360 - 180
        while abs(deg) > 0.5:
            step = max(-180, min(180, deg))
            self.cl.turn(step)
            deg -= step

    def face_point(self, p):
        s = self.st()
        self.turn(math.degrees(math.atan2(-(p[1] - s["y"]), p[0] - s["x"])) - s["heading"])

    def move(self, d, speed=1.0):
        return self.cl.move(d)

    def say(self, text):
        self.messages.append(text)

    def publish(self):
        pass

    def survey(self, step=1.0):
        """Map the room beforehand: look around (8 headings) from a grid of free spots. Sensor only."""
        x0, y0, h0 = self.r.x, self.r.y, self.r.heading
        w = self.r.world
        k = int(w.half / step)
        for gx in [i * step for i in range(-k, k + 1)]:
            for gy in [i * step for i in range(-k, k + 1)]:
                if abs(gx) > w.half - 0.4 or abs(gy) > w.half - 0.4 or w.circle_blocked(gx, gy, 0.45):
                    continue
                self.r.x, self.r.y = gx, gy
                for hh in range(0, 360, 45):
                    self.r.heading = hh
                    self.see()
        self.r.x, self.r.y, self.r.heading = x0, y0, h0
        self.see()

    def hold(self, oid):
        """Put object `oid` in the hand (as after a pickup) for carrying scenarios."""
        o = next(x for x in self.r.world.objects if x.id == oid)
        self.r.x, self.r.y = o.x - (self.r.r + 0.12 + o.r), o.y
        self.r.heading = 0.0
        res = self._run(self.r.pickup())
        self.carry_r = o.r * 1.42 + 0.02
        return res
