"""Path planner correctness: Dijkstra exact, A* optimal (admissible heuristic), diagonal costs,
no corner cutting, footprint inflation (body, hand, load), smoothing safe on the real physics,
turn penalty effective, replanning after a map change.

Run:  .venv\\Scripts\\python tests\\test_planner.py
"""
import math
import random
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "tools"))
from mapping import FREE, N, OCC  # noqa: E402
from nav_sim import SimBody  # noqa: E402
from navigate import navigate, plan_route, predict, route_still_clear  # noqa: E402
from planner import CELL, DIRS, ROBOT_R, Planner, cell, centre  # noqa: E402

fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({info})" if info and not cond else ""), flush=True)
    if not cond:
        fails.append(name)


def room_grid(half=3.0):
    occ = np.zeros((N, N), np.int8)
    xs = -4.0 + (np.arange(N) + 0.5) * CELL
    X, Y = np.meshgrid(xs, xs)
    inside = (np.abs(X) < half) & (np.abs(Y) < half)
    occ[inside] = FREE
    ring = (np.abs(X) < half + CELL) & (np.abs(Y) < half + CELL) & ~inside
    occ[ring] = OCC
    return occ, X, Y


def bellman_ford(P, start_k, keys):
    INF = float("inf")
    dist = {k: INF for k in keys}
    dist[start_k] = 0.0
    for _ in range(len(keys)):
        changed = False
        for k in keys:
            if dist[k] == INF:
                continue
            c, d = divmod(k, 8)
            j, i = divmod(c, N)
            for a, b, nd, w in P._edges(j, i, d):
                nk = (a * N + b) * 8 + nd
                if nk in dist and dist[k] + w < dist[nk] - 1e-12:
                    dist[nk] = dist[k] + w
                    changed = True
        if not changed:
            break
    return dist


