"""The robot's own map, built only from what its sensors report.

Inputs per observation: the pose from /state, a range scan from /scan (horizontal
rays across the camera's 90 deg field of view, distances only) and the published
camera geometry. Nothing is read from the level files or the engine's world.

Two layers are kept apart on purpose:
  occupancy  (range sensor)  unknown / free / occupied: where the robot can drive
  coverage   (camera)        how often, and when, the camera has seen each floor cell
A cell can be free but never seen by the camera (right in front of the bumper, below
the camera's lower edge), or seen but unreachable. "Occluded" marks cells that were
inside the field of view but hidden behind something.

Next-best-view: candidate poses (a 0.3 m lattice of reachable free cells x 12 headings)
are scored by predicted new floor area minus travel time, extra overlap and collision
risk. This is a one-step-lookahead heuristic, not a proven global optimum.
"""
import heapq
import json
import math
import os

import numpy as np

CELL = 0.1                 # m
HALF = 4.0                 # grid spans world -4..4 m; the room size is not assumed
N = int(round(2 * HALF / CELL))
HFOV = 90.0
CAM_AHEAD, CAM_H = 0.2, float(os.environ.get("FETCH_CAM_H", "0.5"))   # spec 0.5 m
CAM_NEAR = CAM_H / math.tan(math.radians(10.0 + 36.87))   # 0.47 m: floor nearer than this is below the image
MAX_RANGE = 4.0            # floor further than this is not counted as reliably observed
ROBOT_R = 0.2
RECENT = 3                 # observations
UNKNOWN, FREE, OCC = 0, 1, 2

WEIGHTS = {                # configurable; tuned with tools/eval_exploration.py
    "time": 0.6,           # m^2 of new floor that one second of travel/turning must be worth
    "overlap": 0.8,        # penalty per m^2 of overlap above `keep_overlap`
    "keep_overlap": 0.25,  # this fraction of each view may repeat at no cost (tracking, consistency)
    "risk": 6.0,           # penalty per metre of clearance below 0.5 m
    "leg": 0.4,            # s of overhead per drive leg (stop, turn, observe)
    "min_gain": 0.25,      # m^2: below this a viewpoint is not worth visiting
    "mode": "utility",     # "utility": gain - time - penalties (default); "rate": (gain - penalties) / (time + view_cost),
                           # measured worse here (more driving, collisions) - kept for comparison
    "view_cost": 1.0,      # s charged per observation (the look itself, and the agent's thinking step)
    "planner": "lattice",  # "lattice": heading-aware Dijkstra costs (planner.py); "grid": the earlier 2-D estimate
}


def fwd(h):
    r = math.radians(h)
    return math.cos(r), -math.sin(r)


def wrap(a):
    return (a + 180.0) % 360.0 - 180.0


def _shift(m, dy, dx):
    out = np.zeros_like(m)
    ys = slice(max(dy, 0), m.shape[0] + min(dy, 0))
    yd = slice(max(-dy, 0), m.shape[0] + min(-dy, 0))
    xs = slice(max(dx, 0), m.shape[1] + min(dx, 0))
    xd = slice(max(-dx, 0), m.shape[1] + min(-dx, 0))
    out[ys, xs] = m[yd, xd]
    return out


def dilate(mask, radius_m):
    k = radius_m / CELL
    out = mask.copy()
    r = int(math.ceil(k))
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if (dx or dy) and dx * dx + dy * dy <= k * k:
                out |= _shift(mask, dy, dx)
    return out


