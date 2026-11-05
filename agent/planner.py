"""Path planning on the robot's own map: A* for one destination, Dijkstra for many.

Planning graph
  state  = (cell j, cell i, heading bin d), 0.1 m cells, 8 headings of 45 degrees
  edges  = drive one cell forward (or backward) in the current heading; turn 45 degrees in place
  cost   = seconds of execution: length / 1 m/s for driving, angle / 90 deg/s for turning,
           plus a stop-start overhead per turn, a reverse penalty, and a risk term that grows
           as clearance from known obstacles drops below `comfort` (WEIGHTS)
Every edge cost is >= 0, so Dijkstra is exact. A* uses h = straight-line distance / 1 m/s:
every metre of progress costs at least 1 s and turns cost >= 0, so h never overestimates
and A* returns the same optimal cost (tests/test_planner.py checks both against each other
and against Bellman-Ford). "Optimal" means: minimum of this cost over paths on this lattice.
The smoothed, curved path that is executed is a feasible improvement of that path, not a
proven continuous optimum.

Footprint (never a point robot)
  body        circle of the robot radius + margin must clear every known obstacle;
  appendage   the folded hand (empty) or the carried object (holding) is checked at its real
              offset for each heading, and at the half-way angle of every turn;
  diagonals   may not cut between two blocked cells;
  unknown     the body only drives on cells the range sensor has seen free.
"""
import heapq
import math
import time

import numpy as np

from mapping import CELL, FREE, HALF, N, OCC

ROBOT_R = 0.2
STOW_SIDE, STOW_AHEAD, HAND_R = 0.27, 0.10, 0.08
CARRY_SIDE, CARRY_AHEAD = 0.36, 0.62

WEIGHTS = {                 # configurable; tuned with tools/eval_navigation.py
    "turn_fixed": 0.25,     # s per in-place turn step beyond its rotation time (stop, settle, start)
    "reverse": 1.5,         # multiplier on driving backwards
    "risk": 6.0,            # s per metre driven at zero spare clearance (fades to 0 at `comfort`);
                            # measured: 2.0 let the body pass 4 mm from a box, 6.0 keeps >= 0.11 m for +5% time
    "comfort": 0.25,        # m of spare clearance beyond the footprint that counts as fully safe
    "margin": 0.07,         # m added to every footprint radius (map cells are 0.1 m); body hard limit ROBOT_R + 0.08
}
TURN_FIXED_DEFAULT = WEIGHTS["turn_fixed"]
DIRS = [(int(round(-math.sin(math.radians(45 * d)))), int(round(math.cos(math.radians(45 * d))))) for d in range(8)]


def fwd(h):
    r = math.radians(h)
    return math.cos(r), -math.sin(r)


def wrap(a):
    return (a + 180.0) % 360.0 - 180.0


def centre(j, i):
    return -HALF + (i + 0.5) * CELL, -HALF + (j + 0.5) * CELL


def cell(x, y):
    return (min(N - 1, max(0, int((y + HALF) / CELL))), min(N - 1, max(0, int((x + HALF) / CELL))))