def main():
    random.seed(4)
    # ---------- the robot's own map of the obstacle level (scan only)
    sb = SimBody("level2")
    sb.survey()
    P = Planner(sb.cmap.occ)

    # 1. A* = Dijkstra on random pairs; A* expands fewer states; h admissible
    free = [(j, i) for j in range(N) for i in range(N) if P.state_ok(j, i, 0)]
    eq, fewer, adm, tried = 0, 0, 0, 0
    for _ in range(25):
        (sj, si), (gj, gi) = random.sample(free, 2)
        sx, sy = centre(sj, si)
        gx, gy = centre(gj, gi)
        a = P.astar((sx, sy, 0.0), (gx, gy), goal_tol=0.0)
        ea = P.expanded
        cost, _, st = P.dijkstra((sx, sy, 0.0))
        dbest = float(cost[gj, gi].min())
        if a is None:
            if not math.isfinite(dbest):
                eq += 1
                fewer += 1
                adm += 1
                tried += 1
            continue
        tried += 1
        eq += abs(a["cost"] - dbest) < 1e-6
        fewer += ea <= st["expanded"]
        adm += math.hypot(gx - sx, gy - sy) <= a["cost"] + 1e-9
    check("A* cost equals Dijkstra cost (25 random start/goal pairs)", eq == tried, f"{eq}/{tried}")
    check("A* expands no more states than Dijkstra", fewer == tried, f"{fewer}/{tried}")
    check("heuristic never exceeds the optimal cost (admissible)", adm == tried, f"{adm}/{tried}")

    # 2. Dijkstra = Bellman-Ford on a small closed region (exactness with non-negative costs)
    occ, X, Y = room_grid()
    small = np.zeros_like(occ)
    box = (np.abs(X + 0.0) < 0.6) & (np.abs(Y) < 0.6)
    small[box] = FREE
    small[(np.abs(X) < 0.7) & (np.abs(Y) < 0.7) & ~box] = OCC
    small[cell(0.15, 0.05)] = OCC                                  # an obstacle inside
    Ps = Planner(small)
    keys = [(j * N + i) * 8 + d for j in range(N) for i in range(N) for d in range(8)
            if small[j, i] == FREE and Ps.state_ok(j, i, d)]
    s0 = keys[0]
    c, d0 = divmod(s0, 8)
    j0, i0 = divmod(c, N)
    x0, y0 = centre(j0, i0)
    bf = bellman_ford(Ps, s0, keys)
    cost, _, _ = Ps.dijkstra((x0, y0, d0 * 45.0))
    flat = cost.ravel()
    worst = max(abs(flat[k] - v) for k, v in bf.items() if math.isfinite(v))
    check(f"Dijkstra equals Bellman-Ford on {len(keys)} states", worst < 1e-9, worst)

    # 3. diagonal and straight step costs (open room, far from walls: no risk term)
    Po = Planner(room_grid()[0])
    a = Po.astar((-0.95, -0.95, -45.0), (0.05, 0.05), goal_tol=0.0)
    check("diagonal: 10 cells cost 10 x 0.1414 s", a is not None and abs(a["cost"] - 1.4142) < 1e-3, a and a["cost"])
    a = Po.astar((-0.95, 0.05, 0.0), (0.05, 0.05), goal_tol=0.0)
    check("straight: 10 cells cost 1.0 s", a is not None and abs(a["cost"] - 1.0) < 1e-6, a and a["cost"])

    # 4. no corner cutting between two diagonally touching obstacles
    occ, X, Y = room_grid()
    j, i = cell(0.05, 0.05)
    occ[j, i] = OCC
    occ[j + 1, i + 1] = OCC
    Pc = Planner(occ, weights={"margin": -0.2})                    # tiny footprint so only the rule decides
    cut = False
    for jj in range(N):
        for ii in range(N):
            for d in range(8):
                for aa, bb, nd, w in Pc._edges(jj, ii, d):
                    if nd == d and aa != jj and bb != ii:
                        if not (Pc.drivable[aa, ii] and Pc.drivable[jj, bb]):
                            cut = True
    check("no diagonal step squeezes between two blocked cells", not cut)

    # 5. footprint: every planned state clears the body and the hand; executed path never touches
    a = P.astar((-2.4, 0.0, 0.0), (2.0, 0.5))
    ok_states = all(P.clear[j, i] >= P.hard and P.app_ok[2 * d][j, i] for j, i, d in a["states"])
    check("every lattice state keeps body and hand clear of known obstacles", ok_states)
    for start, goal in (((-2.4, 0.0, 0.0), (2.0, 0.5)), ((-0.3, 1.0, 0.0), (1.9, 1.15)), ((2.2, -2.2, 90.0), (-2.2, 2.2))):
        b = SimBody("level2")
        b.cmap = sb.cmap
        b.r.x, b.r.y, b.r.heading = start
        ok = navigate(b, goal, observe=False)
        fin = math.hypot(b.r.x - goal[0], b.r.y - goal[1])
        check(f"smoothed route {start[:2]}->{goal}: reached, no collision, body+hand never touch",
              ok and fin < 0.12 and b.r.stats["collisions"] == 0 and b.min_body > 0 and b.min_app > 0,
              (ok, round(fin, 2), b.r.stats["collisions"], round(b.min_body, 3), round(b.min_app, 3)))

    # 6. carried load: plans for the load's footprint, and executes cleanly
    b = SimBody("level1")
    b.survey()
    b.hold("o1")
    ok = navigate(b, (1.3, 1.2), final_heading=0.0, observe=False)
    check("carrying the red cube: reaches the drop pose, no collision, load never touches",
          ok and b.r.stats["collisions"] == 0 and b.min_app > 0 and b.r.held is not None,
          (ok, b.r.stats["collisions"], round(b.min_app, 3)))
    nb = SimBody("narrow")
    nb.survey()
    empty = plan_route(nb.cmap, (-2.4, 0.0, 0.0), (2.2, 0.0))[0]
    nb.hold("o1")
    loaded = plan_route(nb.cmap, (nb.r.x, nb.r.y, nb.r.heading), (2.2, 0.0), holding=True, carry_r=nb.carry_r)[0]
    check("narrow gap: empty robot finds the way through", empty is not None, empty and empty["length_m"])
    check("narrow gap: with a load that cannot fit, no route is claimed", loaded is None, loaded and loaded["route"])

    # 7. turn penalty changes the path in the intended direction
    def lattice_turns(w):
        Pw = Planner(sb.cmap.occ, weights={"turn_fixed": w})
        pl = Pw.astar((-2.4, 0.0, 0.0), (2.0, 0.5))
        st = pl["states"]
        return sum(1 for x, y in zip(st, st[1:]) if x[2] != y[2])
    t_lo, t_hi = lattice_turns(0.0), lattice_turns(3.0)
    check("higher turn penalty -> no more 45-degree turn steps", t_hi <= t_lo, (t_lo, t_hi))

    # 8. replanning: an obstacle appears on the route
    comp, P1 = plan_route(sb.cmap, (-2.4, 0.0, 0.0), (2.0, 0.5))
    x, y, h = -2.4, 0.0, 0.0
    mids = []
    for seg in comp["segments"]:
        x, y, h = predict((x, y, h), seg)
        mids.append((x, y))
    bx, by = mids[len(mids) // 2]
    occ2 = sb.cmap.occ.copy()
    jj, ii = cell(bx, by)
    occ2[jj - 2:jj + 3, ii - 2:ii + 3] = OCC
    P2 = Planner(occ2)
    check("route check notices a new obstacle on the route", not route_still_clear(P2, (-2.4, 0.0, 0.0), comp["segments"]))

    class M:
        occ = occ2
    comp2, P3 = plan_route(M, (-2.4, 0.0, 0.0), (2.0, 0.5))
    check("replanned route avoids it", comp2 is not None and route_still_clear(P3, (-2.4, 0.0, 0.0), comp2["segments"]))
    print(f"\n{len(fails)} failed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
