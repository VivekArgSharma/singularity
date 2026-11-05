"""Scene data, footprints, collision, depth ray and zone test.

Pure Python (no rendering). Everything in this module is engine-private
ground truth and must never be serialised into an API reply.

Conventions: world (x, y) on the floor. Heading h in degrees, 0 = +x,
positive = clockwise seen from above, so forward = (cos h, -sin h).
"""
import json
import math
import random
from pathlib import Path

LEVEL_DIR = Path(__file__).resolve().parent / "levels"


def wrap180(a):
    a = (a + 180.0) % 360.0 - 180.0
    return 180.0 if a == -180.0 else a


def forward(heading):
    r = math.radians(heading)
    return math.cos(r), -math.sin(r)


def bearing_to(x, y, heading, tx, ty):
    """Clockwise bearing (deg) of point (tx, ty) relative to the heading."""
    ang = math.degrees(math.atan2(-(ty - y), tx - x))
    return wrap180(ang - heading)


class Obj:
    def __init__(self, spec):
        self.id = spec["id"]
        self.shape = spec["shape"]
        self.color_name = spec["color_name"]
        self.rgb = tuple(spec["rgb"])
        self.size = spec.get("size")          # cube edge / sphere diameter
        self.radius = spec.get("radius")      # cylinder / cone base radius
        self.h = spec.get("height") or self.size
        self.x, self.y = spec["pos"]
        # boxes may be rectangular (maze walls, the ramp): size_x along the object's own +x axis
        self.sx = spec.get("size_x") or self.size
        self.sy = spec.get("size_y") or self.size
        self.z = float(spec.get("z", 0.0))    # base height (a cube resting on a platform)
        self.lip = float(spec.get("lip", 0.0))   # ramp: hinged lip beyond the high end (placement tolerance)
        self.lid = spec.get("lid", False)
        self.held = False
        self.delivered = False
        self.yaw = float(spec.get("yaw", 0.0))   # clockwise degrees; cubes keep their angle when set down
        self.yaw_off = 0.0        # yaw relative to the robot while held

    def _local(self, dx, dy):
        """World offset -> the object's own axes (only cubes have a rotated footprint)."""
        if not self.yaw:
            return dx, dy
        t = math.radians(self.yaw)
        return dx * math.cos(t) - dy * math.sin(t), dx * math.sin(t) + dy * math.cos(t)

    # footprint: circle (r) or axis-aligned square (half)
    @property
    def is_box(self):
        return self.shape in ("cube", "ramp")

    @property
    def half(self):
        return self.size / 2.0

    @property
    def hx(self):
        return self.sx / 2.0

    @property
    def hy(self):
        return self.sy / 2.0

    @property
    def r(self):
        if self.shape == "sphere":
            return self.size / 2.0
        if self.is_box:
            return max(self.hx, self.hy)
        return self.radius

    @property
    def top(self):
        return self.z + self.h

    def height_at(self, px, py):
        """Ramp only: the surface height over a floor point, or None if the point is not on it.
        The ramp rises along its own +x axis from 0 at the low end to its height at the high end,
        then the lip continues level for `lip` metres (that is how far it may stand off a platform)."""
        if self.shape != "ramp" or self.held:
            return None
        lx, ly = self._local(px - self.x, py - self.y)
        if abs(ly) > self.hy or lx < -self.hx or lx > self.hx + self.lip:
            return None
        return self.z + self.h * min(1.0, (lx + self.hx) / (2 * self.hx))

    @property
    def volume(self):
        if self.is_box:
            return self.sx * self.sy * self.h
        if self.shape == "sphere":
            return math.pi / 6 * self.size ** 3
        if self.shape == "cylinder":
            return math.pi * self.radius ** 2 * self.h
        return math.pi * self.radius ** 2 * self.h / 3.0

    def carry_r(self):
        """Footprint radius while carried (circle that covers the footprint)."""
        return math.hypot(self.hx, self.hy) if self.is_box else self.r

    def dist_to_point(self, px, py):
        """Distance from a point to this object's footprint edge (0 if inside)."""
        if self.is_box:
            lx, ly = self._local(px - self.x, py - self.y)
            dx = max(abs(lx) - self.hx, 0.0)
            dy = max(abs(ly) - self.hy, 0.0)
            return math.hypot(dx, dy)
        return max(math.hypot(px - self.x, py - self.y) - self.r, 0.0)

    def ray_hit(self, ox, oy, fx, fy):
        """Distance along a ray to this footprint, or None."""
        if self.is_box:
            lox, loy = self._local(ox - self.x, oy - self.y)
            lfx, lfy = self._local(fx, fy)
            tmin, tmax = -math.inf, math.inf
            for o, f, hh in ((lox, lfx, self.hx), (loy, lfy, self.hy)):
                lo, hi = -hh, hh
                if abs(f) < 1e-12:
                    if o < lo or o > hi:
                        return None
                    continue
                t1, t2 = (lo - o) / f, (hi - o) / f
                tmin, tmax = max(tmin, min(t1, t2)), min(tmax, max(t1, t2))
            if tmax < max(tmin, 0.0):
                return None
            return max(tmin, 0.0)
        dx, dy = self.x - ox, self.y - oy
        tca = dx * fx + dy * fy
        d2 = dx * dx + dy * dy - tca * tca
        if d2 > self.r ** 2:
            return None
        thc = math.sqrt(self.r ** 2 - d2)
        t0 = tca - thc
        if t0 >= 0:
            return t0
        return 0.0 if tca + thc >= 0 else None


