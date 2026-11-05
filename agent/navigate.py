"""Drive to a goal on the robot's own map: plan with A*, execute the smoothed route with the
existing motion primitives (move, turn, arc), and replan only when there is a reason:

  - a move or arc reports a collision (the engine has already backed off 0.1 m)
  - the robot ends a segment more than 0.15 m / 10 deg away from where the plan said
  - a fresh observation marks the rest of the route as blocked (checked after long segments)
  - the load changes (pickup/drop happen outside, so every call plans for the current state)
A replaced task never resumes its route: the controller ends the agent process, and the engine
refuses requests carrying the old task id.
"""
import math

from planner import Planner, fwd, wrap

REPLANS = 4
DEVIATION_M, DEVIATION_DEG = 0.15, 10.0
OBSERVE_AFTER_M = 0.8


def snap_goal(P, goal, radius=0.6):
    """The closest cell to `goal` (within radius) where the robot body fits with its full margin."""
    import numpy as np
    from planner import cell
    j0, i0 = cell(*goal)
    k = int(radius / 0.1)
    best = None
    for j in range(max(0, j0 - k), min(P.occ.shape[0], j0 + k + 1)):
        for i in range(max(0, i0 - k), min(P.occ.shape[1], i0 + k + 1)):
            if P.safe[j, i]:
                d = (j - j0) ** 2 + (i - i0) ** 2
                if best is None or d < best[0]:
                    best = (d, j, i)
    if best is None or best[0] == 0:
        return None
    from mapping import CoverageMap
    return tuple(float(v) for v in CoverageMap.centre(best[1], best[2]))


def plan_route(cmap, pose, goal, holding=False, carry_r=0.23, final_heading=None, weights=None, algo="astar",
               smooth=True, fillets=True):
    P = Planner(cmap.occ, holding=holding, carry_r=carry_r, weights=weights)
    import numpy as np
    from mapping import FREE
    cmap._inflated = (cmap.occ == FREE) & (P.clear < P.body_need)      # shown on the dashboard
    if algo == "astar":
        plan = P.astar(pose, goal, goal_tol=0.06)
        if plan is None:                       # goal inside a safety margin (next to a wall): nearest safe pose
            alt = snap_goal(P, goal)
            if alt is not None:
                plan = P.astar(pose, alt, goal_tol=0.06)
    else:                                                     # Dijkstra to the goal cell (same optimal cost)
        from planner import cell
        j, i = cell(*goal)
        cost, parent, st = P.dijkstra(pose, goals=[(j + a, i + b) for a in (-1, 0, 1) for b in (-1, 0, 1)])
        best = None
        for a in (-1, 0, 1):
            for b in (-1, 0, 1):
                for d in range(8):
                    jj, ii = j + a, i + b
                    if 0 <= jj < cost.shape[0] and 0 <= ii < cost.shape[1] and (best is None or cost[jj, ii, d] < best[0]):
                        best = (cost[jj, ii, d], jj, ii, d)
        plan = None if best is None or not math.isfinite(best[0]) else P.dijkstra_path(cost, parent, *best[1:], pose)
        if plan is not None:
            plan.update({"expanded": st["expanded"], "ms": st["ms"], "goal": goal})
    if plan is None:
        return None, P
    comp = P.compile(plan, final_heading=final_heading, smooth=smooth, fillets=fillets)
    comp.update({"planner": plan["planner"], "cost": round(plan["cost"], 2), "expanded": plan["expanded"],
                 "plan_ms": plan["ms"], "goal": [round(goal[0], 2), round(goal[1], 2)],
                 "holding": holding})
    return comp, P


def predict(pose, seg):
    x, y, h = pose
    if seg[0] == "turn":
        return x, y, h + seg[1]
    if seg[0] == "move":
        fx, fy = fwd(h)
        return x + fx * seg[1], y + fy * seg[1], h
    R, deg = seg[1], seg[2]
    sign = 1 if deg > 0 else -1
    rx, ry = fwd(h + 90)
    cx, cy = x + sign * rx * R, y + sign * ry * R
    nh = h + deg
    nrx, nry = fwd(nh + 90)
    return cx - sign * nrx * R, cy - sign * nry * R, nh