class CoverageMap:
    def __init__(self):
        self.occ = np.zeros((N, N), np.int8)        # [row = y index, col = x index]
        self.cov = np.zeros((N, N), np.int16)
        self.last = np.full((N, N), -1, np.int32)
        self.occl = np.zeros((N, N), bool)
        self.n_obs = 0
        self.history = []
        self.viewpoints = []
        self.objects = {}
        self.bounds = {"xmin": None, "xmax": None, "ymin": None, "ymax": None}
        self.plan = None
        self.counters = {"turns": 0, "turn_deg": 0.0, "wasted_turns": 0, "legs": 0}

    # -------------------------------------------------------------- geometry
    @staticmethod
    def idx(x, y):
        return (np.clip(((np.asarray(y) + HALF) / CELL).astype(int), 0, N - 1),
                np.clip(((np.asarray(x) + HALF) / CELL).astype(int), 0, N - 1))

    @staticmethod
    def centre(j, i):
        return -HALF + (i + 0.5) * CELL, -HALF + (j + 0.5) * CELL

    def room_mask(self):
        """Cells inside the room as far as the walls seen so far bound it."""
        xs = -HALF + (np.arange(N) + 0.5) * CELL
        b = self.bounds
        okx = (xs >= (b["xmin"] if b["xmin"] is not None else -HALF)) & (xs <= (b["xmax"] if b["xmax"] is not None else HALF))
        oky = (xs >= (b["ymin"] if b["ymin"] is not None else -HALF)) & (xs <= (b["ymax"] if b["ymax"] is not None else HALF))
        return oky[:, None] & okx[None, :]

    # ------------------------------------------------------------ integrate
    def integrate(self, x, y, heading, ranges, fov=HFOV, robot_r=ROBOT_R):
        """Add one observation. Returns its coverage statistics."""
        fx, fy = fwd(heading)
        ox, oy = x + fx * robot_r, y + fy * robot_r
        n = len(ranges)
        free_pts, cov_pts, occl_pts, hits = [], [], [], []
        for k, r in enumerate(ranges):
            b = -fov / 2 + fov * k / (n - 1)
            dx, dy = fwd(heading + b)
            rr = 10.0 if r is None else float(r)
            d = np.arange(0.0, min(rr, 6.0), CELL / 2)
            free_pts.append((ox + dx * d, oy + dy * d))
            dc = d[(d >= CAM_NEAR) & (d <= MAX_RANGE)]
            cov_pts.append((ox + dx * dc, oy + dy * dc))
            if r is not None:
                hx, hy = ox + dx * (rr + 0.02), oy + dy * (rr + 0.02)
                hits.append((hx, hy))
                if rr < MAX_RANGE:
                    db = np.arange(rr + CELL, MAX_RANGE, CELL / 2)
                    occl_pts.append((ox + dx * db, oy + dy * db))
                if abs(dx) > 0.85:                      # ray roughly along x: a wall bounds x
                    key = "xmax" if dx > 0 else "xmin"
                    cur = self.bounds[key]
                    self.bounds[key] = (ox + dx * rr) if cur is None else (max(cur, ox + dx * rr) if dx > 0 else min(cur, ox + dx * rr))
                if abs(dy) > 0.85:
                    key = "ymax" if dy > 0 else "ymin"
                    cur = self.bounds[key]
                    self.bounds[key] = (oy + dy * rr) if cur is None else (max(cur, oy + dy * rr) if dy > 0 else min(cur, oy + dy * rr))

        def cells(pts):
            if not pts:
                return np.zeros(0, int)
            xs = np.concatenate([p[0] for p in pts])
            ys = np.concatenate([p[1] for p in pts])
            j, i = self.idx(xs, ys)
            return np.unique(j * N + i)

        free_c, view_c, occl_c = cells(free_pts), cells(cov_pts), cells(occl_pts)
        hit_c = cells([(np.array([h[0]]), np.array([h[1]])) for h in hits])
        occ, cov, last, occl = self.occ.ravel(), self.cov.ravel(), self.last.ravel(), self.occl.ravel()
        # latest observation wins: something moved away -> free again
        occ[free_c] = FREE
        occ[hit_c] = OCC
        j0, i0 = (int(v) for v in self.idx(x, y))
        rr = int(math.ceil(robot_r / CELL))
        foot = self.occ[max(0, j0 - rr):j0 + rr + 1, max(0, i0 - rr):i0 + rr + 1]   # a view: where the robot stands
        foot[foot != OCC] = FREE
        view_c = view_c[occ[view_c] != OCC]
        new = int((cov[view_c] == 0).sum())
        cov[view_c] += 1
        last[view_c] = self.n_obs
        occl[occl_c] = True
        occl[view_c] = False
        a = CELL * CELL
        stat = {"obs": self.n_obs, "x": round(x, 3), "y": round(y, 3), "heading": round(heading, 1),
                "view_m2": round(len(view_c) * a, 3), "new_m2": round(new * a, 3),
                "overlap": round(1 - new / len(view_c), 3) if len(view_c) else 1.0}
        self.history.append(stat)
        self.viewpoints.append((round(x, 3), round(y, 3), round(heading, 1)))
        self.n_obs += 1
        return stat

    def note_turn(self, degrees, new_m2_after):
        self.counters["turns"] += 1
        self.counters["turn_deg"] += abs(degrees)
        if new_m2_after < 0.1:
            self.counters["wasted_turns"] += 1

    def mark_occupied(self, x, y, r):
        """Something is known to be here (we just put it down, or bumped into it)."""
        k = int(math.ceil(r / CELL)) + 1
        j0, i0 = (int(v) for v in self.idx(x, y))
        for j in range(max(0, j0 - k), min(N, j0 + k + 1)):
            for i in range(max(0, i0 - k), min(N, i0 + k + 1)):
                cx, cy = self.centre(j, i)
                if math.hypot(cx - x, cy - y) <= r:
                    self.occ[j, i] = OCC

    def add_object(self, key, wx, wy, r):
        self.objects[key] = {"x": round(float(wx), 3), "y": round(float(wy), 3), "r": round(float(r), 3), "obs": self.n_obs}

    # ------------------------------------------------------------- summary
    def stats(self):
        a = CELL * CELL
        room = self.room_mask()
        floor = room & (self.occ != OCC)
        seen = floor & (self.cov > 0)
        views = sum(h["view_m2"] for h in self.history) or 1e-9
        new = sum(h["new_m2"] for h in self.history)
        frontier = self.frontier_mask()
        return {"observations": self.n_obs, "observed_m2": round(float(seen.sum() * a), 2),
                "room_est_m2": round(float(floor.sum() * a), 2),
                "coverage_est": round(float(seen.sum() / max(1, floor.sum())), 3),
                "redundant": round(1 - new / views, 3),
                "new_last_m2": self.history[-1]["new_m2"] if self.history else 0.0,
                "frontier_regions": self.frontier_regions(frontier),
                "walls_seen": [k for k, v in self.bounds.items() if v is not None], **self.counters}

    def frontier_mask(self):
        """Floor not yet seen by the camera but next to floor that has been."""
        room = self.room_mask()
        seen = self.cov > 0
        unseen = room & ~seen & (self.occ != OCC)
        near_seen = dilate(seen, CELL * 1.01)
        return unseen & near_seen

    @staticmethod
    def frontier_regions(mask):
        seen = np.zeros_like(mask)
        count = 0
        for j, i in zip(*np.nonzero(mask)):
            if seen[j, i]:
                continue
            count += 1
            stack = [(j, i)]
            seen[j, i] = True
            while stack:
                a, b = stack.pop()
                for c, d in ((a + 1, b), (a - 1, b), (a, b + 1), (a, b - 1)):
                    if 0 <= c < N and 0 <= d < N and mask[c, d] and not seen[c, d]:
                        seen[c, d] = True
                        stack.append((c, d))
        return count

    # ------------------------------------------------------------- planning
    def costmap(self, clearance):
        """Free cells and a soft cost near obstacles; occupied and unknown are not drivable."""
        hard = dilate(self.occ == OCC, ROBOT_R + 0.04)
        soft = dilate(self.occ == OCC, clearance)
        drivable = (self.occ == FREE) & ~hard
        cost = np.where(soft, 6.0, 1.0)
        self._safe = drivable & ~soft              # for straight shortcuts: full clearance (body + hand)
        return drivable, cost

    def distances(self, x, y, clearance):
        drivable, cost = self.costmap(clearance)
        j0, i0 = self.idx(x, y)
        dist = np.full((N, N), np.inf)
        parent = -np.ones((N, N), np.int32)
        dist[j0, i0] = 0.0
        pq = [(0.0, int(j0), int(i0))]
        steps = [(1, 0, 1), (-1, 0, 1), (0, 1, 1), (0, -1, 1), (1, 1, 1.414), (1, -1, 1.414), (-1, 1, 1.414), (-1, -1, 1.414)]
        while pq:
            d, j, i = heapq.heappop(pq)
            if d > dist[j, i]:
                continue
            for dj, di, w in steps:
                a, b = j + dj, i + di
                if 0 <= a < N and 0 <= b < N and drivable[a, b]:
                    nd = d + w * CELL * cost[a, b]
                    if nd < dist[a, b]:
                        dist[a, b] = nd
                        parent[a, b] = j * N + i
                        heapq.heappush(pq, (nd, a, b))
        return dist, parent, drivable

    def path_to(self, parent, j, i, start):
        cells = []
        k = j * N + i
        while k >= 0:
            cells.append(divmod(int(k), N))
            k = parent.ravel()[k]
        cells.reverse()
        pts = [self.centre(a, b) for a, b in cells]
        if pts:
            pts[0] = start
        return pts

    def line_ok(self, a, b, drivable):
        n = max(2, int(math.dist(a, b) / (CELL / 2)))
        xs = np.linspace(a[0], b[0], n)
        ys = np.linspace(a[1], b[1], n)
        j, i = self.idx(xs, ys)
        return bool(drivable[j, i].all())

    def shortcut(self, pts, drivable):
        """Any-angle waypoints: keep only the corners line-of-sight needs (no staircases).
        Shortcuts must keep the full clearance, except right next to the start."""
        if len(pts) <= 2:
            return pts
        safe = getattr(self, "_safe", drivable).copy()
        j0, i0 = (int(v) for v in self.idx(*pts[0]))
        safe[max(0, j0 - 4):j0 + 5, max(0, i0 - 4):i0 + 5] |= drivable[max(0, j0 - 4):j0 + 5, max(0, i0 - 4):i0 + 5]
        out, k = [pts[0]], 0
        while k < len(pts) - 1:
            m = len(pts) - 1
            while m > k + 1 and not self.line_ok(pts[k], pts[m], safe):
                m -= 1
            if m == k + 1:                         # inside a tight spot: allow body-only clearance
                m = len(pts) - 1
                while m > k + 1 and not self.line_ok(pts[k], pts[m], drivable):
                    m -= 1
            out.append(pts[m])
            k = m
        return out

    def plan_path(self, start, goal, clearance=0.48):
        dist, parent, drivable = self.distances(start[0], start[1], clearance)
        j, i = self.idx(goal[0], goal[1])
        if not np.isfinite(dist[j, i]):
            return None
        pts = self.path_to(parent, int(j), int(i), start)
        pts[-1] = goal
        return self.shortcut(pts, drivable)

    def predict(self, px, py, headings, rays=19, step=0.1):
        """Predicted (new area, repeated area) for every candidate pose: rays stop at known
        obstacles and at walls seen so far; unknown space is assumed open (optimistic)."""
        d = np.arange(step / 2, MAX_RANGE, step, dtype=np.float32)
        bears = np.linspace(-HFOV / 2, HFOV / 2, rays, dtype=np.float32)
        hr = np.radians(headings.astype(np.float32))
        ang = hr[:, None] + np.radians(bears)[None, :]
        fx, fy = np.cos(ang), -np.sin(ang)
        ox = px[:, None] + np.cos(hr)[None, :] * CAM_AHEAD
        oy = py[:, None] - np.sin(hr)[None, :] * CAM_AHEAD
        X = ox[:, :, None, None] + fx[None, :, :, None] * d
        Y = oy[:, :, None, None] + fy[None, :, :, None] * d
        j = np.clip(((Y + HALF) / CELL).astype(np.int32), 0, N - 1)
        i = np.clip(((X + HALF) / CELL).astype(np.int32), 0, N - 1)
        inroom = self.room_mask()[j, i] & (np.abs(X) < HALF) & (np.abs(Y) < HALF)
        blocked = (self.occ[j, i] == OCC) | ~inroom
        visible = np.cumprod(~blocked, axis=-1).astype(bool)
        w = step * d * math.radians(HFOV / (rays - 1))
        counted = visible & (d >= CAM_NEAR)
        unseen = self.cov[j, i] == 0
        gain = ((counted & unseen) * w).sum(axis=(-1, -2))
        rep = ((counted & ~unseen) * w).sum(axis=(-1, -2))
        return gain, rep

    def next_best_view(self, x, y, heading, holding=False, weights=None, lattice=3, headings=12, carry_r=0.23):
        """Best (x, y, heading) to observe next, or None when nothing worth seeing is left."""
        W = {**WEIGHTS, **(weights or {})}
        if W["planner"] == "lattice":
            return self._nbv_lattice(x, y, heading, holding, W, lattice, headings, carry_r)
        clearance = 0.62 if holding else 0.48
        dist, parent, drivable = self.distances(x, y, clearance)
        reach = np.isfinite(dist)
        lat = np.zeros_like(reach)
        lat[::lattice, ::lattice] = True
        js, is_ = np.nonzero(reach & lat & drivable)
        cx, cy = self.centre(js, is_)
        px = np.concatenate([[x], np.asarray(cx, np.float32)])
        py = np.concatenate([[y], np.asarray(cy, np.float32)])
        pd = np.concatenate([[0.0], dist[js, is_]])
        hs = np.arange(headings, dtype=np.float32) * (360.0 / headings) - 180.0
        gain, rep = self.predict(px.astype(np.float32), py.astype(np.float32), hs)
        # clearance from known obstacles at each candidate
        occ = self.occ == OCC
        clear = np.full(len(px), 1.0)
        for rad in (0.5, 0.4, 0.3, 0.25):
            m = dilate(occ, rad)
            jj, ii = self.idx(px, py)
            clear = np.where(m[jj, ii], rad, clear)
        travel_dir = np.degrees(np.arctan2(-(py - y), px - x))
        moving = pd > 0.05
        turn1 = np.where(moving, np.abs(wrap_np(travel_dir - heading)), 0.0)
        base = np.where(moving, travel_dir, heading)
        turn2 = np.abs(wrap_np(hs[None, :] - base[:, None]))
        t = pd[:, None] / 1.0 + (turn1[:, None] + turn2) / 90.0 + np.where(moving, W["leg"], 0.0)[:, None]
        view = gain + rep
        extra = np.maximum(0.0, rep - W["keep_overlap"] * view)
        risk = np.maximum(0.0, 0.5 - clear)[:, None] * W["risk"]
        if W["mode"] == "rate":                    # most new information per second spent
            score = (gain - W["overlap"] * extra - risk) / (t + W["view_cost"])
        else:
            score = gain - W["time"] * t - W["overlap"] * extra - risk
        worth = gain >= W["min_gain"]
        if not worth.any():                        # nothing left anywhere that is worth a look
            return None
        score = np.where(worth, score, -np.inf)
        k = int(np.argmax(score))
        p, h = divmod(k, len(hs))
        route = [(float(x), float(y))]
        if p > 0:
            route = self.path_to(parent, int(js[p - 1]), int(is_[p - 1]), (float(x), float(y)))
            route = self.shortcut(route, drivable)
        best = {"x": float(px[p]), "y": float(py[p]), "heading": float(hs[h]), "gain_m2": round(float(gain[p, h]), 2),
                "overlap_m2": round(float(rep[p, h]), 2), "travel_m": round(float(pd[p]), 2),
                "time_s": round(float(t[p, h]), 2), "score": round(float(score[p, h]), 3),
                "route": [(round(a, 2), round(b, 2)) for a, b in route], "candidates": int(score.size)}
        self.plan = best
        return best

    def _nbv_lattice(self, x, y, heading, holding, W, lattice, headings, carry_r):
        """Dijkstra once from the robot's pose over (cell, heading) states gives the true time to
        reach every candidate (turns included); information gain comes from predict()."""
        from planner import Planner, TURN_FIXED_DEFAULT
        P = Planner(self.occ, holding=holding, carry_r=carry_r)
        cost, parent, st = P.dijkstra((x, y, heading))
        best_cell = cost.min(axis=2)
        reach = np.isfinite(best_cell)
        lat = np.zeros_like(reach)
        lat[::lattice, ::lattice] = True
        j0, i0 = (int(v) for v in self.idx(x, y))
        js, is_ = np.nonzero(reach & lat)
        js = np.concatenate([[j0], js])
        is_ = np.concatenate([[i0], is_])
        cx, cy = self.centre(js, is_)
        px = np.asarray(cx, np.float32)
        py = np.asarray(cy, np.float32)
        px[0], py[0] = x, y                                 # the robot's own position: turn-only views
        hs = np.arange(headings, dtype=np.float32) * (360.0 / headings) - 180.0
        gain, rep_ = self.predict(px, py, hs)
        bins = np.arange(8) * 45.0
        turn = np.abs(wrap_np(hs[None, :] - bins[:, None])) / 90.0          # (8, H) final turn
        turn = turn + np.where(turn > 0.005, TURN_FIXED_DEFAULT, 0.0)
        c = cost[js, is_]                                                    # (P, 8)
        c[0] = np.where(np.isfinite(c[0]), c[0], np.inf)
        c[0, int(round((heading % 360) / 45.0)) % 8] = 0.0
        t = (c[:, :, None] + turn[None]).min(axis=1)                         # (P, H) seconds
        dsel = (c[:, :, None] + turn[None]).argmin(axis=1)
        moving = np.concatenate([[False], np.ones(len(js) - 1, bool)])
        t = t + np.where(moving, W["leg"], 0.0)[:, None]
        clear = P.clear[js, is_]
        view = gain + rep_
        extra = np.maximum(0.0, rep_ - W["keep_overlap"] * view)
        risk = np.maximum(0.0, 0.5 - clear)[:, None] * W["risk"]
        if W["mode"] == "rate":
            score = (gain - W["overlap"] * extra - risk) / (t + W["view_cost"])
        else:
            score = gain - W["time"] * t - W["overlap"] * extra - risk
        worth = (gain >= W["min_gain"]) & np.isfinite(t)
        if not worth.any():
            return None
        score = np.where(worth, score, -np.inf)
        k = int(np.argmax(score))
        p, h = divmod(k, len(hs))
        route = [(float(x), float(y))]
        travel = 0.0
        if p > 0:
            plan = P.dijkstra_path(cost, parent, int(js[p]), int(is_[p]), int(dsel[p, h]), (x, y, heading))
            if plan is not None:
                comp = P.compile(plan, final_heading=float(hs[h]))
                route, travel = comp["route"], comp["length_m"]
        best = {"x": float(px[p]), "y": float(py[p]), "heading": float(hs[h]), "gain_m2": round(float(gain[p, h]), 2),
                "overlap_m2": round(float(rep_[p, h]), 2), "travel_m": round(float(travel), 2),
                "time_s": round(float(t[p, h]), 2), "score": round(float(score[p, h]), 3),
                "route": [(round(a, 2), round(b, 2)) for a, b in route], "candidates": int(score.size),
                "dijkstra": {"expanded": st["expanded"], "ms": st["ms"]}}
        self.plan = best
        self.costmap_view = P.costmap_chars(cost)
        return best

    # -------------------------------------------------------------- export
    def snapshot(self):
        """Compact map for the dashboard: one character per cell."""
        g = np.full((N, N), "u", dtype="<U1")
        g[self.occ == FREE] = "s"                      # free by range, not yet seen by the camera
        g[self.occl & (self.cov == 0)] = "h"
        g[self.cov > 0] = "f"
        g[(self.cov > 0) & (self.last >= self.n_obs - RECENT)] = "r"
        g[self.occ == OCC] = "o"
        if getattr(self, "_inflated", None) is not None:
            g[(g == "f") & self._inflated] = "i"           # seen floor too close to obstacles for the body
        return {"cell": CELL, "half": HALF, "n": N, "grid": "".join(g.ravel()), "costmap": getattr(self, "costmap_view", None),
                "viewpoints": self.viewpoints[-60:], "plan": self.plan, "objects": self.objects,
                "bounds": self.bounds, "stats": self.stats(), "history": self.history[-30:]}

    def save(self, path):
        tmp = f"{path}.tmp.npz"
        np.savez_compressed(tmp, occ=self.occ, cov=self.cov, last=self.last, occl=self.occl,
                            meta=json.dumps({"n_obs": self.n_obs, "history": self.history[-200:],
                                             "viewpoints": self.viewpoints[-200:], "objects": self.objects,
                                             "bounds": self.bounds, "counters": self.counters}, default=float))
        os.replace(tmp, path)

    @classmethod
    def load(cls, path):
        m = cls()
        if path and os.path.exists(path):
            with np.load(path, allow_pickle=False) as z:      # closed again, so the next save can replace it
                m.occ, m.cov, m.last, m.occl = (z[k].copy() for k in ("occ", "cov", "last", "occl"))
                meta = json.loads(str(z["meta"]))
            m.n_obs, m.history = meta["n_obs"], meta["history"]
            m.viewpoints = [tuple(v) for v in meta["viewpoints"]]
            m.objects, m.bounds, m.counters = meta["objects"], meta["bounds"], meta["counters"]
        return m


def wrap_np(a):
    return (a + 180.0) % 360.0 - 180.0