class World:
    def __init__(self, level="level1", seed=0, cfg=None):
        given = cfg is not None                   # a mission scene: it places its own seeded layout
        cfg = cfg if given else json.loads((LEVEL_DIR / f"{level}.json").read_text())
        self.cfg = cfg
        self.level = level
        self.seed = seed
        self.half = cfg["room"]["size"] / 2.0
        self.wall_h = cfg["room"]["wall_height"]
        # destination zones: the problem statement's single green square, or several coloured ones
        self.zones = [dict(zz) for zz in cfg.get("zones", [])] or [{"color_name": "green", **cfg["zone"]}]
        z = self.zones[0]
        self.zone_c = tuple(z["center"])
        self.zone_size = z["size"]
        self.exit = cfg.get("exit")           # maze: the exit tile {center, size, rgb}
        self.platforms = [Obj({"shape": "cube", "color_name": p.get("color_name", "purple"), "rgb": p["rgb"],
                               "size_x": p["size"][0], "size_y": p["size"][1], "height": p["height"],
                               "pos": p["pos"], "id": f"platform{i}"})
                          for i, p in enumerate(cfg.get("platforms", []))]
        rb = cfg["robot"]
        self.robot_r = rb["radius"]
        self.start = tuple(rb["start"])
        self.start_heading = rb["heading"]
        self.objects = [Obj(o) for o in cfg["objects"]]
        self.obstacles = [Obj({**o, "color_name": "grey", "id": f"wall{i}"})
                          for i, o in enumerate(cfg.get("obstacles", []))]
        self.elevated = bool(self.platforms) or any(o.shape == "ramp" for o in self.objects)
        if seed and not given:
            self._shuffle(random.Random(seed))

    def _shuffle(self, rng):
        """Seeded layout variant: same objects, new positions, same spacing rules."""
        lim = self.half - 0.6
        zx, zy = self.zone_c
        zh = self.zone_size / 2 + 0.4
        placed = []
        for o in self.objects:
            for _ in range(2000):
                x, y = rng.uniform(-lim, lim), rng.uniform(-lim, lim)
                if math.hypot(x - self.start[0], y - self.start[1]) < 1.0:
                    continue
                if abs(x - zx) < zh and abs(y - zy) < zh:
                    continue
                if any(math.hypot(x - px, y - py) < 0.8 for px, py in placed):
                    continue
                if any(ob.dist_to_point(x, y) < 0.5 for ob in self.obstacles):
                    continue
                break
            o.x, o.y = x, y
            placed.append((x, y))

    # -------------------------------------------------------------- queries
    def solids(self, ignore=None, z=0.0):
        """Things a body standing at height z can bump into. Ramps and platforms are not here:
        they are surfaces (support_height); a robot meets their edges through the step rule."""
        out = [o for o in self.objects if not o.held and o is not ignore and o.shape != "ramp"
               and o.top > z + 0.02 and o.z < z + 0.45] + self.obstacles
        return out

    def support_height(self, x, y, ignore=None):
        """Height of the surface under a floor point: platform tops and ramps, else the floor (0)."""
        if not self.elevated:
            return 0.0
        h = 0.0
        for p in self.platforms:
            if p.dist_to_point(x, y) <= 0.0:
                h = max(h, p.top)
        for o in self.objects:
            if o is not ignore:
                v = o.height_at(x, y)
                if v is not None:
                    h = max(h, v)
        return h

    def circle_blocked(self, x, y, r, ignore=None, z=0.0):
        lim = self.half - r
        if abs(x) > lim or abs(y) > lim:
            return True
        return any(o.dist_to_point(x, y) < r for o in self.solids(ignore, z))

    def ray(self, ox, oy, heading, ignore=None, max_d=10.0, z=0.0):
        """Range along the floor plane from an observer standing at height z. Platforms and ramps
        taller than the observer's floor are seen; anything not above it is looked over."""
        fx, fy = forward(heading)
        best = None
        seen = self.solids(ignore, z) + [p for p in self.platforms if p.top > z + 0.05] + \
            [o for o in self.objects if o.shape == "ramp" and not o.held and o is not ignore and o.top > z + 0.05]
        for o in seen:
            t = o.ray_hit(ox, oy, fx, fy)
            if t is not None and (best is None or t < best):
                best = t
        for o, f in ((ox, fx), (oy, fy)):
            if abs(f) > 1e-12:
                t = ((self.half if f > 0 else -self.half) - o) / f
                if t >= 0 and (best is None or t < best):
                    best = t
        if best is None or best > max_d:
            return None
        return best

    def in_zone(self, x, y):
        zx, zy = self.zone_c
        h = self.zone_size / 2.0
        return abs(x - zx) <= h and abs(y - zy) <= h

    def zone_at(self, x, y):
        """The destination zone containing a floor point (several-zone missions), or None."""
        for z in self.zones:
            h = z["size"] / 2.0
            if abs(x - z["center"][0]) <= h and abs(y - z["center"][1]) <= h:
                return z
        return None

    def in_exit(self, x, y):
        if not self.exit:
            return False
        h = self.exit["size"] / 2.0
        return abs(x - self.exit["center"][0]) <= h and abs(y - self.exit["center"][1]) <= h