def bumped(b, direction, turning=0.0):
    """A collision is information: mark where we touched and look again before replanning.

    We cannot tell which part touched, so both leading points are marked: in front of the body and
    in front of the hand (the folded hand, or the carried object). Turning: the hand sweeps sideways,
    so the mark goes where the hand was heading."""
    from planner import CARRY_AHEAD, CARRY_SIDE, HAND_R, STOW_AHEAD, STOW_SIDE
    s = b.st()
    h = s["heading"]
    fx, fy = fwd(h)
    rx, ry = fwd(h + 90)
    side, ahead, hr = ((CARRY_SIDE, CARRY_AHEAD, getattr(b, "carry_r", 0.23)) if s["holding"]
                       else (STOW_SIDE, STOW_AHEAD, HAND_R))
    hx, hy = s["x"] + fx * ahead + rx * side, s["y"] + fy * ahead + ry * side
    if turning:
        # the hand rotated a little further around the robot centre, in the turn direction (clockwise = +)
        k = math.radians(8.0 if turning > 0 else -8.0)
        dx, dy = hx - s["x"], hy - s["y"]
        cx, cy = s["x"] + dx * math.cos(k) + dy * math.sin(k), s["y"] - dx * math.sin(k) + dy * math.cos(k)
        # the contact is on the leading edge of the hand / carried object, not at its centre
        q = math.radians(90.0 if turning > 0 else -90.0)
        tx, ty = dx * math.cos(q) + dy * math.sin(q), -dx * math.sin(q) + dy * math.cos(q)
        n = math.hypot(tx, ty) or 1.0
        b.cmap.mark_occupied(cx + tx / n * (hr + 0.04), cy + ty / n * (hr + 0.04), 0.08)
    else:
        # the engine backed off 0.1 m: contact was ~0.1 m beyond each leading edge
        d = (0.2 + 0.1 + 0.05) * direction
        b.cmap.mark_occupied(s["x"] + fx * d, s["y"] + fy * d, 0.08)
        e = (hr + 0.1 + 0.03) * direction
        b.cmap.mark_occupied(hx + fx * e, hy + fy * e, 0.08)
    try:
        b.see()
    except Exception:  # noqa: BLE001  (a body without a camera still has the bump mark)
        pass
    b.say("Bumped into something; marked it on my map and replanning around it.")


def route_still_clear(P, pose, segs):
    """Check the remaining segments against a (newer) planner map, with the full footprint."""
    x, y, h = pose
    for seg in segs:
        nx, ny, nh = predict((x, y, h), seg)
        if seg[0] == "move":
            hh = h
            if not P.segment_ok((x, y), (nx, ny), hh, strict=False):
                return False
        elif seg[0] == "turn":
            if not P.sweep_ok(x, y, h, nh, strict=False):
                return False
        else:
            for k in range(1, 9):
                px, py, ph = predict((x, y, h), (seg[0], seg[1], seg[2] * k / 8))
                if not P.pose_ok(px, py, ph, strict=False):
                    return False
        x, y, h = nx, ny, nh
    return True


