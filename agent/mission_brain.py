"""Local solvers for the three missions (Brain Puzzle, Colour Sorting, Maze Explorer).

They drive the robot through the agent API only (/state, /look, /depth, /scan, actions), like the
fetch planner: camera colour blobs, the range scan, the robot's own map and the A*/Dijkstra planner.
Nothing comes from the scene files or the engine's mission evaluator.
"""
import itertools
import math

import explore
from local_brain import CARRY_AHEAD, CARRY_SIDE, Fail, fwd, heading_of, wrap
from perception import ground_point

BOX_COLOURS = ("red", "blue", "green", "yellow")


def _scan_at(scan, bearing):
    """Range of the scan ray nearest a bearing (deg, right positive), or None."""
    rs = (scan.get("ranges") if isinstance(scan, dict) else scan) or []
    if not rs:
        return None
    n = len(rs)
    i = int(round((bearing + 45.0) / 90.0 * (n - 1)))
    vals = [rs[k] for k in range(max(0, i - 1), min(n, i + 2)) if rs[k] is not None]
    return min(vals) if vals else None


def _cam_dist(det):
    """Distance from the camera to where the blob meets the floor (None if its bottom is cut)."""
    if det.get("cut_bottom"):
        return None
    gp = ground_point(det["u"], det["v_bottom"])
    return None if gp is None else math.hypot(gp[0], gp[1] - 0.2)


def _solid(det, scan):
    """The range scan hits solid things, not paint: a blob whose floor contact matches the scan
    range at its bearing is a box; one the scan passes is a floor marking. Across the blob's width
    too (left, centre, right), so a square with a box standing on it still reads as paint."""
    from perception import bearing
    d = _cam_dist(det)
    x0, _, x1, _ = det["bbox"]
    span = x1 - x0
    rays = [det["bearing"], bearing(x0 + 0.25 * span), bearing(x1 - 0.25 * span)]
    rs = [_scan_at(scan, b) for b in rays]
    if any(r is None for r in rs):
        return False
    if d is None:
        return all(r < 0.5 for r in rs)
    return all(abs(r - d) < 0.3 for r in rs)


