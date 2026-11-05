"""Three extra missions on the same engine: Brain Puzzle (ramp + platform), Colour Sorting, Maze Explorer.

Each mission builds its scene from a seed and a config (validated first), and owns a private evaluator
that reads simulator truth only. Nothing in this module is ever sent to the agent port: the agent gets
the same camera, depth, scan and actions as in the fetch task, plus the instruction text.

    m = create("colour_sort", seed=1)
    cfg = m.scene()                       # level dict for World(cfg=...)
    m.on_drop(robot, obj, z_before)       # -> outcome when the mission is decided, else None
    m.after_action(robot)                 # -> outcome when decided (exit reached, limits), else None
    m.status()                            # verified facts for the operator dashboard (no coordinates)
"""
import math
import random

ROOM = 6.0
HALF = ROOM / 2
GREY = [0.55, 0.55, 0.58]
RGB = {"red": [0.86, 0.10, 0.10], "blue": [0.10, 0.28, 0.88], "green": [0.12, 0.72, 0.22],
       "yellow": [0.96, 0.84, 0.08], "purple": [0.46, 0.22, 0.70], "magenta": [0.92, 0.10, 0.78]}


class ConfigError(ValueError):
    pass


class Mission:
    name = "mission"
    title = ""
    instruction = ""
    DEFAULTS = {}

    def __init__(self, seed=0, config=None):
        self.seed = int(seed)
        self.cfg = {**self.DEFAULTS, **(config or {})}
        self.rng = random.Random(1000 + self.seed)
        self.events = []                  # private event trail (evaluator)
        self.state = "initialising"
        self.outcome = None
        self.validate()

    def _event(self, robot, what, **kw):
        self.events.append({"t": round(robot.stats["sim_seconds"], 2), "event": what, **kw})

    def validate(self):
        pass

    def check_limits(self, robot):
        if robot.stats["actions"] > self.cfg["max_actions"]:
            return "fail_limit_actions"
        if robot.stats["sim_seconds"] > self.cfg["max_sim_s"]:
            return "fail_limit_time"
        return None

    def on_drop(self, robot, obj, z_before):
        return None

    def after_action(self, robot):
        out = self.check_limits(robot)
        if out:
            self._finish(robot, out)
        return out

    def _finish(self, robot, outcome):
        self.outcome = outcome
        self.state = "completed" if outcome == "pass" else "failed"
        self._event(robot, "finished", outcome=outcome)
        return outcome

    def status(self):
        return {"mission": self.name, "title": self.title, "seed": self.seed, "state": self.state,
                "outcome": self.outcome, "events": [e["event"] for e in self.events][-12:]}


def _room(extra=None):
    return {"room": {"size": ROOM, "wall_height": 1.2, "ceiling": True}, **(extra or {})}