class Planner:
    def __init__(self, occ, holding=False, carry_r=0.23, weights=None):
        self.W = {**WEIGHTS, **(weights or {})}
        self.occ = occ
        self.holding = holding
        if holding:
            self.app = (CARRY_SIDE, CARRY_AHEAD, carry_r)
        else:
            self.app = (STOW_SIDE, STOW_AHEAD, HAND_R)
        self.clear = self._distance_transform(occ == OCC)
        m = self.W["margin"]
        self.body_need = ROBOT_R + m
        self.hard = ROBOT_R + 0.08                     # cells mark surfaces to about +-7 cm: this keeps >= 1 cm real gap
        self.drivable = (occ == FREE) & (self.clear >= self.hard)        # hard limit
        self.safe = self.drivable & (self.clear >= self.body_need)
        # appendage clearance for 16 half-steps of 22.5 degrees (even = heading bins, odd = mid-turn)
        side, ahead, ar = self.app
        self.app_need = ar + m
        self.app_ok = []
        for k in range(16):
            h = 22.5 * k
            fx, fy = fwd(h)
            rx, ry = fwd(h + 90)
            ox, oy = fx * ahead + rx * side, fy * ahead + ry * side
            self.app_ok.append(self._shifted(self.clear, ox, oy) >= self.app_need)
        risk = np.clip((self.body_need + self.W["comfort"] - self.clear) / self.W["comfort"], 0.0, 1.0)
        self.risk = np.where(self.drivable, risk, 1.0)
        self.expanded = 0

    # ----------------------------------------------------------- geometry
    @staticmethod
    def _distance_transform(occ_mask):
        """Exact distance (m) from every cell centre to the nearest occupied cell centre."""
        js, is_ = np.nonzero(occ_mask)
        if len(js) == 0:
            return np.full(occ_mask.shape, 99.0)
        pts = np.stack([js, is_], 1).astype(np.float32)
        gj, gi = np.mgrid[0:N, 0:N]
        flat = np.stack([gj.ravel(), gi.ravel()], 1).astype(np.float32)
        best = np.full(len(flat), np.inf, np.float32)
        for k in range(0, len(pts), 256):
            d = ((flat[:, None, :] - pts[None, k:k + 256, :]) ** 2).sum(-1)
            best = np.minimum(best, d.min(1))
        return (np.sqrt(best) * CELL).reshape(N, N)

    @staticmethod
    def _shifted(a, ox, oy):
        """a sampled at (cell centre + offset metres); outside the grid -> 0."""
        dj, di = int(round(oy / CELL)), int(round(ox / CELL))
        out = np.zeros_like(a)
        ys = slice(max(0, -dj), N - max(0, dj))
        yd = slice(max(0, dj), N - max(0, -dj))
        xs = slice(max(0, -di), N - max(0, di))
        xd = slice(max(0, di), N - max(0, -di))
        out[ys, xs] = a[yd, xd]
        return out

    def state_ok(self, j, i, d):
        return self.drivable[j, i] and self.app_ok[2 * d][j, i]

    def clearance_at(self, x, y):
        j, i = cell(x, y)
        return float(self.clear[j, i])

    def pose_ok(self, x, y, h, strict=True):
        """Continuous pose check used to validate smoothed paths (body + appendage)."""
        j, i = cell(x, y)
        if self.occ[j, i] != FREE or self.clear[j, i] < (self.body_need if strict else self.hard):
            return False
        side, ahead, _ = self.app
        fx, fy = fwd(h)
        rx, ry = fwd(h + 90)
        ax, ay = x + fx * ahead + rx * side, y + fy * ahead + ry * side
        if abs(ax) >= HALF or abs(ay) >= HALF:
            return False
        aj, ai = cell(ax, ay)
        return bool(self.clear[aj, ai] >= self.app_need and self.occ[aj, ai] != OCC)

    # ------------------------------------------------------------- search
    def _edges(self, j, i, d):
        W = self.W
        for dd in (1, -1):                                    # turn 45 deg in place, sweep checked mid-way
            nd = (d + dd) % 8
            if self.app_ok[(2 * d + dd) % 16][j, i] and self.app_ok[2 * nd][j, i]:
                yield j, i, nd, 0.5 + W["turn_fixed"]
        dj, di = DIRS[d]
        step = CELL * (1.4142 if dj and di else 1.0)
        for sgn, mult in ((1, 1.0), (-1, W["reverse"])):
            a, b = j + sgn * dj, i + sgn * di
            if not (0 <= a < N and 0 <= b < N) or not self.state_ok(a, b, d):
                continue
            if dj and di and not (self.drivable[j + sgn * dj, i] and self.drivable[j, i + sgn * di]):
                continue                                       # no cutting between two blocked cells
            yield a, b, d, step * mult * (1.0 + W["risk"] * self.risk[a, b])

    def _start_state(self, x, y, h):
        j, i = cell(x, y)
        d = int(round((h % 360) / 45.0)) % 8
        return j, i, d

    def astar(self, start, goal, goal_tol=0.06, goal_heading=None):
        """Lowest-cost lattice path from start pose (x, y, h) to goal (x, y). Returns a dict or None."""
        t0 = time.perf_counter()
        sj, si, sd = self._start_state(*start)
        gj, gi = cell(*goal)
        gx, gy = goal
        gd = None if goal_heading is None else int(round((goal_heading % 360) / 45.0)) % 8
        reach = goal_tol + 0.075                              # a cell counts as arrived if its centre is this close
        size = N * N * 8
        g = np.full(size, np.inf)
        parent = np.full(size, -1, np.int64)
        k0 = (sj * N + si) * 8 + sd
        g[k0] = 0.0

        def h(j, i):
            x, y = centre(j, i)
            return max(0.0, math.hypot(x - gx, y - gy) - reach)    # seconds at 1 m/s; admissible

        pq = [(h(sj, si), 0.0, sj, si, sd)]
        self.expanded = 0
        while pq:
            f, gc, j, i, d = heapq.heappop(pq)
            k = (j * N + i) * 8 + d
            if gc > g[k]:
                continue
            self.expanded += 1
            cx_, cy_ = centre(j, i)
            if math.hypot(cx_ - gx, cy_ - gy) <= reach and (gd is None or d == gd):
                return self._result(k, parent, g, start, goal, goal_heading, t0, "astar")
            for a, b, nd, c in self._edges(j, i, d):
                nk = (a * N + b) * 8 + nd
                ng = gc + c
                if ng < g[nk] - 1e-12:
                    g[nk] = ng
                    parent[nk] = k
                    heapq.heappush(pq, (ng + h(a, b), ng, a, b, nd))
        return None

    def dijkstra(self, start, goals=None):
        """Costs from the start pose to every reachable state (or until all `goals` cells are settled).
        Returns (cost[N, N, 8], parent, stats)."""
        t0 = time.perf_counter()
        sj, si, sd = self._start_state(*start)
        size = N * N * 8
        g = np.full(size, np.inf)
        parent = np.full(size, -1, np.int64)
        k0 = (sj * N + si) * 8 + sd
        g[k0] = 0.0
        pending = None if goals is None else {(j, i) for j, i in goals}
        pq = [(0.0, sj, si, sd)]
        self.expanded = 0
        while pq:
            gc, j, i, d = heapq.heappop(pq)
            k = (j * N + i) * 8 + d
            if gc > g[k]:
                continue
            self.expanded += 1
            if pending is not None:
                pending.discard((j, i))
                if not pending:
                    break
            for a, b, nd, c in self._edges(j, i, d):
                nk = (a * N + b) * 8 + nd
                ng = gc + c
                if ng < g[nk] - 1e-12:
                    g[nk] = ng
                    parent[nk] = k
                    heapq.heappush(pq, (ng, a, b, nd))
        stats = {"expanded": self.expanded, "ms": round((time.perf_counter() - t0) * 1000, 1)}
        return g.reshape(N, N, 8), parent, stats

    def path_from(self, parent, k):
        out = []
        while k >= 0:
            c, d = divmod(int(k), 8)
            j, i = divmod(c, N)
            out.append((j, i, d))
            k = parent[k]
        return out[::-1]

    def _result(self, k, parent, g, start, goal, goal_heading, t0, name):
        states = self.path_from(parent, k)
        return {"planner": name, "states": states, "cost": float(g[k]), "expanded": self.expanded,
                "ms": round((time.perf_counter() - t0) * 1000, 1), "start": start, "goal": goal,
                "goal_heading": goal_heading}

    def dijkstra_path(self, cost, parent, j, i, d, start, goal_heading=None):
        k = (j * N + i) * 8 + d
        if not np.isfinite(cost[j, i, d]):
            return None
        return {"planner": "dijkstra", "states": self.path_from(parent, k), "cost": float(cost[j, i, d]),
                "expanded": self.expanded, "ms": None, "start": start, "goal": centre(j, i), "goal_heading": goal_heading}

    # ------------------------------------------------------- smoothing
    def corners(self, plan):
        """Lattice path -> points where a straight run ends: (x, y, kind) with kind = +1 forward
        run or -1 reverse run arriving at that point. The first point is the true start pose."""
        st = plan["states"]
        sx, sy, _ = plan["start"]
        pts = [(sx, sy, 0)]
        run = None                                            # (kind, heading bin)
        for a, b in zip(st, st[1:]):
            if a[2] != b[2]:
                continue                                      # a turn in place: position unchanged
            kind = 1 if (b[0] - a[0], b[1] - a[1]) == DIRS[a[2]] else -1
            if run is not None and run != (kind, a[2]):
                x, y = centre(a[0], a[1])
                pts.append((x, y, run[0]))
            run = (kind, a[2])
        if run is not None:
            x, y = centre(st[-1][0], st[-1][1])
            pts.append((x, y, run[0]))
        gx, gy = plan["goal"]
        if len(pts) > 1 and math.hypot(pts[-1][0] - gx, pts[-1][1] - gy) < 0.2:
            a = pts[-2][:2]
            h = self._dir(a, (gx, gy)) if pts[-1][2] == 1 else wrap(self._dir(a, (gx, gy)) + 180)
            if self.segment_ok(a, (gx, gy), h, strict=False):
                pts[-1] = (gx, gy, pts[-1][2])                # finish on the exact goal when that is safe
        return pts

    def segment_ok(self, a, b, h, strict=True, start_slack=0.0):
        n = max(2, int(math.dist(a, b) / 0.04))
        for k in range(n + 1):
            x, y = a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n
            loose = math.dist((x, y), a) < start_slack
            if not self.pose_ok(x, y, h, strict=strict and not loose):
                return False
        return True

    def sweep_ok(self, x, y, h0, h1, strict=True):
        dh = wrap(h1 - h0)
        n = max(1, int(abs(dh) / 8))
        return all(self.pose_ok(x, y, h0 + dh * k / n, strict=strict) for k in range(n + 1))

    @staticmethod
    def _dir(a, b):
        return math.degrees(math.atan2(-(b[1] - a[1]), b[0] - a[0]))

    def waypoints(self, plan):
        """Any-angle shortcuts across forward runs, each validated with the full footprint (body,
        hand or load along the leg, and the turn sweep at its start). Reverse runs are kept as they are."""
        pts = self.corners(plan)
        sh = plan["start"][2]
        out = [pts[0]]
        heading = sh
        k = 0
        while k < len(pts) - 1:
            m = len(pts) - 1
            if True:                                          # a valid straight forward leg may also replace
                while m > k + 1:                              # reverse-and-turn manoeuvres (they were only cheaper, not needed)
                    if True:
                        a, b = out[-1][:2], pts[m][:2]
                        h = self._dir(a, b)
                        first = len(out) == 1
                        if self.segment_ok(a, b, h, start_slack=0.35 if first else 0.0) and \
                                self.sweep_ok(a[0], a[1], heading, h, strict=not first):
                            break
                    m -= 1
            a, b = out[-1][:2], pts[m][:2]
            kind = pts[m][2] if m == k + 1 else 1             # a shortcut is always driven forwards
            if math.dist(a, b) > 1e-6:
                heading = self._dir(a, b) if kind == 1 else wrap(self._dir(a, b) + 180)
            out.append((b[0], b[1], kind))
            k = m
        return out

    def compile(self, plan, final_heading=None, fillets=True, smooth=True):
        """Plan -> executable segments [("turn", deg) | ("move", m) | ("arc", r, deg)] + predictions."""
        sx, sy, sh = plan["start"]
        pts = self.waypoints(plan) if smooth else self.corners(plan)
        legs = []
        for a, b in zip(pts, pts[1:]):
            L = math.dist(a[:2], b[:2])
            if L > 1e-6:
                h = self._dir(a, b) if b[2] == 1 else wrap(self._dir(a, b) + 180)   # reverse: face away
                legs.append((a[:2], b[:2], h, L, b[2]))
        segs, heading = [], sh
        for n, (a, b, h, L, sgn) in enumerate(legs):
            dh = wrap(h - heading)
            used_in = 0.0
            if (fillets and n > 0 and sgn > 0 and legs[n - 1][4] > 0 and segs and segs[-1][0] == "move"
                    and abs(dh) > 8):
                fil = self._fillet(legs[n - 1], legs[n], heading, dh)
                if fil is not None:
                    R, t = fil
                    segs[-1] = ("move", segs[-1][1] - t)
                    segs.append(("arc", R, dh))
                    used_in, dh = t, 0.0
            if abs(dh) > 0.5:
                segs.append(("turn", dh))
            segs.append(("move", sgn * (L - used_in)))
            heading = h
        if final_heading is not None and abs(wrap(final_heading - heading)) > 0.5:
            segs.append(("turn", wrap(final_heading - heading)))
        segs = [q for q in segs if not (q[0] == "move" and abs(q[1]) < 0.005)]
        T = L_tot = 0.0
        turns = 0
        for q in segs:
            if q[0] == "move":
                T += abs(q[1])
                L_tot += abs(q[1])
            elif q[0] == "turn":
                T += abs(q[1]) / 90.0
                turns += 1
            else:
                v = min(1.0, q[1] * math.pi / 2)
                arc_len = q[1] * math.radians(abs(q[2]))
                T += arc_len / v
                L_tot += arc_len
        end = legs[-1][1] if legs else (sx, sy)
        return {"segments": segs, "time_s": round(T, 2), "length_m": round(L_tot, 2), "turns": turns,
                "arcs": sum(1 for q in segs if q[0] == "arc"), "end": end,
                "route": [(round(sx, 2), round(sy, 2))] + [(round(l[1][0], 2), round(l[1][1], 2)) for l in legs]}

    def _fillet(self, leg_in, leg_out, h_in, dh):
        """Round a corner with an arc when it fits, is clear, and is not slower than stop-turn-go."""
        if abs(dh) > 150:
            return None
        corner = leg_in[1]
        for R in (0.6, 0.45, 0.3, 0.2):
            t = R * math.tan(math.radians(abs(dh)) / 2)
            if t > 0.5 * leg_in[3] or t > 0.5 * leg_out[3]:
                continue
            v = min(1.0, R * math.pi / 2)
            t_arc = R * math.radians(abs(dh)) / v
            t_turn = abs(dh) / 90.0 + 2 * t + WEIGHTS["turn_fixed"]
            if t_arc > t_turn * 1.1:
                continue
            fx, fy = fwd(h_in)
            px, py = corner[0] - fx * t, corner[1] - fy * t          # tangent point on the way in
            sign = 1 if dh > 0 else -1
            rx, ry = fwd(h_in + 90)
            cx, cy = px + sign * rx * R, py + sign * ry * R
            ok = True
            for k in range(1, 13):
                hh = h_in + dh * k / 12
                nrx, nry = fwd(hh + 90)
                if not self.pose_ok(cx - sign * nrx * R, cy - sign * nry * R, hh):
                    ok = False
                    break
            if ok:
                return R, t
        return None

    def costmap_chars(self, cost):
        """Per-cell minimum cost (over headings) as one character per cell: '0'-'9','a'-'z', '.' unreachable."""
        best = cost.min(axis=2)
        alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
        out = np.full(best.shape, ".", dtype="<U1")
        ok = np.isfinite(best)
        q = np.clip((best[ok] / 0.5).astype(int), 0, len(alphabet) - 1)      # 0.5 s per step of the scale
        out[ok] = np.array(list(alphabet))[q]
        return "".join(out.ravel())