# =============================================================================== Maze
class MazeSolver:
    DIRS = {(1, 0): 0.0, (0, -1): 90.0, (-1, 0): 180.0, (0, 1): -90.0}     # heading to the neighbour

    def __init__(self, b, n=5, room=6.0, max_steps=160):
        self.b, self.n, self.cell, self.half = b, n, room / n, room / 2
        self.edges = {}             # frozenset((a, b)) -> "open" | "wall"   (the belief map)
        self.visited, self.exit_cell, self.exit_seen = set(), None, 0
        self.max_steps, self.steps, self.backtrack_m = max_steps, 0, 0.0

    def cell_of(self, x, y):
        k = lambda v: min(self.n - 1, max(0, int((v + self.half) / self.cell)))  # noqa: E731
        return k(x), k(y)

    def centre(self, c):
        return -self.half + self.cell * (c[0] + 0.5), -self.half + self.cell * (c[1] + 0.5)

    def inside(self, c):
        return 0 <= c[0] < self.n and 0 <= c[1] < self.n

    def publish_belief(self):
        self.b.emit("mission", belief={"mission": "maze", "visited": len(self.visited),
                                       "known_open": sum(v == "open" for v in self.edges.values()),
                                       "known_walls": sum(v == "wall" for v in self.edges.values()),
                                       "exit_estimate": self.exit_cell})

    def _look_for_exit(self):
        for d in self.b.see():
            if d["color"] == "magenta" and "w" in d:
                c = self.cell_of(*d["w"])
                if c != self.exit_cell:
                    self.b.say(f"Magenta exit tile seen, about cell {c}.")
                self.exit_cell = c
                self.exit_seen += 1

    def observe(self, c):
        """At a cell centre: for every direction not yet known, face it and read the depth ray. One
        reading also tells how many cells along that corridor are open."""
        b = self.b
        s = b.st()
        order = sorted(self.DIRS.items(), key=lambda kv: abs(wrap(kv[1] - s["heading"])))
        for d, h in order:
            nb = (c[0] + d[0], c[1] + d[1])
            if not self.inside(nb) or frozenset((c, nb)) in self.edges:
                continue
            s = b.st()
            b.turn(wrap(h - s["heading"]))
            depth = b.cl.depth()
            free = 10.0 if depth is None else depth + 0.2                      # from the robot centre
            k, a = 1, c
            while True:
                nxt = (a[0] + d[0], a[1] + d[1])
                if not self.inside(nxt):
                    break
                wall_at = (k - 0.5) * self.cell - 0.04                          # wall face between a and nxt
                e = frozenset((a, nxt))
                if free > wall_at + 0.25:
                    self.edges.setdefault(e, "open")
                else:
                    self.edges.setdefault(e, "wall")
                    break
                a, k = nxt, k + 1
            self._look_for_exit()
        self.publish_belief()

    def neighbours(self, c):
        for d in self.DIRS:
            nb = (c[0] + d[0], c[1] + d[1])
            if self.inside(nb) and self.edges.get(frozenset((c, nb))) == "open":
                yield nb

    def bfs(self, start):
        prev, q = {start: None}, [start]
        while q:
            a = q.pop(0)
            for nb in self.neighbours(a):
                if nb not in prev:
                    prev[nb] = a
                    q.append(nb)
        return prev

    def path_to(self, prev, goal):
        p = [goal]
        while prev[p[-1]] is not None:
            p.append(prev[p[-1]])
        return p[::-1]

    def unknown_dirs(self, c):
        return [d for d in self.DIRS if self.inside((c[0] + d[0], c[1] + d[1]))
                and frozenset((c, (c[0] + d[0], c[1] + d[1]))) not in self.edges]

    def goto_centre(self, c):
        b = self.b
        x, y = self.centre(c)
        s = b.st()
        dist = math.hypot(x - s["x"], y - s["y"])
        if dist > 0.04:
            b.face_point((x, y))
            r = b.cl.move(dist)
            return not r.get("collided")
        return True

    def step_to(self, a, nb):
        """Drive one cell; a bump means the belief was wrong: that edge is a wall."""
        b = self.b
        d = (nb[0] - a[0], nb[1] - a[1])
        s = b.st()
        b.turn(wrap(self.DIRS[d] - s["heading"]))
        x, y = self.centre(nb)
        r = b.cl.move(math.hypot(x - s["x"], y - s["y"]))
        self.steps += 1
        if r.get("collided"):
            self.edges[frozenset((a, nb))] = "wall"
            b.say(f"Blocked between {a} and {nb}: marking a wall and replanning.")
            self.goto_centre(a)
            return False
        self.goto_centre(nb)
        return True

    def run(self):
        b = self.b
        s = b.st()
        self.goto_centre(self.cell_of(s["x"], s["y"]))
        while self.steps < self.max_steps:
            s = b.st()
            if s["name"] == "Done":
                return (f"Reached the exit tile: {len(self.visited)} cells visited, {self.steps} cell moves, "
                        f"{self.backtrack_m:.1f} m of backtracking.")
            c = self.cell_of(s["x"], s["y"])
            first = c not in self.visited
            self.visited.add(c)
            if first or self.unknown_dirs(c):
                self.observe(c)
            prev = self.bfs(c)
            if self.exit_cell is not None and self.exit_cell in prev:
                route = self.path_to(prev, self.exit_cell)
                why = "to the exit"
            else:
                frontier = [k for k in prev if k not in self.visited or self.unknown_dirs(k)]
                frontier = [k for k in frontier if k != c] or ([c] if self.unknown_dirs(c) else [])
                if not frontier:
                    raise Fail(f"Explored every reachable cell ({len(self.visited)}) and found no exit.")
                ex = self.exit_cell

                def cost(k):
                    hop = len(self.path_to(prev, k)) - 1
                    return hop + (0.5 * (abs(k[0] - ex[0]) + abs(k[1] - ex[1])) if ex else 0.0)
                tgt = min(frontier, key=cost)
                route = self.path_to(prev, tgt)
                why = "to the nearest unexplored corridor"
            if len(route) > 2 and why != "to the exit":
                self.backtrack_m += (len(route) - 2) * self.cell
            if len(route) > 1:
                b.say(f"Going {why}: {len(route) - 1} cells.")
            for a, nb in zip(route, route[1:]):
                if not self.step_to(a, nb):
                    break
                if b.st()["name"] == "Done":
                    break
        raise Fail(f"Step limit reached ({self.max_steps} cell moves) without finding the exit.")