# =============================================================================== Brain Puzzle
class BrainPuzzle(Mission):
    """A red cube waits on a purple platform the wheels cannot climb. A yellow ramp lies elsewhere:
    carry it to the platform, set its high end against an edge, drive up, take the cube, come back
    down and bring the cube to the green square."""
    name = "brain_puzzle"
    title = "Brain Puzzle: staircase manipulation"
    instruction = ("Mission brain puzzle: the red cube is on the purple platform. Use the yellow ramp to "
                   "reach it, then bring the red cube to the green square.")
    DEFAULTS = {"platform_size": 1.0, "platform_height": 0.30, "ramp_length": 0.90, "ramp_width": 0.50,
                "lip": 0.15, "align_deg": 35.0, "max_actions": 400, "max_sim_s": 900.0, "max_collisions": 12}

    def validate(self):
        c = self.cfg
        if not 0.1 <= c["platform_height"] <= 0.4:
            raise ConfigError("platform_height must be 0.1-0.4 m (the wheels climb ramps, not risers)")
        if c["platform_height"] / c["ramp_length"] > 0.4:
            raise ConfigError("ramp too steep for the robot (rise/length must be <= 0.4)")
        if c["ramp_width"] < 0.45:
            raise ConfigError("ramp narrower than the robot body (0.4 m) plus margin")
        if c["platform_size"] < 0.8:
            raise ConfigError("platform too small to stand on next to the cube")

    def scene(self):
        c, rng = self.cfg, self.rng
        ps, ph = c["platform_size"], c["platform_height"]
        px, py = 1.5 + rng.uniform(-0.3, 0.3), -1.2 + rng.uniform(-0.4, 0.4)
        self.platform = (px, py, ps / 2)
        # ramp: somewhere on the free floor, any orientation
        for _ in range(500):
            rx, ry = rng.uniform(-2.2, 0.8), rng.uniform(-2.2, 2.2)
            if math.hypot(rx + 2.4, ry) > 1.2 and math.hypot(rx - px, ry - py) > 1.6 and math.hypot(rx + 1.8, ry - 1.8) > 1.3:
                break
        yaw = rng.choice([0.0, 45.0, 90.0, 135.0, 180.0, -45.0, -90.0, -135.0])
        return _room({
            "zone": {"center": [-1.8, 1.8], "size": 1.0, "rgb": RGB["green"]},
            "robot": {"start": [-2.4, 0.0], "heading": 0.0, "radius": 0.2},
            "platforms": [{"pos": [px, py], "size": [ps, ps], "height": ph, "rgb": RGB["purple"], "color_name": "purple"}],
            "objects": [
                {"id": "cube", "shape": "cube", "color_name": "red", "rgb": RGB["red"], "size": 0.22,
                 "pos": [px, py], "z": ph},
                {"id": "ramp", "shape": "ramp", "color_name": "yellow", "rgb": RGB["yellow"],
                 "size_x": c["ramp_length"], "size_y": c["ramp_width"], "height": ph, "lip": c["lip"],
                 "pos": [rx, ry], "yaw": yaw},
            ],
        })

    def _ramp_ok(self, world):
        """Ramp on the floor, high end (plus lip) touching a platform edge, axis square to that edge."""
        ramp = next(o for o in world.objects if o.id == "ramp")
        if ramp.held or ramp.z > 0.01:
            return False
        px, py, h = self.platform
        f = (math.cos(math.radians(ramp.yaw)), -math.sin(math.radians(ramp.yaw)))
        end = (ramp.x + f[0] * ramp.hx, ramp.y + f[1] * ramp.hx)            # high end, centre of its edge
        tip = (ramp.x + f[0] * (ramp.hx + ramp.lip), ramp.y + f[1] * (ramp.hx + ramp.lip))
        plat = world.platforms[0]
        side = (-f[1], f[0])
        tips = [(tip[0] + side[0] * k * ramp.hy, tip[1] + side[1] * k * ramp.hy) for k in (-0.8, 0.0, 0.8)]
        if not any(plat.dist_to_point(*t) <= 0.0 for t in tips) or plat.dist_to_point(*end) <= 0.0:
            return False                                   # the lip must land on the top; the ramp stays off it
        # the edge it meets: the platform side the high end faces
        dx, dy = end[0] - px, end[1] - py
        normal = (1, 0) if abs(dx) >= abs(dy) and dx > 0 else (-1, 0) if abs(dx) >= abs(dy) else (0, 1) if dy > 0 else (0, -1)
        ang = math.degrees(math.acos(max(-1.0, min(1.0, -(f[0] * normal[0] + f[1] * normal[1])))))
        return ang <= self.cfg["align_deg"]

    def after_action(self, robot):
        w = robot.world
        ramp = next(o for o in w.objects if o.id == "ramp")
        cube = next(o for o in w.objects if o.id == "cube")
        have = {e["event"] for e in self.events}
        if robot.held is ramp and "stair_carried" not in have:
            self._event(robot, "stair_carried")
        if "stair_carried" in have and not ramp.held and "stair_placed" not in have and self._ramp_ok(w):
            self._event(robot, "stair_placed")
            self.state = "stair_placed"
        ph = self.platform and w.platforms[0].top
        if robot.z >= ph - 0.01 and "platform_reached" not in have:
            self._event(robot, "platform_reached", via_ramp=self._ramp_ok(w))
            self.state = "platform_reached"
        if robot.held is cube and "cube_carried" not in have:
            self._event(robot, "cube_carried", from_platform=robot.z >= ph - 0.01)
            self.state = "cube_carried"
        if robot.held is cube and robot.z < 0.01 and "cube_carried" in have and "ground_reached" not in have:
            self._event(robot, "ground_reached")
            self.state = "ground_reached"
        return super().after_action(robot)

    def on_drop(self, robot, obj, z_before):
        if obj.id != "cube":
            return None
        have = {e["event"] for e in self.events}
        if not w_in_zone(robot.world, obj):
            self._event(robot, "cube_released_outside_zone", fell=z_before - obj.z > 0.05)
            return None
        ok = {"stair_placed", "platform_reached", "cube_carried", "ground_reached"} <= have
        self._event(robot, "cube_delivered" if ok else "delivery_rejected")
        return self._finish(robot, "pass" if ok else "fail_invalid_route")

    def status(self):
        st = super().status()
        have = {e["event"] for e in self.events}
        st["checks"] = {k: k in have for k in ("stair_carried", "stair_placed", "platform_reached", "cube_carried",
                                               "ground_reached", "cube_delivered")}
        return st


