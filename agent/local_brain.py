"""Local planner: a free, offline stand-in for the Claude agent.

It understands a fixed set of English commands with regular expressions and finds
things with colour blobs in the robot camera image (perception.py). It uses only
the agent API (/state, /look, /depth and the actions), never ground truth, so it is
a fair baseline and lets the dashboard, pre-emption and video pipeline run without
an API key. Claude (claude_brain.py) is the real agent for open-ended language.
"""
import math
import re

import explore
from mapping import CoverageMap
from perception import detect, to_world
from typos import COMMON, NOT_OBJECT, SHAPE_WORDS, VOCAB, _correct, _fix_typos  # noqa: F401

COLOURS = r"red|blue|yellow|orange|green|grey|gray|purple|white|black|pink|brown|silver|gold|violet|cyan"
SEEABLE = {"red", "blue", "yellow", "orange", "green"}       # colours perception.py can find
WORDS = ["cube", "box", "block", "sphere", "ball", "cylinder", "can", "cone", "square", "zone", "green",
         "bring", "fetch", "circle", "around", "behind", "pick", "drop", "approach", "explore", "inspect"]
SHAPES = {"cube": "cube", "box": "cube", "block": "cube", "sphere": "sphere", "ball": "sphere",
          "cylinder": "cylinder", "can": "cylinder", "cone": "cone"}
OBJ = re.compile(rf"(?:\b({COLOURS})\s+)?\b(cube|box|block|sphere|ball|cylinder|can|cone)s?\b")
SUPERLATIVES = {"tallest": ("height", max), "highest": ("height", max), "shortest": ("height", min),
                "lowest": ("height", min), "biggest": ("volume", max), "largest": ("volume", max),
                "smallest": ("volume", min), "farthest": ("zone", max), "furthest": ("zone", max),
                "closest": ("zone", min), "nearest": ("zone", min)}
# the drop square, however it is called: "green square", "green tile", "the finish", "the destination", ...
DEST_WORDS = r"finish(?: line| zone| area| state| tile| point)?|destination|goal(?: zone| area| tile)?|target (?:zone|area|square|tile|spot)|drop[ -]?(?:zone|off|area|point|spot)|end zone|landing zone|tile"
ZONE = re.compile(r"green (zone|square|area|patch|box|block|pad|mat|tile)|\bthe zone\b|\b(?:" + DEST_WORDS + r")\b")
COUNT = {"once": 1, "one time": 1, "twice": 2, "two times": 2, "thrice": 3, "three times": 3}
CARRY_AHEAD, CARRY_SIDE, ROBOT_R = 0.62, 0.36, 0.2
STOW_AHEAD, STOW_SIDE, HAND_R = 0.10, 0.27, 0.08     # folded empty hand (engine/robot.py)


def fwd(h):
    r = math.radians(h)
    return math.cos(r), -math.sin(r)


def heading_of(dx, dy):
    return math.degrees(math.atan2(-dy, dx))


def wrap(a):
    return (a + 180.0) % 360.0 - 180.0


class Fail(Exception):
    pass