# =============================================================================== Colour Sorting
class SortSolver:
    def __init__(self, b, total=5):
        self.b, self.total = b, total
        self.boxes = []             # [{"color", "x", "y", "n", "status"}]  status: NOT_COLLECTED / DELIVERED
        self.zones = {}             # colour -> [sum x, sum y, n]
        self.delivered = 0
        self.dropped = []           # where this robot put boxes down (they are sorted, or will be re-found)
        b.on_see = self._on_see
        b.extra_obstacles = lambda: [((o["x"], o["y"]), 0.15) for o in self.boxes if o["status"] != "DELIVERED"]

    def _on_see(self, dets, s, scan):
        for d in dets:
            if d["color"] not in BOX_COLOURS or "floor_rel" not in d:
                continue
            from perception import to_world
            if _solid(d, scan) and "w" in d and not d["edge"]:
                x, y = d["w"]
                if self._in_zone_area(x, y):
                    continue                          # a box already standing on a square
                near = [o for o in self.boxes if o["color"] == d["color"] and math.dist((o["x"], o["y"]), (x, y)) < 0.6
                        and o["status"] != "DELIVERED"]
                if near:
                    o = near[0]
                    o["x"] = (o["x"] * o["n"] + x) / (o["n"] + 1)
                    o["y"] = (o["y"] * o["n"] + y) / (o["n"] + 1)
                    o["n"] += 1
                else:
                    self.boxes.append({"color": d["color"], "x": x, "y": y, "n": 1, "status": "NOT_COLLECTED"})
            elif not _solid(d, scan) and d["area"] > 900:
                fx, fy = to_world(d["floor_rel"], s["x"], s["y"], s["heading"])
                if abs(fx) > 1.5 and abs(fy) > 1.5:            # paint seen well inside a corner: one vote
                    corner = (1 if fx > 0 else -1, 1 if fy > 0 else -1)
                    votes = self.zones.setdefault(d["color"], {})
                    votes[corner] = votes.get(corner, 0) + d["area"]

    def walls(self):
        """Room half-sizes from the walls the range scan has outlined (the robot's own map)."""
        b = self.b.cmap.bounds if self.b.cmap is not None else {}
        get = lambda k, d: abs(b[k]) if b.get(k) is not None else d  # noqa: E731
        return get("xmin", 3.0), get("xmax", 3.0), get("ymin", 3.0), get("ymax", 3.0)

    def zone_centre(self, colour):
        """The squares are in the corners (the task says so): which corner comes from where the camera
        saw that colour on the floor; the drop point is 0.55 m in from both walls of that corner."""
        votes = self.zones.get(colour)
        if not votes:
            return None
        x, y = max(votes, key=votes.get)                # the corner this colour was seen in most
        x0, x1, y0, y1 = self.walls()
        return ((x1 if x > 0 else -x0) - math.copysign(0.55, x), (y1 if y > 0 else -y0) - math.copysign(0.55, y))

    def _in_zone_area(self, x, y):
        for col in self.zones:
            c = self.zone_centre(col)
            if c and abs(x - c[0]) < 0.85 and abs(y - c[1]) < 0.85:     # boxes never start this close to a square
                return True
        return any(math.dist((x, y), q) < 0.6 for q in self.dropped)  # or where I put one down myself

    @staticmethod
    def _seg_near(a, b, q, r):
        dx, dy = b[0] - a[0], b[1] - a[1]
        L2 = dx * dx + dy * dy or 1e-9
        t = max(0.0, min(1.0, ((q[0] - a[0]) * dx + (q[1] - a[1]) * dy) / L2))
        return math.dist((a[0] + dx * t, a[1] + dy * t), q) < r

    def plan_order(self, pose):
        """Every order of the remaining boxes (5! = 120 at most): driving distance robot -> box -> its
        square -> next box ..., plus 2 m for every carried leg that passes close to a box still on the
        floor (carrying past it is where bumps happen; moving that box first clears the way)."""
        todo = [o for o in self.boxes if o["status"] != "DELIVERED" and self.zone_centre(o["color"])]
        best, best_c = None, None
        for perm in itertools.permutations(todo, min(len(todo), 5)):
            p, c = pose, 0.0
            left = list(perm)
            for o in perm:
                z = self.zone_centre(o["color"])
                c += math.dist(p, (o["x"], o["y"])) + math.dist((o["x"], o["y"]), z)
                left.remove(o)
                c += 3.0 * sum(self._seg_near((o["x"], o["y"]), z, (q["x"], q["y"]), 0.55) for q in left)
                p = z
            if best_c is None or c < best_c:
                best, best_c = perm, c
        return list(best or []), best_c

    def publish_belief(self):
        self.b.emit("mission", belief={"mission": "colour_sort", "boxes": [
            {"color": o["color"], "x": round(o["x"], 2), "y": round(o["y"], 2), "status": o["status"]} for o in self.boxes],
            "squares": {c: [round(v, 2) for v in self.zone_centre(c)] for c in self.zones if self.zone_centre(c)},
            "delivered": self.delivered})

    def deliver(self, colour):
        b = self.b
        z = self.zone_centre(colour)
        s = b.st()
        # face diagonally into the corner: the pose with the most room from both walls
        h = heading_of(math.copysign(1.0, z[0]), math.copysign(1.0, z[1]))
        fx, fy = fwd(h)
        rx, ry = fwd(h + 90)
        p = (z[0] - fx * CARRY_AHEAD - rx * CARRY_SIDE, z[1] - fy * CARRY_AHEAD - ry * CARRY_SIDE)
        if not b.go_point(p, tol=0.08, final_heading=h):
            b.face_point(p)
            s = b.st()
            b.move(math.hypot(p[0] - s["x"], p[1] - s["y"]))
            s = b.st()
            b.turn(wrap(h - s["heading"]))
        r = b.drop()
        b.cl.move(-0.3)
        return r

    def run(self):
        b = self.b
        b.say("Looking at the whole room first: which squares are where, and where the five boxes are.")
        explore.explore_nbv(b, goal=0.95)
        for attempt in range(40):
            todo = [o for o in self.boxes if o["status"] != "DELIVERED"]
            if b.st()["name"] == "Done":
                break
            missing = [c for c in {o["color"] for o in todo} if not self.zone_centre(c)]
            if missing or len(self.boxes) < self.total:
                if attempt > 6:
                    break
                b.say(f"Still missing: {'squares ' + ', '.join(missing) if missing else ''} "
                      f"{'boxes ' + str(self.total - len(self.boxes)) if len(self.boxes) < self.total else ''}; looking more.")
                explore.explore_nbv(b, goal=0.99, max_views=8)
            order, cost = self.plan_order((b.st()["x"], b.st()["y"]))
            if not order:
                if not todo:
                    break
                continue
            self.publish_belief()
            o = order[0]
            b.say(f"Delivery order ({len(order)} left, about {cost:.1f} m): " +
                  " -> ".join(f"{q['color']}" for q in order) + f". Now the {o['color']} box.")
            b.lock = b.track = (o["x"], o["y"])
            b.exclude = [(self.zone_centre(c), 0.85) for c in self.zones if self.zone_centre(c)] + [(q, 0.45) for q in self.dropped]
            try:
                b.pick({"color": o["color"], "shape": "cube", "name": f"{o['color']} box"})
            except Fail as e:
                b.say(f"Could not pick up that {o['color']} box ({e}); trying the plan again.")
                o["n"] = 0
                self.boxes.remove(o)
                b.lock = b.track = None
                continue
            b.lock = b.track = None
            if not b.st()["holding"]:
                continue
            r = self.deliver(o["color"])
            s = b.st()
            hx = s["x"] + fwd(s["heading"])[0] * (CARRY_AHEAD + 0.3) + fwd(s["heading"] + 90)[0] * CARRY_SIDE
            hy = s["y"] + fwd(s["heading"])[1] * (CARRY_AHEAD + 0.3) + fwd(s["heading"] + 90)[1] * CARRY_SIDE
            if r.get("in_zone"):
                self.dropped.append((hx, hy))
                o["status"] = "DELIVERED"
                self.delivered += 1
                zc = self.zone_centre(o["color"])
                self.boxes = [q for q in self.boxes if q is o or q["status"] == "DELIVERED" or q["color"] != o["color"]
                              or math.dist((q["x"], q["y"]), zc) > 1.0]      # earlier double sightings of this one
                for k in [k for k in b.map if k[0] == o["color"]]:
                    b.map.pop(k, None)                    # the memory of where it stood is out of date now
                b.say(f"{o['color'].capitalize()} box is on the {o['color']} square ({self.delivered} done).")
            else:
                s = b.st()                               # it stands where the hand put it down (0.3 m ago)
                fx, fy = fwd(s["heading"])
                rx, ry = fwd(s["heading"] + 90)
                o["x"] = s["x"] + fx * (CARRY_AHEAD + 0.3) + rx * CARRY_SIDE
                o["y"] = s["y"] + fy * (CARRY_AHEAD + 0.3) + ry * CARRY_SIDE
                b.say("The box landed outside the square; it goes back on the list.")
            self.publish_belief()
        s = b.st()
        if s["name"] == "Done":
            return f"All boxes sorted: {self.delivered} deliveries."
        raise Fail(f"Sorted {self.delivered} of {self.total} boxes.")