def w_in_zone(world, obj):
    return world.in_zone(obj.x, obj.y) and obj.z < 0.02


# =============================================================================== Colour Sorting
class ColourSort(Mission):
    """Four corner squares (red, blue, green, yellow) and five boxes (one red, blue, green, two yellow):
    every box to the square of its own colour."""
    name = "colour_sort"
    title = "Colour Sorting"
    instruction = ("Mission colour sorting: put every box on the square of the same colour in the corners "
                   "(there are five boxes).")
    DEFAULTS = {"zone_size": 1.0, "box_size": 0.25, "boxes": {"red": 1, "blue": 1, "green": 1, "yellow": 2},
                "spawn_margin": 0.8, "max_actions": 900, "max_sim_s": 1500.0, "max_collisions": 12}
    CORNERS = {"red": (-1, 1), "blue": (1, 1), "green": (1, -1), "yellow": (-1, -1)}

    def validate(self):
        c = self.cfg
        if any(col not in self.CORNERS for col in c["boxes"]):
            raise ConfigError("every box colour needs a corner square (red, blue, green, yellow)")
        if not 0.6 <= c["zone_size"] <= 1.4:
            raise ConfigError("zone_size must be 0.6-1.4 m")
        if sum(c["boxes"].values()) > 8:
            raise ConfigError("at most 8 boxes fit with the spawn spacing")

    def scene(self):
        c, rng = self.cfg, self.rng
        zs = c["zone_size"]
        k = HALF - zs / 2 - 0.05
        zones = [{"color_name": col, "center": [sx * k, sy * k], "size": zs, "rgb": RGB[col]}
                 for col, (sx, sy) in self.CORNERS.items()]
        objs, placed = [], []
        n = 0
        for col, cnt in c["boxes"].items():
            for _ in range(cnt):
                for _try in range(3000):
                    x, y = rng.uniform(-2.2, 2.2), rng.uniform(-2.2, 2.2)
                    if math.hypot(x, y) < 0.9:                          # robot starts in the middle
                        continue
                    if any(abs(x - z["center"][0]) < zs / 2 + 0.35 and abs(y - z["center"][1]) < zs / 2 + 0.35 for z in zones):
                        continue
                    if any(math.hypot(x - a, y - b) < c["spawn_margin"] for a, b in placed):
                        continue
                    break
                else:
                    raise ConfigError("could not place the boxes without overlap")
                placed.append((x, y))
                n += 1
                objs.append({"id": f"box{n}", "shape": "cube", "color_name": col, "rgb": RGB[col],
                             "size": c["box_size"], "pos": [x, y], "yaw": rng.uniform(-30, 30)})
        self.total = n
        self.done = set()
        self.wrong = 0
        return _room({"zones": zones, "robot": {"start": [0.0, 0.0], "heading": 0.0, "radius": 0.2},
                      "objects": objs})

    def on_drop(self, robot, obj, z_before):
        z = robot.world.zone_at(obj.x, obj.y)
        if z is not None and z["color_name"] == obj.color_name and obj.z < 0.02:
            self.done.add(obj.id)
            self._event(robot, "box_delivered", color=obj.color_name)
        else:
            self.done.discard(obj.id)
            if z is not None:
                self.wrong += 1
                self._event(robot, "wrong_square", color=obj.color_name, square=z["color_name"])
        self.state = f"{len(self.done)}/{self.total} delivered"
        if len(self.done) == self.total:
            return self._finish(robot, "pass")
        return None

    def status(self):
        st = super().status()
        st["delivered"] = len(getattr(self, "done", ()))
        st["total"] = getattr(self, "total", 0)
        st["wrong_drops"] = getattr(self, "wrong", 0)
        return st