def navigate(b, goal, final_heading=None, tol=0.08, observe=True, weights=None, log=None, algo="astar",
             smooth=True, fillets=True):
    """Drive the body `b` (LocalBrain-like: st, see, cl, cmap, carry_r, publish, say) to goal."""
    prev_route = None
    last_hit = None
    for attempt in range(REPLANS + 1):
        s = b.st()
        here = (s["x"], s["y"], s["heading"])
        if last_hit is not None and math.hypot(here[0] - last_hit[0], here[1] - last_hit[1]) < 0.04 \
                and abs(wrap(here[2] - last_hit[2])) < 4.0 and attempt >= 2:
            b.say("Same contact twice from here; backing away before planning again.")
            r = b.cl.move(-0.2)
            if r.get("collided"):
                b.cl.move(0.15)
            s = b.st()
            here = (s["x"], s["y"], s["heading"])
        if math.hypot(goal[0] - here[0], goal[1] - here[1]) <= tol:
            if final_heading is not None and abs(wrap(final_heading - here[2])) > 0.5:
                b.cl.turn(wrap(final_heading - here[2]))
            return True
        comp, P = plan_route(b.cmap, here, goal, s["holding"], getattr(b, "carry_r", 0.23), final_heading, weights,
                             algo=algo, smooth=smooth, fillets=fillets)
        if comp is None:
            b.say(f"No safe route to ({goal[0]:.2f}, {goal[1]:.2f}) on what I have seen so far.")
            return False
        if not comp["segments"] or comp["length_m"] < 0.01:     # planner says "here": close the last few cm
            d = math.hypot(goal[0] - here[0], goal[1] - here[1])
            h = math.degrees(math.atan2(-(goal[1] - here[1]), goal[0] - here[0]))
            if d < 0.3 and P.segment_ok(here[:2], goal, h, strict=False) and P.sweep_ok(here[0], here[1], here[2], h, strict=False):
                b.cl.turn(wrap(h - here[2]))
                b.cl.move(d)
            if final_heading is not None:
                s = b.st()
                b.cl.turn(wrap(final_heading - s["heading"]))
            s = b.st()
            return math.hypot(goal[0] - s["x"], goal[1] - s["y"]) <= max(tol, 0.12)
        if log is not None:
            log.append(comp)
        b.cmap.plan = {"x": goal[0], "y": goal[1], "heading": final_heading, "route": comp["route"],
                       "prev_route": prev_route, "stats": {k: comp[k] for k in ("planner", "cost", "length_m", "time_s",
                                                                                "turns", "arcs", "expanded", "plan_ms")},
                       "replan": attempt}
        prev_route = comp["route"]
        if attempt:
            b.say(f"Replanning ({attempt}): {comp['length_m']:.2f} m, {comp['turns']} turns, {comp['arcs']} curves.")
        else:
            b.say(f"Route: {comp['length_m']:.2f} m, about {comp['time_s']:.1f} s, {comp['turns']} turns, "
                  f"{comp['arcs']} curves (A*, {comp['expanded']} states).")
        b.publish()
        pose, replan = here, False
        segs = comp["segments"]
        for n, seg in enumerate(segs):
            want = predict(pose, seg)
            if seg[0] == "turn":
                r = b.cl.turn(seg[1], getattr(b, "speed", 1.0))
                got_h = r["heading"]
                if abs(wrap(got_h - want[2])) > 2.0:          # the turn was blocked
                    bumped(b, 1, turning=seg[1])
                    s = b.st()
                    last_hit = (s["x"], s["y"], s["heading"])
                    replan = True
                    break
            elif seg[0] == "move":
                r = b.cl.move(seg[1], getattr(b, "speed", 1.0))
                if r.get("collided"):
                    bumped(b, 1 if seg[1] > 0 else -1)
                    if abs(r.get("moved", 0.0)) < 0.02:      # stuck against it at the start: step back first
                        b.cl.move(-0.15 if seg[1] > 0 else 0.15)
                    s = b.st()
                    last_hit = (s["x"], s["y"], s["heading"])
                    replan = True
                    break
            else:
                r = b.cl.arc(seg[1], seg[2], getattr(b, "speed", 1.0))
                if r.get("collided"):
                    bumped(b, 1)
                    replan = True
                    break
            s = b.st()
            pose = (s["x"], s["y"], s["heading"])
            if math.hypot(pose[0] - want[0], pose[1] - want[1]) > DEVIATION_M or abs(wrap(pose[2] - want[2])) > DEVIATION_DEG:
                replan = True
                break
            long_seg = (seg[0] == "move" and abs(seg[1]) >= OBSERVE_AFTER_M) or seg[0] == "arc"
            if observe and long_seg and n < len(segs) - 1:
                b.see()                                       # new map information on the way
                P2 = Planner(b.cmap.occ, holding=s["holding"], carry_r=getattr(b, "carry_r", 0.23), weights=weights)
                if not route_still_clear(P2, pose, segs[n + 1:]):
                    b.say("Something new is in the way; replanning.")
                    replan = True
                    break
        if not replan:
            s = b.st()
            if math.hypot(goal[0] - s["x"], goal[1] - s["y"]) <= max(tol, 0.12):
                return True
    return False
