"""Exploration strategies over the robot's own map (mapping.CoverageMap).

  nbv       coverage-aware next-best-view: go where the most unseen floor is visible
            for the least time, overlap and risk (the new planner)
  sweep     the problem statement's search pattern: turn 45 deg x 8 looking each
            time, then drive up to 2 m towards open space and repeat (existing approach)
  frontier  classic nearest-frontier: drive to the closest edge of the unseen area
            (a common greedy baseline)

All three observe the same way (look + scan after every move/turn they choose to
look after), use the same map and stop on the same rules, so they can be compared.
`stop` is an optional callable checked after each observation (target found).
"""
import math

import numpy as np

from mapping import N, CELL, wrap


def follow(b, route, final_heading=None, observe_legs=True):
    """Drive a waypoint route leg by leg (turn, move). Looks at intermediate corners only;
    the caller observes at the end, after the final turn (no double look on arrival)."""
    legs = route[1:]
    for n, wp in enumerate(legs):
        s = b.st()
        d = math.dist((s["x"], s["y"]), wp)
        if d < 0.04:
            continue
        b.face_point(wp)
        b.move(d)
        if observe_legs and n < len(legs) - 1 and d > 0.5:
            b.see()
    if final_heading is not None:
        s = b.st()
        b.turn(final_heading - s["heading"])


def go(b, best):
    """Reach a chosen viewpoint: A* + smoothed route with replanning; old leg-by-leg fallback."""
    from navigate import navigate
    if math.hypot(best["x"] - b.st()["x"], best["y"] - b.s["y"]) < 0.05:
        b.turn(best["heading"] - b.s["heading"])
        return
    if not navigate(b, (best["x"], best["y"]), final_heading=best["heading"], observe=False):
        follow(b, best["route"], best["heading"])


def _done(b, goal, stop):
    if stop is not None and stop():
        return "found"
    if b.cmap.stats()["coverage_est"] >= goal:
        return "coverage"
    return None


def unstick(b):
    """No viewpoint is reachable although much is unseen: the robot is usually standing too close to
    something to turn on the spot. Step back (or forward) into open floor, at most twice per search."""
    n = getattr(b, "_unstuck", 0)
    if n >= 2:
        return False
    b._unstuck = n + 1
    b.say("No reachable viewpoint from here (too close to something to turn); stepping into open floor.")
    if hasattr(b, "open_space") and b.open_space(0.6):
        return True
    r = b.cl.move(-0.3)
    if r.get("collided"):
        b.cl.move(0.3)
    return True


def explore_nbv(b, goal=0.95, max_views=40, stop=None, weights=None):
    b.see()
    for _ in range(max_views):
        why = _done(b, goal, stop)
        if why:
            return why
        s = b.st()
        best = b.cmap.next_best_view(s["x"], s["y"], s["heading"], s["holding"], weights,
                                     carry_r=getattr(b, "carry_r", 0.23))
        if best is None and b.cmap.stats()["coverage_est"] < goal - 0.15 and unstick(b):
            continue
        if best is None:
            return "no_gain"
        b.say(f"Next view ({best['x']:.2f}, {best['y']:.2f}) facing {best['heading']:.0f}: about "
              f"{best['gain_m2']:.1f} m^2 unseen for {best['travel_m']:.1f} m of driving.")
        b.publish()
        go(b, best)
        b.see()
    return "max_views"


def explore_sweep(b, goal=0.95, max_views=40, stop=None, step=45.0):
    b.see()
    views = 1
    while views < max_views:
        for _ in range(int(round(360 / step)) - 1):
            why = _done(b, goal, stop)
            if why:
                return why
            b.turn(step)
            b.see()
            views += 1
        why = _done(b, goal, stop)
        if why:
            return why
        s = b.st()
        ranges = b.cl.scan(9)                         # where is the most open space?
        k = max(range(len(ranges)), key=lambda i: ranges[i] if ranges[i] is not None else 10)
        b.turn(-45 + 90 * k / 8)
        r = ranges[k] if ranges[k] is not None else 10
        b.move(max(0.0, min(2.0, r - 0.6)))
        b.see()
        views += 1
    return "max_views"


def explore_frontier(b, goal=0.95, max_views=40, stop=None, standoff=(0.8, 1.5)):
    """Nearest-frontier with a camera-aware stand-off: drive to the closest reachable cell
    0.8-1.5 m from the nearest unseen frontier and face it (closer than ~0.5 m the floor
    is below the image, so standing on the frontier would never see it)."""
    from mapping import dilate
    b.see()
    tried = []
    for _ in range(max_views):
        why = _done(b, goal, stop)
        if why:
            return why
        s = b.st()
        m = b.cmap
        dist, parent, drivable = m.distances(s["x"], s["y"], 0.62 if s["holding"] else 0.48)
        front = m.frontier_mask()
        for tx, ty in tried:                       # do not chase the same frontier twice
            tj, ti = (int(v) for v in m.idx(tx, ty))
            front[max(0, tj - 3):tj + 4, max(0, ti - 3):ti + 4] = False
        if not front.any():
            return "no_gain"
        ring = dilate(front, standoff[1]) & ~dilate(front, standoff[0])
        cand = drivable & np.isfinite(dist) & ring
        if not cand.any():                         # nowhere to stand yet: turn to face the nearest frontier
            fj, fi = np.nonzero(front)
            fx, fy = m.centre(fj, fi)
            near = int(np.argmin((fx - s["x"]) ** 2 + (fy - s["y"]) ** 2))
            tried.append((float(fx[near]), float(fy[near])))
            b.face_point((float(fx[near]), float(fy[near])))
            b.see()
            continue
        k = int(np.argmin(np.where(cand, dist, np.inf)))
        j, i = divmod(k, N)
        cx, cy = m.centre(j, i)
        fj, fi = np.nonzero(front)
        fx, fy = m.centre(fj, fi)
        near = int(np.argmin((fx - cx) ** 2 + (fy - cy) ** 2))
        tried.append((float(fx[near]), float(fy[near])))
        look = math.degrees(math.atan2(-(fy[near] - cy), fx[near] - cx))
        route = m.shortcut(m.path_to(parent, j, i, (s["x"], s["y"])), drivable)
        m.plan = {"x": cx, "y": cy, "heading": look, "route": [(round(a, 2), round(c, 2)) for a, c in route]}
        follow(b, route, look)
        b.see()
    return "max_views"


STRATEGIES = {"nbv": explore_nbv, "sweep": explore_sweep, "frontier": explore_frontier,
              "sweep60": lambda b, **k: explore_sweep(b, step=60.0, **k)}


def heading_to(x, y, tx, ty):
    return math.degrees(math.atan2(-(ty - y), tx - x))


def unused():                                     # keep linters quiet about wrap import in tools
    return wrap