# =============================================================================== Maze Explorer
class Maze(Mission):
    """A grid maze of low grey walls; somewhere is a magenta exit tile. Find it from the robot's own
    camera and range scan only."""
    name = "maze"
    title = "Maze Explorer"
    instruction = "Mission maze: explore the maze and stop on the magenta exit tile."
    DEFAULTS = {"cells": 5, "wall_height": 0.6, "wall_thickness": 0.08, "loops": 0, "max_actions": 700,
                "max_sim_s": 1500.0, "exit_size": 0.8, "max_collisions": 12}

    def validate(self):
        c = self.cfg
        if not 3 <= c["cells"] <= 6:
            raise ConfigError("cells must be 3-6 (corridors narrower than ~1 m do not fit the robot)")
        if ROOM / c["cells"] - c["wall_thickness"] < 0.95:
            raise ConfigError("corridors too narrow for the robot to turn")
        if c["wall_height"] < 0.55:
            raise ConfigError("walls must be taller than the camera (0.5 m) so the maze cannot be seen over")

    def scene(self):
        c, rng = self.cfg, self.rng
        n = c["cells"]
        cell = ROOM / n
        self.cell, self.n = cell, n
        centre = lambda i, j: (-HALF + cell * (i + 0.5), -HALF + cell * (j + 0.5))  # noqa: E731
        # perfect maze by randomised depth-first search, then optional extra openings (loops)
        open_e = set()
        seen = {(0, 0)}
        stack = [(0, 0)]
        while stack:
            i, j = stack[-1]
            nb = [(i + di, j + dj) for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1))
                  if 0 <= i + di < n and 0 <= j + dj < n and (i + di, j + dj) not in seen]
            if not nb:
                stack.pop()
                continue
            nxt = rng.choice(nb)
            open_e.add(frozenset(((i, j), nxt)))
            seen.add(nxt)
            stack.append(nxt)
        walls_all = [frozenset(((i, j), (i + 1, j))) for i in range(n - 1) for j in range(n)] + \
                    [frozenset(((i, j), (i, j + 1))) for i in range(n) for j in range(n - 1)]
        closed = [e for e in walls_all if e not in open_e]
        rng.shuffle(closed)
        for e in closed[:c["loops"]]:
            open_e.add(e)
        self.open_edges = open_e
        # start: a random cell; exit: the border cell farthest from it along the corridors
        start = (rng.randrange(n), rng.randrange(n))
        dist = {start: 0}
        q = [start]
        while q:
            a = q.pop(0)
            for e in open_e:
                if a in e:
                    b = next(iter(e - {a}))
                    if b not in dist:
                        dist[b] = dist[a] + 1
                        q.append(b)
        if len(dist) != n * n:
            raise ConfigError("maze is not connected")
        border = [k for k in dist if k[0] in (0, n - 1) or k[1] in (0, n - 1)]
        goal = max(border, key=lambda k: (dist[k], k))
        self.start_cell, self.exit_cell, self.route_len = start, goal, dist[goal]
        t, h = c["wall_thickness"], c["wall_height"]
        obstacles = []
        for e in walls_all:
            if e in open_e:
                continue
            (i1, j1), (i2, j2) = sorted(e)
            x1, y1 = centre(i1, j1)
            if i2 != i1:      # wall between columns: vertical (along y)
                obstacles.append({"shape": "cube", "rgb": GREY, "size": cell, "size_x": t, "size_y": cell + t,
                                  "height": h, "pos": [x1 + cell / 2, y1]})
            else:
                obstacles.append({"shape": "cube", "rgb": GREY, "size": cell, "size_x": cell + t, "size_y": t,
                                  "height": h, "pos": [x1, y1 + cell / 2]})
        sx, sy = centre(*start)
        # face an open corridor so the first look is useful
        heading = 0.0
        for (di, dj), hd in (((1, 0), 0.0), ((0, -1), 90.0), ((-1, 0), 180.0), ((0, 1), -90.0)):
            if frozenset((start, (start[0] + di, start[1] + dj))) in open_e:
                heading = hd
                break
        ex, ey = centre(*goal)
        return _room({"zones": [], "zone": {"center": [1e3, 1e3], "size": 0.0, "rgb": RGB["green"]},
                      "robot": {"start": [sx, sy], "heading": heading, "radius": 0.2},
                      "exit": {"center": [ex, ey], "size": c["exit_size"], "rgb": RGB["magenta"]},
                      "obstacles": obstacles, "objects": []})

    def cell_of(self, x, y):
        return (min(self.n - 1, max(0, int((x + HALF) / self.cell))), min(self.n - 1, max(0, int((y + HALF) / self.cell))))

    def after_action(self, robot):
        visited = self.__dict__.setdefault("visited", set())
        visited.add(self.cell_of(robot.x, robot.y))
        if robot.world.in_exit(robot.x, robot.y):
            self._event(robot, "exit_reached", cells_visited=len(visited))
            return self._finish(robot, "pass")
        self.state = f"{len(visited)}/{self.n * self.n} cells visited"
        return super().after_action(robot)

    def status(self):
        st = super().status()
        st["cells_visited"] = len(self.__dict__.get("visited", ()))
        st["cells"] = self.n * self.n
        st["shortest_route_cells"] = self.route_len if self.outcome else None   # shown after the run only
        return st


REGISTRY = {"brain_puzzle": BrainPuzzle, "colour_sort": ColourSort, "maze": Maze}


def create(name, seed=0, config=None):
    if name not in REGISTRY:
        raise ConfigError(f"unknown mission {name!r}; choose one of {', '.join(REGISTRY)}")
    return REGISTRY[name](seed, config)