class LocalBrain:
    def __init__(self, cl, emit, command, context="", map_file=None, mapping=True):
        from typos import fix as _fix_all
        command = _fix_all(command)                # typos, then the many ways people say the same thing
        self.cl, self.emit = cl, emit
        self.map_file = map_file
        self.cmap = CoverageMap.load(map_file) if mapping else None     # the robot's own map, kept across tasks
        self._turned = 0.0
        self.last_obs = None
        self.carry_r = 0.23              # footprint of whatever is carried (from its measured size)
        self.command = command
        self.text = re.sub(r"\b(can|could) (you|u|i|we)\b", r"please \2", command.lower())
        self.context = context.lower()
        self.speed = 0.35 if re.search(r"\bslow(ly|er)?\b|carefully|gently", self.text) else 1.0
        self.lock = None            # world estimate of the object being tracked
        self.track = None           # set by a mission solver: the known position of the one box it wants
        self.zone_w = None          # last seen centre of the green square (remembered)
        self.map = {}               # (colour, shape) -> (world centre, radius), built from the camera
        self.heights = {}           # (colour, shape) -> height estimates (m) from the camera
        self.target_key = None
        self.side = 1
        self.on_see = None          # mission solvers: called with (detections, pose, scan) after every look
        self.exclude = []           # [((x, y), half)] floor areas whose detections are ignored (sorted boxes)
        self.extra_obstacles = None  # mission solvers: callable -> [((x, y), r)] objects the colour map cannot hold
                                     # (two boxes of the same colour share one map key)

    # ---------------------------------------------------------------- basics
    def say(self, text):
        self.emit("thinking", text=text)

    def st(self):
        self.s = self.cl.state()
        if self.s["name"] == "Done" and self.s["retries"] > 3 and not self.text.lstrip().startswith("mission"):
            raise Fail("too many collisions (fail_collision)")
        return self.s

    def see(self):
        s = self.st()
        dets = detect(self.cl.look(), holding=s["holding"])
        scan = self.cl.scan(91) if (self.cmap is not None or self.on_see is not None) else None
        if scan is not None:
            from mission_brain import _solid
            for d in dets:                        # a green (or magenta) blob the range scan hits is a thing, not paint
                if "obj" in d and d["obj"].get("centre_rel") and _solid(d, scan):
                    d.update(d.pop("obj"))
        for d in dets:
            if "centre_rel" in d:
                d["w"] = to_world(d["centre_rel"], s["x"], s["y"], s["heading"])
        if self.on_see is not None:               # mission solvers see everything (squares included)
            self.on_see(dets, s, scan if scan is not None else self.cl.scan(91))
        if self.exclude:                          # already-sorted areas: never remembered, never targeted
            dets = [d for d in dets if "w" not in d or not any(
                abs(d["w"][0] - c[0]) < h and abs(d["w"][1] - c[1]) < h for c, h in self.exclude)]
        for d in dets:
            if d["shape"] not in (None, "zone", "tile") and "w" in d and not d["edge"]:
                k = (d["color"], d["shape"])
                for other in [o for o in self.map if o[0] == d["color"] and o != k]:
                    if math.dist(self.map[other][0], d["w"]) < 0.3:    # same object, earlier mis-read shape
                        del self.map[other]
                        self.heights.pop(other, None)
                self.map[k] = (d["w"], d.get("radius", 0.15))
                if d.get("height"):
                    self.heights.setdefault((d["color"], d["shape"]), []).append(d["height"])
        if self.cmap is not None:                 # every look also updates the map (no extra moves)
            stat = self.cmap.integrate(s["x"], s["y"], s["heading"], scan)
            for d in dets:
                if d["shape"] not in (None, "zone", "tile") and "w" in d and not d["edge"]:
                    self.cmap.add_object(f"{d['color']} {d['shape']}", d["w"][0], d["w"][1], d.get("radius", 0.15))
            if self._turned:
                self.cmap.note_turn(self._turned, stat["new_m2"])
                self._turned = 0.0
            self.last_obs = stat
            self.publish()
        zones = [d for d in dets if d["shape"] == "zone" and "w" in d and d["area"] > 600]
        if zones:
            self.zone_w = max(zones, key=lambda d: d["area"])["w"]
        return dets

    def publish(self):
        if self.cmap is None:
            return
        self.emit("map", map=self.cmap.snapshot())
        if self.map_file:
            try:
                self.cmap.save(self.map_file)
            except Exception as e:  # noqa: BLE001  (the task goes on; the next task just starts a fresh map)
                self.emit("warning", text=f"map not saved: {type(e).__name__}: {e}")

    def _sweep_clear(self, h0, delta):
        """Would the carried object, or the folded empty hand, hit anything seen so far while turning by delta?"""
        s = self.s
        holding = bool(s.get("holding"))
        ahead, side, pad = (CARRY_AHEAD, CARRY_SIDE, 0.24) if holding else (STOW_AHEAD, STOW_SIDE, HAND_R + 0.05)
        n = max(2, int(abs(delta) / 5))
        for k in range(n + 1):
            h = h0 + delta * k / n
            fx, fy = fwd(h)
            rx, ry = fwd(h + 90)
            cx, cy = s["x"] + fx * ahead + rx * side, s["y"] + fy * ahead + ry * side
            for c, r in self.map.values():
                if not holding and self.lock is not None and math.dist(c, self.lock) < 0.3:
                    continue                     # the thing being approached: facing it is the point
                if math.dist((cx, cy), c) < r + pad:
                    return False
            if self.cmap is not None:
                j, i = self.cmap.idx(cx, cy)
                win = self.cmap.occ[max(0, j - 2):j + 3, max(0, i - 2):i + 3]
                if (win == 2).any():
                    return False
        return True

    def _step_clear(self, dist):
        """Could the body drive `dist` straight (negative = backwards) without touching anything mapped?"""
        s = self.st()
        fx, fy = fwd(s["heading"])
        a = (s["x"], s["y"])
        b = (a[0] + fx * dist, a[1] + fy * dist)
        if not self.segment_clear(a, b):
            return False
        L2 = (b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2 or 1e-9
        for c, r in self.map.values():
            t = max(0.0, min(1.0, ((c[0] - a[0]) * (b[0] - a[0]) + (c[1] - a[1]) * (b[1] - a[1])) / L2))
            if math.dist(c, (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))) < r + ROBOT_R + 0.05:
                return False
        return True

    def _make_room(self):
        """Step out of a tight spot: back the way we came if that is clear (it usually is), else forward
        if the map and the depth ray both say so, else back anyway (backing never swings the hand)."""
        if self._step_clear(-0.25):
            self.say("No room to turn here; backing up first.")
            self.cl.move(-0.25)
            return
        d = self.cl.depth()
        if (d is None or d > 0.7) and self._step_clear(0.3):
            self.say("No room to turn here; moving forward into free space first.")
            self.cl.move(0.3)
            return
        self.say("No room to turn here; backing up a little.")
        self.cl.move(-0.15)

    def back_off(self, dist=0.3):
        """Step back from what was just put down, only as far as the map says is clear behind."""
        for d in (dist, dist / 2):
            if self._step_clear(-d):
                return self.cl.move(-d)
        self.say("Something is right behind me; staying put instead of backing up.")
        return {"moved": 0.0, "collided": False}

    def _carry_turn(self, deg):
        """Pick the turn direction whose sweep is clear (the carried object, or the folded empty hand,
        reaches past the body), making room first if neither is."""
        if abs(deg) < 8:
            return deg
        holding = bool(self.st()["holding"])
        for attempt in range(2):
            h0 = self.st()["heading"]
            for d in (deg, deg - 360 if deg > 0 else deg + 360):
                if self._sweep_clear(h0, d):
                    return d
            if holding:
                self.say("Turning here would swing the load into something; backing up first.")
                if attempt == 0 and self.open_space():
                    continue
                self.cl.move(-0.3)
            else:
                self._make_room()
        return deg

    def turn(self, deg, speed=None):
        deg = wrap(deg)
        if self.st()["holding"]:
            deg = self._carry_turn(deg)
        while abs(deg) > 180:                    # the long way round, in two legal turns
            half = 180 if deg > 0 else -180
            self.cl.turn(half, speed or self.speed)
            self._turned += half
            deg -= half
        for attempt in range(2):
            if abs(deg) <= 0.5:
                return
            h0 = self.st()["heading"]
            got = wrap(self.cl.turn(deg, speed or self.speed)["heading"] - h0)
            self._turned += got
            if abs(got - deg) < 1.0:
                return
            self._make_room()                    # the turn was blocked: never drive blindly into it
            deg -= got

    def face_point(self, p):
        s = self.st()
        self.turn(heading_of(p[0] - s["x"], p[1] - s["y"]) - s["heading"])

    def move(self, d, speed=None):
        if abs(d) < 0.005:
            return {"moved": 0.0, "collided": False}
        r = self.cl.move(d, speed or self.speed)
        self._turned = 0.0                       # a turn followed by a drive was part of a route
        if r.get("collided"):
            self.say("Bumped into something; backing off and stepping around it.")
            self.detour()
        return r

    def detour(self):
        self.side = -self.side
        self.cl.turn(55 * self.side)
        r = self.cl.move(0.4)
        self.cl.turn(-55 * self.side)
        if r.get("collided"):
            self.cl.move(-0.2)

    def blocker(self, a, b, skip=None):
        """First mapped object (other than `skip`) the robot would hit driving from a to b."""
        clear = ROBOT_R + 0.12 + (0.32 if self.s["holding"] else 0.0)
        dx, dy = b[0] - a[0], b[1] - a[1]
        L2 = dx * dx + dy * dy or 1e-9
        best = None
        tpos = self.lock if self.lock is not None else (self.map[skip][0] if skip in self.map else None)
        items = list(self.map.items())
        if self.extra_obstacles is not None:
            items += [(("extra", i), (c, r)) for i, (c, r) in enumerate(self.extra_obstacles())
                      if tpos is None or math.dist(c, tpos) > 0.45]
        for key, (c, r) in items:
            if key == skip:
                continue
            if skip and key[0] == skip[0] and tpos is not None and math.dist(c, tpos) < 0.45:
                continue                                  # the target itself, seen once as another shape
            t = max(0.0, min(1.0, ((c[0] - a[0]) * dx + (c[1] - a[1]) * dy) / L2))
            q = (a[0] + dx * t, a[1] + dy * t)
            if math.dist(q, c) < r + clear and (best is None or t < best[0]):
                best = (t, c, r, q)
        if best is None:
            return None
        _, c, r, q = best
        n = (q[0] - c[0], q[1] - c[1])
        if math.hypot(*n) < 1e-6:
            n = (-dy, dx)
        k = math.hypot(*n)
        return (c[0] + n[0] / k * (r + clear + 0.12), c[1] + n[1] / k * (r + clear + 0.12))

    def segment_clear(self, a, b, margin=0.05, ignore=None):
        """No wall or obstacle the range scan has mapped within the body's half-width of the straight
        segment a -> b (cells near `ignore`, the target itself, do not count)."""
        if self.cmap is None:
            return True
        from mapping import OCC
        cm = self.cmap
        n = max(2, int(math.dist(a, b) / 0.05))
        r = ROBOT_R + margin
        k = int(r / 0.1) + 1
        for t in range(n + 1):
            x, y = a[0] + (b[0] - a[0]) * t / n, a[1] + (b[1] - a[1]) * t / n
            j0, i0 = (int(v) for v in cm.idx(x, y))
            win = cm.occ[max(0, j0 - k):j0 + k + 1, max(0, i0 - k):i0 + k + 1]
            if (win == OCC).any():
                for jj, ii in zip(*((win == OCC).nonzero())):
                    cx, cy = cm.centre(max(0, j0 - k) + jj, max(0, i0 - k) + ii)
                    if math.hypot(cx - x, cy - y) < r and (ignore is None or math.dist((cx, cy), ignore) > 0.35):
                        return False
        return True

    def go_point(self, p, tol=0.05, skip=None, final_heading=None):
        if self.cmap is not None:                    # A* on the robot's own map, smoothed, replanned
            from navigate import navigate
            if navigate(self, p, final_heading=final_heading, tol=max(tol, 0.06)):
                return True
            s = self.st()
            if not self.segment_clear((s["x"], s["y"]), p, ignore=self.map[skip][0] if skip in self.map else None):
                self.say("No planned route there, and the straight line crosses a mapped wall: not driving blind.")
                return False
        if self.cmap is not None and skip is None:
            s = self.st()
            route = self.cmap.plan_path((s["x"], s["y"]), p, 0.62 if s["holding"] else 0.48)
            if route is not None and len(route) >= 2:
                self.cmap.plan = {"x": p[0], "y": p[1], "heading": None,
                                  "route": [(round(a, 2), round(b, 2)) for a, b in route]}
                explore.follow(self, route, observe_legs=len(route) > 2)
        for _ in range(10):
            s = self.st()
            dist = math.hypot(p[0] - s["x"], p[1] - s["y"])
            if dist < tol:
                return True
            via = self.blocker((s["x"], s["y"]), p, skip)
            tgt = p
            if via is not None:
                self.say(f"Something is in the way; going round it via ({via[0]:.2f}, {via[1]:.2f}).")
                tgt = via
            self.face_point(tgt)
            self.move(min(math.dist((s["x"], s["y"]), tgt), 1.5))
        return False

    # ------------------------------------------------------------ perception
    def _matches(self, d, tgt):
        return d["color"] == tgt["color"] if tgt["color"] else d["shape"] not in ("zone", "tile")

    def find(self, tgt, sweeps=9):
        """Turn in place until the target is in view; returns a detection, or None."""
        name = tgt["name"]
        key = f"{tgt['color']} {tgt['shape']}" if tgt["color"] and tgt["shape"] else None
        remembered = None
        if self.cmap is not None and key in self.cmap.objects and self.lock is None:
            o = self.cmap.objects[key]                # seen before (this or an earlier task): look there first
            remembered = (o["x"], o["y"])
        elif key and self.lock is None and (tgt["color"], tgt["shape"]) in self.map:
            remembered = tuple(self.map[(tgt["color"], tgt["shape"])][0])
        if remembered is not None:
            self.say(f"I saw the {name} before near ({remembered[0]:.2f}, {remembered[1]:.2f}); looking there.")
            self.face_point(remembered)
        limit = 24 if self.cmap is not None else sweeps
        closer = 0
        for i in range(limit):
            seen_it = key and (tgt["color"], tgt["shape"]) in self.map
            if self.cmap is not None and i >= sweeps and self.cmap.stats()["coverage_est"] >= 0.95 \
                    and not (seen_it and closer < 3):
                break                                 # the whole room has been seen: it is not here
            if seen_it and remembered is None and self.track is None and closer < 3 and i >= 1:
                remembered = tuple(self.map[(tgt["color"], tgt["shape"])][0])   # seen on the way: go and look
            dets = [d for d in self.see() if self._matches(d, tgt)]
            whole = [d for d in dets if d["shape"] is not None and (tgt["shape"] is None or d["shape"] == tgt["shape"])]
            if self.lock is not None:
                near = [d for d in dets if "w" in d and math.dist(d["w"], self.lock) < 0.6
                        and (tgt["shape"] is None or d["shape"] in (tgt["shape"], None))]   # same place AND shape
                if near:
                    return max(near, key=lambda d: d["area"])
            if self.track is not None:
                # a mission tracks one particular box (two of the same colour): a look-alike far from
                # where this one is known to be is not it, however big it looks
                whole = [d for d in whole if "w" in d and math.dist(d["w"], self.track) < 1.2]
            if whole:
                best = max(whole, key=lambda d: d["area"])
                if tgt["color"] is None and best["color"]:
                    tgt["color"] = best["color"]      # "a drink": now we know which one -> track that object only
                return best
            partial = [d for d in dets if d["shape"] is None and (tgt["color"] or self.lock is not None)]
            if partial and self.lock is not None:
                close = [d for d in partial if abs(d["bearing"]) < 30]
                if close:                       # already identified from further away; too close to see whole
                    return max(close, key=lambda d: d["area"])
            if partial:
                p = max(partial, key=lambda d: d["area"])
                if abs(p["bearing"]) > 8:
                    self.say(f"Something {p['color']} at the edge of the view; turning to see it whole.")
                    self.turn(max(-40, min(40, p["bearing"])))
                    continue
                if p["cut_bottom"]:
                    self.say(f"Too close to see the {p['color']} object whole; backing up a little.")
                    self.move(-0.3)
                    continue
            if remembered is None and key and self.lock is None and (tgt["color"], tgt["shape"]) in self.map and closer < 3:
                remembered = tuple(self.map[(tgt["color"], tgt["shape"])][0])   # glimpsed while driving
                self.say(f"I glimpsed the {name} on the way, near ({remembered[0]:.2f}, {remembered[1]:.2f}).")
            if remembered is not None and closer < 3:     # not seen cleanly from here: go closer to where it was
                closer += 1
                s = self.st()
                d = math.dist((s["x"], s["y"]), remembered)
                if d > 1.1:
                    k = (d - 1.0) / d
                    stand = (s["x"] + (remembered[0] - s["x"]) * k, s["y"] + (remembered[1] - s["y"]) * k)
                    self.say(f"Driving closer to where I saw the {name}.")
                    self.go_point(stand, tol=0.15)
                self.face_point(remembered)
                continue
            if i == 0:
                self.say(f"Looking for the {name}.")
            if self.cmap is not None:                 # look where the most unseen floor is, not round in circles
                s = self.st()
                best = self.cmap.next_best_view(s["x"], s["y"], s["heading"], s["holding"])
                if best is not None:
                    explore.go(self, best)
                    continue
                if self.cmap.stats()["coverage_est"] < 0.8 and explore.unstick(self):
                    continue
            self.turn(45)
        if tgt["color"] and tgt["shape"] and self.cmap is not None:
            for k, (c, r) in list(self.map.items()):
                if k[0] != tgt["color"] or k[1] == tgt["shape"]:
                    continue
                self.say(f"I saw a {k[0]} {k[1]} - maybe the {name} seen badly; taking a closer look.")
                s = self.st()
                d = math.dist((s["x"], s["y"]), c) or 1.0
                k2 = min(1.0, 0.9 / d)
                view = (c[0] + (s["x"] - c[0]) * k2, c[1] + (s["y"] - c[1]) * k2)
                self.go_point(view, tol=0.15, skip=k)
                self.face_point(c)
                for _ in range(3):
                    dets = [d for d in self.see() if self._matches(d, tgt) and d["shape"] == tgt["shape"]]
                    if dets:
                        self.map.pop(k, None)             # it was the target all along
                        return max(dets, key=lambda d: d["area"])
                    self.turn(20)
        return None

    def measure(self, tgt):
        """Face the target and measure it: (surface distance from the robot front, centre, radius)."""
        det = self.find(tgt)
        if det is None:
            seen = sorted({f"{c} {sh}" for c, sh in self.map})
            cov = ""
            if self.cmap is not None:
                cov = f" after searching {self.cmap.stats()['coverage_est']:.0%} of the room"
            raise Fail(f"Not present: there is no {tgt['name']} here{cov}. "
                       f"I saw {', '.join(seen) if seen else 'no objects'}. Nothing was picked up.")
        self.turn(det["bearing"])
        dets = [d for d in self.see() if self._matches(d, tgt)
                and (tgt["shape"] is None or d["shape"] in (tgt["shape"], None))   # the red sphere is not the red cube
                and (self.track is None or "w" not in d or math.dist(d["w"], self.track) < 1.2)]   # nor its twin elsewhere
        if dets:
            det = min(dets, key=lambda d: abs(d["bearing"]))
        rad = det.get("radius", 0.15)
        s = self.s
        fx, fy = fwd(s["heading"])
        front = (s["x"] + fx * ROBOT_R, s["y"] + fy * ROBOT_R)
        est = None
        if "w" in det:
            est = math.dist(front, det["w"]) - rad
        depth = self.cl.depth()
        if depth is not None and (est is None or abs(depth - est) < 0.45):
            surf = depth
            centre = (front[0] + fx * (depth + rad), front[1] + fy * (depth + rad))
        elif est is not None:
            surf, centre = est, det["w"]
        else:
            raise Fail(f"lost the {tgt['name']}")
        self.lock = centre
        return surf, centre, rad

    def approach(self, tgt, stand):
        for _ in range(14):
            surf, centre, rad = self.measure(tgt)
            gap = surf - stand
            if gap <= 0.04:
                return surf, centre, rad
            s = self.s
            if self.cmap is not None and gap > 0.6:
                d = math.dist((s["x"], s["y"]), centre) or 1.0
                k = (rad + ROBOT_R + stand + 0.25) / d
                standp = (centre[0] + (s["x"] - centre[0]) * k, centre[1] + (s["y"] - centre[1]) * k)
                self.say(f"{tgt['name'].capitalize()} {surf:.2f} m away; planning a route to it.")
                if self.go_point(standp, tol=0.12, skip=(tgt["color"], tgt["shape"])):
                    continue
            fx, fy = fwd(s["heading"])
            stop = (s["x"] + fx * min(gap, 1.2), s["y"] + fy * min(gap, 1.2))
            if self.cmap is not None and not self.segment_clear((s["x"], s["y"]), stop, ignore=centre):
                if not self.approach_side(tgt, centre, rad, stand):
                    raise Fail(f"the {tgt['name']} is behind something I cannot get round from any side")
                continue
            via = self.blocker((s["x"], s["y"]), stop, skip=(tgt["color"], tgt["shape"]))
            if via is not None:
                self.say(f"Something is between me and the {tgt['name']}; going round it.")
                self.face_point(via)
                self.move(math.dist((s["x"], s["y"]), via))
                continue
            self.say(f"{tgt['name'].capitalize()} {surf:.2f} m ahead; driving {min(gap, 1.2):.2f} m.")
            self.move(min(gap, 1.2))
        raise Fail(f"could not reach the {tgt['name']}")

    def approach_side(self, tgt, centre, rad, stand):
        """A wall or obstacle is between us and the target: try standpoints around it (this side,
        left, right, the far side), each planned with A* on the map; the cheapest reachable wins."""
        s = self.st()
        base = math.atan2(s["y"] - centre[1], s["x"] - centre[0])
        R = rad + ROBOT_R + stand + 0.3
        cands = []
        for da in (0, 60, -60, 120, -120, 180):
            a = base + math.radians(da)
            p = (centre[0] + R * math.cos(a), centre[1] + R * math.sin(a))
            if abs(p[0]) < 2.75 and abs(p[1]) < 2.75:
                cands.append((abs(da), p))
        for da, p in sorted(cands):
            self.say(f"Something solid is between me and the {tgt['name']}; trying the standpoint "
                     f"{'in front' if da == 0 else 'to the side' if da < 150 else 'behind it'} at ({p[0]:.2f}, {p[1]:.2f}).")
            if self.go_point(p, tol=0.12, skip=(tgt["color"], tgt["shape"])):
                self.face_point(centre)
                return True
        return False

    def open_space(self, need=None):
        """Carrying something near a wall, a turn on the spot swings it into the wall: first drive
        (A*) to the nearest mapped spot with `need` metres of clearance all round."""
        if self.cmap is None or getattr(self, "_in_open_space", False):
            return False
        if need is None:                          # the load swings on a circle this big round the centre
            need = math.hypot(CARRY_SIDE, CARRY_AHEAD) + self.carry_r + 0.1 if self.st()["holding"] else 0.6
        self._in_open_space = True
        try:
            return self._open_space(need)
        finally:
            self._in_open_space = False

    def _open_space(self, need):
        import numpy as np
        from planner import Planner
        s = self.st()
        P = Planner(self.cmap.occ, holding=s["holding"], carry_r=self.carry_r)
        j0, i0 = (int(v) for v in self.cmap.idx(s["x"], s["y"]))
        if P.clear[j0, i0] >= need:
            return True
        ok = np.argwhere((P.clear >= need) & P.safe)
        if not len(ok):
            return False
        d = (ok[:, 0] - j0) ** 2 + (ok[:, 1] - i0) ** 2
        j, i = ok[int(np.argmin(d))]
        p = tuple(float(v) for v in self.cmap.centre(int(j), int(i)))
        self.say(f"Too close to something to turn with the load; moving to open floor at ({p[0]:.2f}, {p[1]:.2f}).")
        return self.go_point(p, tol=0.12)

    def find_zone(self):
        if self.st()["holding"]:
            self.open_space()
        for i in range(8):
            z = [d for d in self.see() if d["shape"] == "zone" and "w" in d]
            if z:
                return max(z, key=lambda d: d["area"])["w"]
            if i == 0:
                self.say("Looking for the green square.")
            self.turn(45)
        if self.cmap is not None:                 # not visible from here (walls): go to where it could be seen
            self.say("The green square is not visible from here; exploring until I see it.")
            explore.explore_nbv(self, goal=1.01, max_views=25, stop=lambda: self.zone_w is not None)
            if self.zone_w is not None:
                return self.zone_w
        raise Fail("could not find the green square")

    # --------------------------------------------------------------- skills
    def pick(self, tgt):
        for attempt in range(3):
            self.approach(tgt, 0.10)
            self.say(f"In reach of the {tgt['name']}; picking it up.")
            if self.cl.pickup().get("ok"):
                rad = self.map.get((tgt["color"], tgt["shape"]), (None, 0.15))[1]
                self.carry_r = rad * 1.42 + 0.02                     # worst case: a box seen corner-on
                self.map.pop((tgt["color"], tgt["shape"]), None)     # it travels with us now
                if self.cmap is not None and self.lock is not None:  # and the floor it stood on is free
                    import numpy as np
                    from mapping import FREE, OCC, CELL, N
                    j, i = self.cmap.idx(*self.lock)
                    k = int(0.35 / CELL) + 1
                    win = self.cmap.occ[max(0, j - k):j + k + 1, max(0, i - k):i + k + 1]
                    win[win == OCC] = FREE
                    self.cmap.objects.pop(f"{tgt['color']} {tgt['shape']}", None)
                self.cl.move(-0.3)               # room to swing the carried object
                return
            self.move(-0.25)
        raise Fail(f"pickup of the {tgt['name']} kept failing")

    def drop(self):
        """Drop, and remember that the object now stands where the hand was."""
        s = self.st()
        r = self.cl.drop()
        if r.get("ok") and self.cmap is not None:
            fx, fy = fwd(s["heading"])
            rx, ry = fwd(s["heading"] + 90)
            x = s["x"] + fx * CARRY_AHEAD + rx * CARRY_SIDE
            y = s["y"] + fy * CARRY_AHEAD + ry * CARRY_SIDE
            self.cmap.mark_occupied(x, y, max(0.1, (self.carry_r - 0.02) / 1.42))
            self.publish()
        return r

    def put_down_here(self):
        self.say("Putting down the object I am holding first.")
        self.drop()
        self.back_off()

    def deliver_zone(self):
        z = self.zone_w
        if z is not None:
            self.face_point(z)                   # seen before: turn straight towards it
            zs = [d for d in self.see() if d["shape"] == "zone" and "w" in d]
            if zs:
                z = max(zs, key=lambda d: d["area"])["w"]
        else:
            z = self.find_zone()
        self.say(f"Green square at about ({z[0]:.2f}, {z[1]:.2f}); carrying the object there.")
        s = self.st()
        h = heading_of(z[0] - s["x"], z[1] - s["y"])     # drop heading, chosen once

        def pose_for(z):
            fx, fy = fwd(h)
            rx, ry = fwd(h + 90)
            return (z[0] - fx * CARRY_AHEAD - rx * CARRY_SIDE, z[1] - fy * CARRY_AHEAD - ry * CARRY_SIDE)
        p = pose_for(z)
        if self.cmap is not None and self.go_point(p, tol=0.06, final_heading=h):
            r = self.drop()
            self.back_off()
            return r.get("in_zone", False)
        for _ in range(8):
            s = self.st()
            dist = math.hypot(p[0] - s["x"], p[1] - s["y"])
            if dist < 0.06:
                break
            via = self.blocker((s["x"], s["y"]), p)
            if via is not None:
                self.say("Something is on the way to the green square; going round it.")
                self.face_point(via)
                self.move(math.dist((s["x"], s["y"]), via))
                continue
            self.face_point(p)
            self.move(min(dist, 1.2))
            if dist > 1.6:                          # still far: look again for a better estimate
                zs = [d for d in self.see() if d["shape"] == "zone" and "w" in d and d["area"] > 600]
                if zs:
                    z = max(zs, key=lambda d: d["area"])["w"]
                    p = pose_for(z)
        s = self.st()
        self.turn(h - s["heading"])
        r = self.drop()
        self.back_off()
        return r.get("in_zone", False)

    def orbit(self, tgt, loops, sweep=None, finish_facing=False):
        surf, c, rad = self.measure(tgt)
        if surf > 1.6:
            surf, c, rad = self.approach(tgt, 1.0)
        holding = self.s["holding"]
        R = rad + ROBOT_R + 0.28 + (0.42 if holding else 0.0)
        cw = not holding                         # a carried object rides on the right: keep it outside
        key = (tgt["color"], tgt["shape"])
        for k, (o, r) in self.map.items():         # widen the circle past anything sitting on it
            d = math.dist(o, c)
            if k != key and abs(d - R) < r + ROBOT_R + 0.12 + (0.32 if holding else 0) and d + r + 0.4 < 1.6:
                R = d + r + ROBOT_R + 0.15 + (0.32 if holding else 0)
        s = self.st()
        v = (s["x"] - c[0], s["y"] - c[1])
        n = math.hypot(*v) or 1.0
        start = (c[0] + v[0] / n * R, c[1] + v[1] / n * R)
        self.say(f"{tgt['name'].capitalize()} centre at about ({c[0]:.2f}, {c[1]:.2f}); "
                 f"orbit radius {R:.2f} m, {'clockwise' if cw else 'anticlockwise'}.")
        self.go_point(start, tol=0.04, skip=key)
        s = self.st()
        h = heading_of(c[0] - s["x"], c[1] - s["y"]) + (-90 if cw else 90)
        self.turn(h - s["heading"])
        total = sweep if sweep is not None else 360.0 * loops
        done = 0.0
        for _ in range(12):
            if done >= total - 0.5:
                break
            seg = min(360.0, total - done)
            r = self.cl.arc(R, seg if cw else -seg, self.speed)
            done += math.degrees(r["moved"] / R)
            if r.get("collided"):
                self.say("Something is on the circle; widening the orbit by 0.25 m.")
                self.cl.turn(-90 if cw else 90)
                self.cl.move(0.25)
                self.cl.turn(90 if cw else -90)
                R += 0.25
        if finish_facing:
            self.cl.turn(90 if cw else -90)
        return done

    def inspect(self, tgt, views=4):
        """Look at the object from `views` evenly spaced sides, facing it each time."""
        surf, c, rad = self.measure(tgt)
        R = rad + ROBOT_R + 0.45
        s = self.st()
        n = math.hypot(s["x"] - c[0], s["y"] - c[1]) or 1.0
        start = (c[0] + (s["x"] - c[0]) / n * R, c[1] + (s["y"] - c[1]) / n * R)
        self.go_point(start, tol=0.04, skip=(tgt["color"], tgt["shape"]))
        s = self.st()
        to_c = heading_of(c[0] - s["x"], c[1] - s["y"])
        self.turn(to_c - s["heading"])
        seen = []
        for k in range(views):
            dets = [d for d in self.see() if self._matches(d, tgt)]
            seen.append(bool(dets))
            if k < views - 1:
                self.cl.turn(-90)                     # object on the right, then arc to the next side
                self.cl.arc(R, 360.0 / views)
                self.cl.turn(90)
        return sum(seen)

    def avoid_ahead(self):
        d = self.cl.depth()
        if d is None or d > 2.0:
            raise Fail("no obstacle ahead to go around")
        s = self.st()
        fx, fy = fwd(s["heading"])
        R = 0.75
        c = (s["x"] + fx * (ROBOT_R + d + 0.2), s["y"] + fy * (ROBOT_R + d + 0.2))
        self.move(max(0.0, d + 0.2 + ROBOT_R - R))
        self.cl.turn(-90)
        self.cl.arc(R, 180)
        self.cl.turn(-90)
        return c

    # ----------------------------------------------------------------- main
    def objects(self):
        out = []
        for m in OBJ.finditer(self.text):
            colour, shape = m.group(1), SHAPES[m.group(2)]
            colour = "grey" if colour == "gray" else colour
            if colour == "green":
                if m.group(2) in ("box", "block") or ZONE.match(self.text, m.start()):
                    continue                          # "the green block/box" is the drop zone, not an object
                out.append({"color": "green", "shape": shape, "name": f"green {shape}", "zone_colour": True})
                continue
            out.append({"color": colour, "shape": shape, "name": f"{colour + ' ' if colour else ''}{shape}"})
        # "the red pyramid", "fetch the star": a thing we have no shape for is still a requirement
        for m in re.finditer(rf"(?:\b({COLOURS})\s+|\b(?:the|a|an|that|this)\s+)(?=([a-z]+)\b)", self.text):
            word = m.group(2)
            base = word[:-1] if word.endswith("s") and word[:-1] in SHAPE_WORDS | NOT_OBJECT else word
            if base in SHAPE_WORDS or base in NOT_OBJECT or base in COMMON or re.fullmatch(COLOURS, base) \
                    or base in VOCAB or ZONE.match(self.text, m.start()):
                continue
            if not m.group(1) and not re.search(r"\b(bring|fetch|carry|deliver|pick|grab|lift|get|find|locate|"
                                                r"circle|orbit|inspect|approach)\b", self.text[:m.start()]):
                continue                              # "the" + noun with no fetch/find verb before it: not a target
            if any(o["shape"] for o in out) and not m.group(1):
                continue
            colour = m.group(1)
            if colour == "green" and base in ("square", "zone"):
                continue
            out.append({"color": colour, "shape": None, "unknown": base,
                        "name": f"{colour + ' ' if colour else ''}{base}"})
        if not out and not re.search(r"\bround\b", self.text):
            m = re.search(rf"\b({COLOURS})\s+(?:thing|object|one|item)s?\b", self.text)
            if m and m.group(1) != "green":            # "the orange thing": colour only, any shape
                out.append({"color": m.group(1), "shape": None, "name": f"{m.group(1)} object"})
        if not out and re.search(r"\bround\b", self.text):             # "the round red thing" = a sphere
            c = re.search(rf"\b({COLOURS})\b", self.text)
            colour = c.group(1) if c else None
            out.append({"color": colour, "shape": "sphere", "name": f"{colour + ' ' if colour else ''}sphere"})
        if not out and re.search(r"\b(drink|drinks|soda|beverage|juice|thirsty)\b", self.text):
            out.append({"color": None, "shape": "cylinder", "name": "drink can"})   # a can is the drink
        m = re.search(r"\b(" + "|".join(SUPERLATIVES) + r")\b", self.text)
        if m:
            if out and not out[0].get("unknown") and not out[0].get("zone_colour"):
                out[0] = {**out[0], "superlative": m.group(1), "name": f"{m.group(1)} {out[0]['name']}"}
            elif not out:
                c = re.search(rf"\b({COLOURS})\b", re.sub(r"\bgreen\s+(square|zone|area|box|block)\b", " ", self.text))
                colour = c.group(1) if c else None
                out.append({"color": colour, "shape": None, "superlative": m.group(1),
                            "name": f"{m.group(1)} {colour + ' ' if colour else ''}object"})
        return out

    def loops(self):
        for k, v in COUNT.items():
            if k in self.text:
                return v
        m = re.search(r"(\d+)\s*(times|loops|rounds)", self.text)
        return int(m.group(1)) if m else 1

    def run(self):
        try:
            return True, self._run()
        except Fail as e:
            return False, str(e)

    def _seen(self):
        if not self.map:
            self.see()
        return sorted({f"{c} {sh}" for c, sh in self.map})

    def _check_exists(self, objs):
        """Never substitute: a colour the camera cannot see, a shape it has no name for, or a combination
        that is not in the room -> say so and do nothing."""
        for o in objs:
            if o.get("unknown"):
                seen = self._seen()
                raise Fail(f"Not present: there is no {o['name']} here. I can only recognise cubes, spheres, "
                           f"cylinders (cans) and cones, and I see {', '.join(seen) if seen else 'nothing like it'}. "
                           "Nothing was picked up.")
            if o.get("zone_colour"):
                seen = self._seen()
                raise Fail(f"Not present: there is no {o['name']}; the only green thing is the drop square. "
                           f"I see {', '.join(seen) if seen else 'no objects yet'}. Nothing was picked up.")
            if o["color"] and o["color"] not in SEEABLE:
                seen = sorted({f"{c} {sh}" for c, sh in self.map}) if self.map else []
                if not seen:
                    self.see()
                    seen = sorted({f"{c} {sh}" for c, sh in self.map})
                raise Fail(f"Not present: I can't find a {o['name']}. I only recognise red, blue, yellow, orange "
                           f"and green "
                           f"things, and I see {', '.join(seen) if seen else 'nothing like it'}. "
                           "Nothing was picked up.")

    def resolve_superlative(self, spec):
        """'the tallest object', 'the cube farthest from the green square': look at everything first
        (camera only), then compare the estimates. Returns a concrete colour + shape target."""
        key, pick = SUPERLATIVES[spec["superlative"]]
        self.say(f"To find the {spec['name']} I first look at every object, then compare.")
        if self.cmap is not None:
            explore.explore_nbv(self, goal=0.97)
        if len(self.map) < 2:                     # the map was already complete from earlier tasks:
            for _ in range(8):                    # this task's own look round the room
                self.see()
                self.turn(45)
        cands = [(k, v) for k, v in self.map.items()
                 if (spec["color"] is None or k[0] == spec["color"]) and (spec["shape"] is None or k[1] == spec["shape"])]
        if not cands:
            seen = sorted({f"{c} {sh}" for c, sh in self.map})
            raise Fail(f"Not present: there is no {spec['name'].split(' ', 1)[1]} here. "
                       f"I saw {', '.join(seen) if seen else 'no objects'}. Nothing was picked up.")
        if key == "zone":
            if self.zone_w is None:
                self.find_zone()
            ref = self.zone_w

        def metric(kv):
            k, (c, rad) = kv
            hs = sorted(self.heights.get(k, [])) or [2 * rad]
            h = hs[len(hs) // 2]
            if key == "height":
                return h
            if key == "volume":
                return {"cube": (2 * rad) ** 3, "sphere": math.pi / 6 * (2 * rad) ** 3,
                        "cylinder": math.pi * rad ** 2 * h}.get(k[1], math.pi * rad ** 2 * h / 3)
            return math.dist(c, ref)
        vals = {kv[0]: metric(kv) for kv in cands}
        best = pick(vals.values())
        ties = [k for k, v in vals.items() if abs(v - best) <= 0.02 * max(abs(best), 1e-9)]
        s = self.st()
        k = min(ties, key=lambda k: math.dist(self.map[k][0], (s["x"], s["y"])))
        unit = "m" if key != "volume" else "m^3"
        self.say("Compared: " + ", ".join(f"{c} {sh} {v:.2f} {unit}" for (c, sh), v in sorted(vals.items(), key=lambda kv: -kv[1]))
                 + f" -> the {spec['superlative']} is the {k[0]} {k[1]}.")
        return {"color": k[0], "shape": k[1], "name": f"{k[0]} {k[1]}"}

    def _run(self):
        if re.match(r"\s*mission\b", self.text):
            import mission_brain
            return mission_brain.run_mission(self, self.text)
        self._check_exists(self.objects())
        t = self.text.split(" (update:")[0] if "(update:" in self.text else self.text
        upd = self.text[len(t):]
        if upd:
            t = t + " " + upd                       # refinements ("slowly", "three times") count too
        objs = self.objects()
        if objs and objs[0].get("superlative"):
            objs[0] = self.resolve_superlative(objs[0])
        s = self.st()
        holding = s["holding"]
        if holding and self.context:
            self.say("I am still holding the object from the previous instruction.")

        if self.cmap is None and re.search(r"\b(explore|map)\b", t):
            for _ in range(8):
                self.see()
                self.turn(45)
            return "Looked all the way round."
        if self.cmap is not None and re.search(r"\b(explore|map)\b", t):
            why = explore.explore_nbv(self, goal=0.95)
            st = self.cmap.stats()
            return (f"Explored: {st['observed_m2']:.1f} m^2 seen of about {st['room_est_m2']:.1f} m^2 "
                    f"({st['coverage_est']:.0%}), {st['observations']} views, redundancy {st['redundant']:.0%} ({why}).")
        if objs and re.search(r"\b(find|locate|look for|search for|where is|where's|spot)\b", t):
            surf, c, rad = self.measure(objs[0])
            return f"Found the {objs[0]['name']} about {surf:.2f} m ahead, centre near ({c[0]:.2f}, {c[1]:.2f})."
        if objs and re.search(r"\binspect\b", t):
            n = self.inspect(objs[0])
            return f"Inspected the {objs[0]['name']} from 4 sides (in view {n} of 4 times)."
        if re.search(r"\b(drop|put (it )?down|release|let go|set it down)\b", t) and not objs and not ZONE.search(t):
            self.drop()
            return "Put the object down."
        m = re.search(r"\b(?:rotate|turn|spin)\b(?: around| round)?(?: by)? (-?\d+(?:\.\d+)?)\s*(?:deg|degrees|°)?(?: to the)?( left| right)?", t)
        if m or re.search(r"\bturn (a)?round\b|\babout[- ]face\b", t):
            deg = float(m.group(1)) if m else 180.0
            if m and m.group(2) == " left":
                deg = -deg
            left = deg
            while abs(left) > 0.5:
                step = max(-180.0, min(180.0, left))
                self.cl.turn(step, self.speed)
                left -= step
            return f"Rotated {deg:.0f} degrees."
        m = re.search(r"\b(?:move|go|drive|back)?\s*(back(?:wards?)?|reverse|forwards?|ahead)\b(?: by)? (\d+(?:\.\d+)?)\s*(?:m|metres?|meters?)\b", t)
        if m:
            d = float(m.group(2)) * (1 if m.group(1).startswith(("forward", "ahead")) else -1)
            r = self.move(d)
            return f"Moved {r['moved']:+.2f} m."
        if re.search(r"\bobstacle", t) and re.search(r"\baround|avoid|past\b", t):
            self.avoid_ahead()
            return "Went around the obstacle ahead."
        if objs and re.search(r"\b(circle|orbit|loop|around|round)\b", t) and not re.search(r"\b(bring|fetch|carry|deliver)\b", t):
            n = self.loops()
            done = self.orbit(objs[0], n)
            return f"Circled the {objs[0]['name']} {done / 360:.2f} times ({done:.0f} degrees)."
        if objs and re.search(r"\bbehind\b", t):
            self.orbit(objs[0], 0, sweep=180.0, finish_facing=True)
            return f"Moved behind the {objs[0]['name']}."
        dest_zone = ZONE.search(re.sub(r"\b(from|than|of)\s+the\s+green\s+\w+", " ", t))   # "farthest from the green square" is not a destination
        verb = re.search(r"\b(bring|fetch|take|carry|deliver|put|place|move|drop|leave|set|keep|get)\b", t)
        fetch = verb and re.search(r"\bto\b|\binto\b|\bonto\b|\bon\b|\bin\b|\bat\b|\bnext to\b|\bnear\b|\bbeside\b"
                                   r"|\bby\b|\bclose to\b|\balongside\b", t)
        if dest_zone and not fetch and verb:
            fetch = True                           # "pick up the cone and the green tile" style: a destination was named
        if objs and len(objs) == 1 and not dest_zone and re.search(r"\b(bring|fetch)\b", t):
            fetch, dest_zone = True, True          # "bring me X": no destination named -> the drop zone
        if holding and not objs and dest_zone and verb:
            # "now put it on the green tile": the object is already in the hand
            self.say("Taking what I am holding to the green square.")
            if not self.deliver_zone():
                raise Fail("dropped the object but outside the green square")
            return "Put the object I was holding on the green square."
        if objs and fetch and (dest_zone or len(objs) > 1):
            tgt = objs[0]
            told = tgt["name"] in self.context and holding
            if holding and not told:
                self.put_down_here()
            if not dest_zone and len(objs) > 1 and (objs[1]["color"], objs[1]["shape"]) not in self.map:
                # "put X next to Y": find Y first with an empty hand (searching with a load swings it into
                # walls not mapped yet), and refuse before touching anything if Y is not here
                self.say(f"First finding the {objs[1]['name']}, before picking anything up.")
                if self.find(objs[1]) is None:
                    self.measure(objs[1])                 # raises the honest "Not present"
                self.lock = None
            if not (holding and told):
                self.pick(tgt)
            if dest_zone:
                ok = self.deliver_zone()
                if not ok:
                    raise Fail(f"dropped the {tgt['name']} but outside the green square")
                return f"Brought the {tgt['name']} to the green square."
            dst = objs[1]
            for attempt in range(3):
                surf, c, rad = self.approach(dst, 0.55)
                s = self.st()
                fx, fy = fwd(s["heading"])
                rx, ry = fwd(s["heading"] + 90)
                hand = (s["x"] + fx * CARRY_AHEAD + rx * CARRY_SIDE, s["y"] + fy * CARRY_AHEAD + ry * CARRY_SIDE)
                gap = math.dist(hand, c) - rad - self.carry_r
                # close up the shape is reliable: is this really the object asked for?
                here = [d for d in self.see() if d["color"] == dst["color"] and "w" in d and math.dist(d["w"], c) < 0.5
                        and d["shape"] is not None]
                if dst["shape"] and here and all(d["shape"] != dst["shape"] for d in here):
                    self.say(f"Close up this is a {dst['color']} {here[0]['shape']}, not the {dst['name']}; searching again.")
                    self.map.pop((dst["color"], here[0]["shape"]), None)
                    self.map.pop((dst["color"], dst["shape"]), None)
                    self.lock = None
                    self.cl.move(-0.3)
                    continue
                if gap <= 0.3:                            # the load will stand right next to it
                    break
                self.say(f"Still {gap:.2f} m from the {dst['name']}; closing in before putting it down.")
            else:
                raise Fail(f"could not get next to the {dst['name']} (still holding the {tgt['name']})")
            self.drop()
            self.back_off()
            return f"Put the {tgt['name']} next to the {dst['name']} ({max(0.0, gap):.2f} m apart)."
        if objs and re.search(r"\b(pick|grab|get|lift|collect)\b", t):
            if holding:
                self.put_down_here()
            self.pick(objs[0])
            return f"Picked up the {objs[0]['name']}."
        if objs and re.search(r"\b(go|move|drive|head|approach|come|navigate|stop|face|find|look|towards?|closer)\b", t):
            stand = 0.3 if re.search(r"in front|stop|closer|approach", t) else 0.35
            surf, _, _ = self.approach(objs[0], stand)
            return f"Stopped {surf:.2f} m in front of the {objs[0]['name']}."
        if dest_zone and re.search(r"\b(go|move|drive|head|navigate|towards?|to)\b", t):
            z = self.find_zone()
            self.go_point(z, tol=0.08)
            return "Moved to the green square."
        if re.search(r"\bcloser\b", t):
            d = self.cl.depth()
            self.move(min(0.5, d - 0.25) if d else 0.3)
            return "Moved closer."
        raise Fail("the local planner does not understand this command; "
                   "set ANTHROPIC_API_KEY to use the Claude agent for open-ended language")