# =============================================================================== Brain Puzzle
class PuzzleSolver:
    def __init__(self, b):
        self.b = b
        self.purple, self.yellow = [], []
        b.on_see = self._on_see

    def _on_see(self, dets, s, scan):
        for d in dets:
            if "w" in d and not d["edge"] and d["color"] == "purple":
                self.purple.append(d["w"])
            if "w" in d and not d["edge"] and d["color"] == "yellow":
                self.yellow.append(d["w"])

    def _cells_near(self, c, radius):
        cm = self.b.cmap
        out = []
        j0, i0 = (int(v) for v in cm.idx(*c))
        k = int(radius / 0.1) + 1
        for j in range(max(0, j0 - k), min(cm.occ.shape[0], j0 + k + 1)):
            for i in range(max(0, i0 - k), min(cm.occ.shape[1], i0 + k + 1)):
                if cm.occ[j, i] == 2:
                    x, y = cm.centre(j, i)
                    if math.dist((x, y), c) <= radius:
                        out.append((x, y))
        return out

    def platform_box(self):
        if not self.purple:
            raise Fail("I have not seen the purple platform.")
        c = (sum(p[0] for p in self.purple) / len(self.purple), sum(p[1] for p in self.purple) / len(self.purple))
        cells = self._cells_near(c, 0.9)
        if len(cells) < 4:
            raise Fail("The range scan has not outlined the platform yet.")
        xs, ys = [p[0] for p in cells], [p[1] for p in cells]
        return min(xs), max(xs), min(ys), max(ys)

    def ramp_shape(self, plat):
        c = (sum(p[0] for p in self.yellow[-6:]) / len(self.yellow[-6:]), sum(p[1] for p in self.yellow[-6:]) / len(self.yellow[-6:]))
        x0, x1, y0, y1 = plat
        cells = [p for p in self._cells_near(c, 0.75) if not (x0 - 0.15 <= p[0] <= x1 + 0.15 and y0 - 0.15 <= p[1] <= y1 + 0.15)]
        if len(cells) < 5:
            return c, None, 0.9
        mx, my = sum(p[0] for p in cells) / len(cells), sum(p[1] for p in cells) / len(cells)
        sxx = sum((p[0] - mx) ** 2 for p in cells)
        syy = sum((p[1] - my) ** 2 for p in cells)
        sxy = sum((p[0] - mx) * (p[1] - my) for p in cells)
        ang = 0.5 * math.atan2(2 * sxy, sxx - syy)
        ax = (math.cos(ang), math.sin(ang))
        proj = [(p[0] - mx) * ax[0] + (p[1] - my) * ax[1] for p in cells]
        length = max(proj) - min(proj) + 0.1
        return (mx, my), ax, max(0.6, min(1.2, length))

    def survey_ramp(self, c):
        """Look at the ramp from four sides so the scan outlines it completely."""
        b = self.b
        for k in range(4):
            a = math.radians(90 * k + 45)
            p = (c[0] + 1.25 * math.cos(a), c[1] + 1.25 * math.sin(a))
            if abs(p[0]) > 2.6 or abs(p[1]) > 2.6:
                continue
            if b.go_point(p, tol=0.15):
                b.face_point(c)
                b.see()

    def probe_end(self, centre, ax, length, sign):
        """Stand on the ramp axis beyond one end, facing it, and try to drive onto it: if the wheels
        rise this is the low end. Returns True (low), False (high), None (could not get there)."""
        b = self.b
        end = (centre[0] + sign * ax[0] * length / 2, centre[1] + sign * ax[1] * length / 2)
        stand = (end[0] + sign * ax[0] * 0.55, end[1] + sign * ax[1] * 0.55)
        if not b.go_point(stand, tol=0.08):
            return None
        b.face_point(centre)
        r = b.cl.move(0.45)
        s = b.st()
        z = s.get("z", 0.0)
        gap = math.dist((s["x"], s["y"]), end) - 0.2          # robot front to the ramp's end
        b.cl.move(-abs(r.get("moved", 0.0)))
        # a low end lets the front pass its end line (the wheels start to rise); a high end is a riser
        # that stops the front at the line, and the engine then backs off 0.1 m
        if z > 0.005 or gap < -0.05:
            b.say(f"Probed the {'+' if sign > 0 else '-'} end of the ramp: the wheels climbed (low end).")
            return True
        if r.get("collided") and gap < 0.35:
            b.say(f"Probed the {'+' if sign > 0 else '-'} end of the ramp: a step right at its end (high end).")
            return False
        b.say(f"Probed the {'+' if sign > 0 else '-'} end of the ramp: inconclusive (stopped {gap:.2f} m short).")
        return None

    def clear_cells(self, c, radius):
        """The robot moved this thing itself: its old outline is free floor in the map now."""
        cm = self.b.cmap
        for x, y in self._cells_near(c, radius):
            j, i = (int(v) for v in cm.idx(x, y))
            cm.occ[j, i] = 1

    def carry_and_place(self, centre, ax, length, low_sign, plat):
        """Pick the ramp up from its low end (so the high end points ahead), carry it square to the
        nearest usable platform edge and push until it touches; put it down. Returns (centre, heading)."""
        b = self.b
        end = (centre[0] + low_sign * ax[0] * length / 2, centre[1] + low_sign * ax[1] * length / 2)
        stand = (end[0] + low_sign * ax[0] * 0.5, end[1] + low_sign * ax[1] * 0.5)
        if not b.go_point(stand, tol=0.06):
            b.face_point(stand)
            s = b.st()
            b.cl.move(math.dist((s["x"], s["y"]), stand))
        b.face_point(centre)
        ok = b.cl.pickup().get("ok")
        if not ok:
            b.cl.move(0.12)
            ok = b.cl.pickup().get("ok")
        if not ok:
            raise Fail("The ramp is not in reach of the claws.")
        self.clear_cells(centre, length / 2 + 0.25)
        b.say("Ramp in the claws, high end ahead.")
        b.carry_r = 0.5
        b.cl.move(-0.4)                               # pull the load clear of walls before turning with it
        x0, x1, y0, y1 = plat
        pc = ((x0 + x1) / 2, (y0 + y1) / 2)
        s = b.st()
        cands = []
        for mid, n in (((x0 - 0.05, pc[1]), (-1, 0)), ((x1 + 0.05, pc[1]), (1, 0)), ((pc[0], y0 - 0.05), (0, -1)),
                       ((pc[0], y1 + 0.05), (0, 1))):
            h = heading_of(-n[0], -n[1])
            rx, ry = fwd(h + 90)
            back = CARRY_AHEAD + length / 2 + 0.75
            stage = (mid[0] + n[0] * back - rx * CARRY_SIDE, mid[1] + n[1] * back - ry * CARRY_SIDE)
            far = (mid[0] + n[0] * (back + 0.4), mid[1] + n[1] * (back + 0.4))
            if max(abs(stage[0]), abs(stage[1]), abs(far[0]), abs(far[1])) < 2.6:
                cands.append((math.dist(stage, (s["x"], s["y"])), mid, h, stage))
        placed = False
        for _, mid, h, stage in sorted(cands):
            b.say(f"Taking the ramp to the platform edge at ({mid[0]:.2f}, {mid[1]:.2f}).")
            if b.go_point(stage, tol=0.08, final_heading=h):
                placed = True
                break
        if not placed:
            _, mid, h, stage = sorted(cands)[0]
            b.say("No planned route with the ramp; driving there directly.")
            b.face_point(stage)
            s = b.st()
            b.cl.move(math.dist((s["x"], s["y"]), stage))
            s = b.st()
            b.cl.turn(wrap(h - s["heading"]))
        r = b.cl.move(1.4)                            # until the ramp's end touches the platform
        if not r.get("collided"):
            b.say("The ramp did not touch the platform where I expected; putting it down here.")
        b.cl.drop()
        b.carry_r = 0.23
        s = b.st()
        h = s["heading"]
        fx, fy = fwd(h)
        rx, ry = fwd(h + 90)
        rc = (s["x"] + fx * CARRY_AHEAD + rx * CARRY_SIDE, s["y"] + fy * CARRY_AHEAD + ry * CARRY_SIDE)
        b.cl.move(-0.3)
        return rc, h

    def climb(self, rc, h, length):
        """Line up on the ramp's axis behind its low end and drive up. True when on the platform."""
        b = self.b
        fx, fy = fwd(h)
        rx, ry = fwd(h + 90)
        low = (rc[0] - fx * length / 2, rc[1] - fy * length / 2)
        start = (low[0] - fx * 0.55, low[1] - fy * 0.55)
        b.say("Lining up behind the ramp's low end.")
        s = b.st()
        b.face_point(start)
        s = b.st()
        b.cl.move(math.dist((s["x"], s["y"]), start))
        s = b.st()
        b.cl.turn(wrap(h - s["heading"]))
        s = b.st()
        lat = (s["x"] - rc[0]) * rx + (s["y"] - rc[1]) * ry
        if abs(lat) > 0.04:
            b.cl.turn(90 if lat < 0 else -90)
            b.cl.move(abs(lat))
            b.cl.turn(-90 if lat < 0 else 90)
        r = b.cl.move(0.55 + length + 0.35)
        z = b.st().get("z", 0.0)
        if z >= 0.2:
            return True
        b.cl.move(-abs(r.get("moved", 0.0)))           # back down to the floor and line up again
        return False

    def run(self):
        b = self.b
        b.say("Looking around: the platform, the ramp, the cube and the green square.")
        explore.explore_nbv(b, goal=0.9)
        if not self.yellow:
            raise Fail("Not present: I could not find the yellow ramp.")
        plat = self.platform_box()
        yc = (sum(p[0] for p in self.yellow[-6:]) / len(self.yellow[-6:]), sum(p[1] for p in self.yellow[-6:]) / len(self.yellow[-6:]))
        self.survey_ramp(yc)
        centre, ax, length = self.ramp_shape(plat)
        if ax is None:
            raise Fail("Could not measure the ramp's direction from the range scan.")
        b.say(f"Ramp at about ({centre[0]:.2f}, {centre[1]:.2f}), {length:.2f} m long, axis "
              f"{math.degrees(math.atan2(ax[1], ax[0])):.0f} deg. Finding its low end.")
        low_sign, high = None, None
        for sign in (1, -1, 1, -1):
            res = self.probe_end(centre, ax, length, sign)
            if res is True:
                low_sign = sign
                break
            if res is False:
                high = sign
        if low_sign is None and high is not None:
            low_sign = -high                                 # only the high end answered
        if low_sign is None:
            raise Fail("Could not reach either end of the ramp.")
        rc = h = None
        for attempt in range(3):
            rc, h = self.carry_and_place(centre, ax, length, low_sign, plat)
            if self.climb(rc, h, length):
                break
            b.say("The ramp is not usable where it landed; picking it up again.")
            centre, ax, low_sign = rc, fwd(h), -1          # it lies along h, low end towards -h
        else:
            raise Fail("Could not get onto the platform after three ramp placements.")
        fx, fy = fwd(h)
        rx, ry = fwd(h + 90)
        z = b.st().get("z", 0.0)
        b.say(f"On the platform (wheels at {z:.2f} m). Taking the cube.")
        reds = [d for d in b.see() if d["color"] == "red"]
        if reds:
            b.cl.turn(max(d["area"] for d in reds) and max(reds, key=lambda d: d["area"])["bearing"])
        ok = b.cl.pickup().get("ok")
        if not ok:
            d = b.cl.depth()
            if d is not None and d > 0.25:
                b.cl.move(min(0.2, d - 0.2))
            ok = b.cl.pickup().get("ok")
        if not ok:
            raise Fail("The cube is not in reach from the platform.")
        b.carry_r = 0.2
        b.say("Cube in the claws. Turning round and driving back down the ramp.")
        s = b.st()
        b.cl.turn(wrap(heading_of(-fx, -fy) - s["heading"]))
        s = b.st()
        lat = (s["x"] - rc[0]) * rx + (s["y"] - rc[1]) * ry
        r = b.cl.move(length + 0.9)
        z = b.st().get("z", 0.0)
        if z > 0.01:
            raise Fail(f"Did not get back down (wheels at {z:.2f} m).")
        b.say("Back on the floor with the cube; to the green square.")
        if not b.deliver_zone():
            raise Fail("The cube landed outside the green square.")
        return "Ramp placed, platform climbed, cube carried down and delivered to the green square."


def run_mission(b, text):
    import perception
    perception.MISSION_MODE = True            # the purple platform and the magenta exit tile exist here
    t = text.lower()
    if "maze" in t:
        return MazeSolver(b).run()
    if "colour sort" in t or "color sort" in t:
        return SortSolver(b).run()
    if "brain puzzle" in t or "ramp" in t:
        return PuzzleSolver(b).run()
    raise Fail("Unknown mission (brain puzzle, colour sorting, maze).")
