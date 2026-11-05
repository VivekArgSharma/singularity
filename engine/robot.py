"""Robot pose, actions and state machine. Pure Python; no rendering.

Actions are generators: each next() advances the simulation by one fixed
1/60 s step. The engine runs them only while an action is active, so the
clock is frozen between actions.
"""
import math
import os

from grader import grade, grade_near, resolve_reference, resolve_targets
from world import World, bearing_to, forward, wrap180

DT = 1.0 / 60.0
MOVE_SPEED = float(os.environ.get("FETCH_MOVE_SPEED", "1.0"))   # m/s; problem statement: 1.0 (others = experiments)
TURN_SPEED = 90.0       # deg/s
BACK_OFF = 0.1          # m
REACH = 0.5             # m, robot front to object surface
REACH_ANGLE = 30.0      # deg
CARRY_AHEAD = 0.62      # m, robot centre to carried object centre, forward
CARRY_SIDE = 0.36       # m, to the right, so the object sits at the bottom-right of the view
STOW_AHEAD = 0.10       # m, empty hand folded beside the body (front-right)
STOW_SIDE = 0.27        # m
HAND_R = 0.08           # m, collision circle of the folded empty hand
GRIP_OPEN = 0.22        # m, half-gap of open claws
GRIP_STOWED = 0.06      # m, half-gap of closed empty claws
LIFT = 0.04             # m, how far a held object is lifted off the floor
MAX_RETRIES = 3
MAX_MOVE = 5.0
MAX_TURN = 180.0
STALL_TIME = 2.0        # s without progress -> Blocked
STALL_EPS = 0.01        # m
MAX_ARC = 720.0         # deg, one arc call may sweep up to two full loops
MIN_RADIUS, MAX_RADIUS = 0.1, 3.0
MIN_SPEED = 0.1         # optional speed factor for "slowly"; 1.0 = the spec speed
STEP_MAX = 0.05         # m, largest change of floor height the wheels take in one step (no climbing risers)
MAX_SLOPE = 0.4         # rise per metre the body may straddle (a ramp up to ~22 deg)
REACH_DZ = 0.12         # m, the hand only reaches objects standing about level with the wheels

RUNNING = ("Locate", "Go to object", "Pick up", "Carry", "Drop", "Blocked")


class Robot:
    def __init__(self, level="level1", seed=0):
        self.reset(level, seed)

    # ------------------------------------------------------------ lifecycle
    def reset(self, level="level1", seed=0, cfg=None, mission=None):
        self.world = World(level, seed, cfg)
        self.mission = mission              # private mission evaluator (engine side only), or None
        self.x, self.y = self.world.start
        self.heading = float(self.world.start_heading)
        self.r = self.world.robot_r
        self.held = None
        # hand kinematics in the robot frame: (right, ahead) metres, claw half-gap, lift
        self.hand = [STOW_SIDE, STOW_AHEAD]
        self.grip = GRIP_STOWED
        self.hand_z = 0.0
        self.retries = 0
        self.cancel = False      # set by the engine when a newer task takes over
        self.interactive = False # dashboard mode: the operator, not a drop, ends a task
        self.arc_centre = None
        self.state = "Idle"
        self.prev_state = None
        self.command = None
        self.run_id = None
        self.targets = None
        self.outcome = None
        self.stats = {"sim_seconds": 0.0, "collisions": 0, "path_length": 0.0, "actions": 0}

    def set_command(self, text, run_id):
        self.interactive = False
        self.command = text
        self.run_id = run_id
        self.targets = resolve_targets(text, self.world.objects, self.world.zone_c)
        self.near = resolve_reference(text, self.world.objects, self.world.zone_c)
        self.state = "Locate"

    def continue_task(self, text):
        """Sandbox demos: a new command on the same scene and pose, fresh run counters."""
        for o in self.world.objects:
            o.delivered = False
        self.command = text
        self.targets = resolve_targets(text, self.world.objects, self.world.zone_c)
        if self.targets is None and self.held is not None:
            self.targets = {self.held.id}      # "now put it on the green tile": 'it' is what the robot holds
        self.near = resolve_reference(text, self.world.objects, self.world.zone_c)
        self.retries = 0
        self.outcome = None
        self.state = "Carry" if self.held is not None else "Locate"

    @property
    def running(self):
        return self.state in RUNNING

    @property
    def z(self):
        """Height of the floor under the robot (0 except on a ramp or platform)."""
        return self.world.support_height(self.x, self.y, ignore=self.held)

    def public_state(self):
        st = {"name": self.state, "x": round(self.x, 3), "y": round(self.y, 3),
              "heading": round(self.heading, 1), "holding": self.held is not None,
              "retries": self.retries}
        if self.world.elevated:            # mission extension: the wheels' own height (odometry, not scene data)
            st["z"] = round(self.z, 3)
        return st

    def after_action(self):
        """Called by the engine when an action finishes: the private mission evaluator looks at the
        verified simulator state (e.g. the robot is standing on the maze exit)."""
        if self.mission is not None and self.state != "Done":
            out = self.mission.after_action(self)
            if out is not None:
                self._finish(out)

    def result(self):
        return {"outcome": self.outcome, "state": self.state, "command": self.command,
                "run_id": self.run_id,
                "sim_seconds": round(self.stats["sim_seconds"], 2),
                "collisions": self.stats["collisions"],
                "path_length": round(self.stats["path_length"], 2),
                "actions": self.stats["actions"],
                "delivered": self._delivered_desc()}

    def _delivered_desc(self):
        for o in self.world.objects:
            if o.delivered:
                return {"id": o.id, "shape": o.shape, "color": o.color_name}
        return None

    # -------------------------------------------------------------- geometry
    @property
    def front(self):
        fx, fy = forward(self.heading)
        return self.x + fx * self.r, self.y + fy * self.r

    def local_to_world(self, right, ahead, x=None, y=None, heading=None):
        x = self.x if x is None else x
        y = self.y if y is None else y
        h = self.heading if heading is None else heading
        fx, fy = forward(h)
        rx, ry = forward(h + 90.0)              # right-hand side (clockwise +90)
        return x + fx * ahead + rx * right, y + fy * ahead + ry * right

    def world_to_local(self, px, py):
        fx, fy = forward(self.heading)
        rx, ry = forward(self.heading + 90.0)
        dx, dy = px - self.x, py - self.y
        return dx * rx + dy * ry, dx * fx + dy * fy

    def carry_point(self, x=None, y=None, heading=None):
        return self.local_to_world(CARRY_SIDE, CARRY_AHEAD, x, y, heading)

    def hand_world(self, x=None, y=None, heading=None):
        return self.local_to_world(self.hand[0], self.hand[1], x, y, heading)

    def _pose_blocked(self, x, y, heading):
        """Collision test for a candidate pose. If the robot is already in contact here (an object
        lifted into the carry position right next to a wall), a step that increases the clearance of
        the hand is allowed, so it can always back out; it can never push further in."""
        if not self._pose_blocked_raw(x, y, heading):
            return False
        if not self._pose_blocked_raw(self.x, self.y, self.heading):
            return True
        if self._body_blocked(x, y):
            return True
        return self._hand_clearance(x, y, heading) < self._hand_clearance(self.x, self.y, self.heading) - 1e-6

    def _body_blocked(self, x, y):
        w = self.world
        z = w.support_height(x, y, ignore=self.held) if w.elevated else 0.0
        if w.elevated and abs(z - self.z) > STEP_MAX:
            return True
        return w.circle_blocked(x, y, self.r, ignore=self.held, z=z)

    def _hand_clearance(self, x, y, heading):
        """Distance from the hand (or carried object's centre) to the nearest wall or solid."""
        w = self.world
        hx, hy = self.hand_world(x, y, heading)
        d = min(w.half - abs(hx), w.half - abs(hy))
        for o in w.solids(self.held, w.support_height(x, y, ignore=self.held) if w.elevated else 0.0):
            d = min(d, o.dist_to_point(hx, hy))
        return d

    def _pose_blocked_raw(self, x, y, heading):
        """Body, plus the hand: the held object when carrying, the folded hand when empty.
        With ramps/platforms: the wheels cannot climb or drop an edge (STEP_MAX), and no part of the
        body or hand may push into a surface that stands higher than it can straddle."""
        w = self.world
        z = 0.0
        if w.elevated:
            z = w.support_height(x, y, ignore=self.held)
            if abs(z - self.z) > STEP_MAX:
                return True
            lim = z + STEP_MAX + self.r * MAX_SLOPE
            for k in range(8):
                a = math.radians(45 * k)
                if w.support_height(x + math.cos(a) * self.r, y + math.sin(a) * self.r, ignore=self.held) > lim:
                    return True
        if w.circle_blocked(x, y, self.r, ignore=self.held, z=z):
            return True
        hx, hy = self.hand_world(x, y, heading)
        r = self.held.carry_r() + 0.02 if self.held is not None else HAND_R
        if w.elevated:
            if self.held is not None and self.held.is_box:     # the carried box's real outline
                o = self.held
                yaw = math.radians(heading + o.yaw_off)
                fx, fy = math.cos(yaw), -math.sin(yaw)          # the box's own +x axis; +y is (-fy, fx)
                pts = []
                for a in (-1, -0.5, 0, 0.5, 1):                 # points along its outline
                    for b in (-1, 1):
                        for lx, ly in ((a * o.hx, b * o.hy), (b * o.hx, a * o.hy)):
                            pts.append((hx + fx * lx - fy * ly, hy + fy * lx + fx * ly))
            else:
                pts = [(hx + math.cos(math.radians(45 * k)) * r, hy + math.sin(math.radians(45 * k)) * r)
                       for k in range(8)] + [(hx, hy)]
            # like the body: the hand may hang over a gentle slope, never push into a riser
            under = z + self.hand_z + STEP_MAX + 0.45 * MAX_SLOPE
            if any(w.support_height(px, py, ignore=self.held) > under for px, py in pts):
                return True
            if self.held is not None and self.held.is_box:
                return any(w.circle_blocked(px, py, 0.02, ignore=self.held, z=z) for px, py in pts) or \
                    w.circle_blocked(hx, hy, 0.02, ignore=self.held, z=z)
        return w.circle_blocked(hx, hy, r, ignore=self.held, z=z)

    def depth(self):
        fx, fy = forward(self.heading)
        ox, oy = self.x + fx * self.r, self.y + fy * self.r
        d = self.world.ray(ox, oy, self.heading, ignore=self.held, z=self.z)
        return None if d is None else round(d, 3)

    # ---------------------------------------------------------- state helpers
    def _resume(self):
        if self.state == "Blocked":
            self.state = self.prev_state or "Locate"

    def _enter_blocked(self):
        self.stats["collisions"] += 1
        self.retries += 1
        self.prev_state = self.state if self.state != "Blocked" else self.prev_state
        self.state = "Blocked"
        limit = self.mission.cfg.get("max_collisions", MAX_RETRIES) if self.mission is not None else MAX_RETRIES
        if self.retries > limit:
            self._finish("fail_collision")

    def _finish(self, outcome):
        self.state = "Done"
        self.outcome = outcome

    def _tick(self):
        self.stats["sim_seconds"] += DT

    # --------------------------------------------------------------- actions
    def move(self, distance, speed=1.0):
        if not self.running:
            return {"moved": 0.0, "collided": False}
        self.stats["actions"] += 1
        v = MOVE_SPEED * _speed(speed)
        d = max(-MAX_MOVE, min(MAX_MOVE, float(distance)))
        self._resume()
        if self.state == "Locate" and d > 0 and self.held is None:
            self.state = "Go to object"
        sign = 1.0 if d >= 0 else -1.0
        fx, fy = forward(self.heading)
        x0, y0 = self.x, self.y
        remaining, collided = abs(d), False
        stall_t, stall_ref = 0.0, (self.x, self.y)      # measured on the real position, not the command
        interrupted = False
        while remaining > 1e-9:
            if self.cancel:                         # newer task: stop where we are
                interrupted = True
                break
            s = min(v * DT, remaining)
            nx, ny = self.x + fx * s * sign, self.y + fy * s * sign
            if self._pose_blocked(nx, ny, self.heading):
                collided = True
                break
            self.stats["path_length"] += math.hypot(nx - self.x, ny - self.y)
            self.x, self.y = nx, ny
            remaining -= s
            stall_t += DT
            if stall_t >= STALL_TIME:
                if math.hypot(self.x - stall_ref[0], self.y - stall_ref[1]) < STALL_EPS:
                    collided = True                 # 2 s without getting anywhere -> Blocked
                    break
                stall_t, stall_ref = 0.0, (self.x, self.y)
            self._tick()
            yield
        if collided:
            # stop is visible: hold briefly, then back off 0.1 m
            for _ in range(6):
                self._tick()
                yield
            back = BACK_OFF
            while back > 1e-9:
                s = min(MOVE_SPEED * DT, back)
                nx, ny = self.x - fx * s * sign, self.y - fy * s * sign
                if not self._pose_blocked(nx, ny, self.heading):
                    self.stats["path_length"] += math.hypot(nx - self.x, ny - self.y)
                    self.x, self.y = nx, ny
                back -= s
                self._tick()
                yield
            self._enter_blocked()
        moved = (self.x - x0) * fx + (self.y - y0) * fy          # real displacement along the heading
        return {"moved": round(moved, 3), "collided": collided, "interrupted": interrupted}

    def turn(self, degrees, speed=1.0):
        if not self.running:
            return {"heading": round(self.heading, 1)}
        self.stats["actions"] += 1
        w = TURN_SPEED * _speed(speed)
        deg = max(-MAX_TURN, min(MAX_TURN, float(degrees)))
        self._resume()
        sign = 1.0 if deg >= 0 else -1.0
        remaining = abs(deg)
        collided = interrupted = False
        while remaining > 1e-9:
            if self.cancel:
                interrupted = True
                break
            s = min(w * DT, remaining)
            nh = wrap180(self.heading + sign * s)
            if self._pose_blocked(self.x, self.y, nh):
                for _ in range(6):
                    self._tick()
                    yield
                self._enter_blocked()
                collided = True
                break
            self.heading = nh
            remaining -= s
            self._tick()
            yield
        return {"heading": round(self.heading, 1), "collided": collided, "interrupted": interrupted}

    def arc(self, radius, degrees, speed=1.0):
        """Drive along a true circle. The turning centre sits `radius` m to the right
        (degrees > 0, clockwise) or left (degrees < 0) and stays fixed, so the path is
        x = cx + r cos(t), y = cy + r sin(t) with the heading always tangent to it.
        Speed is 1 m/s along the path, capped so the heading never turns faster than 90 deg/s."""
        if not self.running:
            return {"moved": 0.0, "heading": round(self.heading, 1), "collided": False}
        self.stats["actions"] += 1
        r = max(MIN_RADIUS, min(MAX_RADIUS, float(radius)))
        deg = max(-MAX_ARC, min(MAX_ARC, float(degrees)))
        self._resume()
        if self.state == "Locate" and self.held is None:
            self.state = "Go to object"
        sign = 1.0 if deg >= 0 else -1.0
        k = _speed(speed)
        v = min(MOVE_SPEED * k, r * math.radians(TURN_SPEED * k))     # m/s along the arc
        step = math.degrees(v * DT / r)                                  # heading change per step
        rx, ry = forward(self.heading + 90.0)
        cx, cy = self.x + sign * rx * r, self.y + sign * ry * r          # fixed centre
        self.arc_centre = (cx, cy, r)
        h = self.heading                                                 # unwrapped while sweeping
        remaining, swept, collided, interrupted = abs(deg), 0.0, False, False
        while remaining > 1e-9:
            if self.cancel:
                interrupted = True
                break
            s = min(step, remaining)
            nh = h + sign * s
            nrx, nry = forward(nh + 90.0)
            nx, ny = cx - sign * nrx * r, cy - sign * nry * r            # on the circle, tangent heading
            if self._pose_blocked(nx, ny, nh):
                collided = True
                break
            self.stats["path_length"] += math.hypot(nx - self.x, ny - self.y)
            h = nh
            self.x, self.y, self.heading = nx, ny, wrap180(nh)
            remaining -= s
            swept += s
            self._tick()
            yield
        if collided:
            for _ in range(6):
                self._tick()
                yield
            fx, fy = forward(self.heading)
            back = BACK_OFF
            while back > 1e-9:
                s = min(MOVE_SPEED * DT, back)
                nx, ny = self.x - fx * s, self.y - fy * s
                if not self._pose_blocked(nx, ny, self.heading):
                    self.x, self.y = nx, ny
                    self.stats["path_length"] += s
                back -= s
                self._tick()
                yield
            self._enter_blocked()
        self.arc_centre = None
        return {"moved": round(math.radians(swept) * r, 3), "swept": round(sign * swept, 1),
                "heading": round(self.heading, 1), "collided": collided, "interrupted": interrupted}

    def pickup(self):
        if not self.running or self.held is not None:
            return {"ok": False}
        self.stats["actions"] += 1
        self._resume()
        fx_, fy_ = self.front
        best, best_d = None, None
        z = self.z
        for o in self.world.objects:
            if o.held or abs(o.z - z) > REACH_DZ:   # too high (or low) for the hand: not reachable from here
                continue
            surf = o.dist_to_point(fx_, fy_)
            rel = bearing_to(self.x, self.y, self.heading, o.x, o.y)
            if surf <= REACH and abs(rel) <= REACH_ANGLE and (best is None or surf < best_d):
                best, best_d = o, surf
        if best is None:
            return {"ok": False}
        self.state = "Pick up"
        o = best
        ol = self.world_to_local(o.x, o.y)
        hw = self._half_width(o)                  # object's half-width across the claws
        grip_open = max(GRIP_OPEN, hw + 0.06)
        clear = o.h + 0.05                        # claw bottoms above the object's top
        # come from above so no part of the hand ever passes through the object:
        # raise (open) -> travel over it -> lower around it -> close -> lift and carry
        before = self.prev_state if self.state == "Blocked" else "Locate"
        phases = ((self.hand, grip_open, clear, 10), (ol, grip_open, clear, 14),
                  (ol, grip_open, 0.0, 12), (ol, hw + 0.004, 0.0, 8))
        for target, g, z, n in phases:
            if self.cancel:                       # not grasped yet: lift clear, fold back, empty-handed
                yield from self._hand_to(self.hand, grip_open, clear, 10)
                yield from self._hand_to((STOW_SIDE, STOW_AHEAD), grip_open, clear, 14)
                yield from self._hand_to((STOW_SIDE, STOW_AHEAD), GRIP_STOWED, 0.0, 10)
                self.state = before
                return {"ok": False, "interrupted": True}
            yield from self._hand_to(target, g, z, n)
        o.held = True
        o.yaw_off = o.yaw - self.heading
        self.held = o
        yield from self._hand_to((CARRY_SIDE, CARRY_AHEAD), hw + 0.004, LIFT, 18)
        self.state = "Carry"
        return {"ok": True}

    def _half_width(self, o):
        """Half the object's width along the robot's left-right axis (claw direction)."""
        if not o.is_box:
            return o.r
        d = math.radians(o.yaw - self.heading)
        return o.hx * abs(math.sin(d)) + o.hy * abs(math.cos(d))

    def _hand_to(self, target, grip, z, frames):
        """Ease the hand (right, ahead), claw half-gap and height to a target over frames."""
        s0, a0, g0, z0 = self.hand[0], self.hand[1], self.grip, self.hand_z
        for i in range(1, frames + 1):
            k = i / frames
            k = k * k * (3 - 2 * k)               # smoothstep: no jerk at start and end
            self.hand = [s0 + (target[0] - s0) * k, a0 + (target[1] - a0) * k]
            self.grip = g0 + (grip - g0) * k
            self.hand_z = z0 + (z - z0) * k
            if self.held is not None:
                self.held.x, self.held.y = self.hand_world()
                self.held.yaw = self.heading + self.held.yaw_off
            self._tick()
            yield

    def drop(self):
        if not self.running or self.held is None:
            return {"ok": False, "in_zone": False}
        self.stats["actions"] += 1
        self._resume()
        self.state = "Drop"
        o = self.held
        here = tuple(self.hand)
        clear = o.h + 0.05
        grip_open = max(GRIP_OPEN, self.grip + 0.06)
        # lower onto the floor -> open -> rise clear of the object -> fold back -> settle
        yield from self._hand_to(here, self.grip, LIFT / 2, 5)
        if self.cancel:                           # not released yet: lift back, still carrying
            yield from self._hand_to(here, self.grip, LIFT, 5)
            self.state = "Carry"
            return {"ok": False, "in_zone": False, "interrupted": True}
        yield from self._hand_to(here, self.grip, 0.0, 5)
        o.x, o.y = self.hand_world()
        o.yaw = self.heading + o.yaw_off
        z_here = self.z
        o.held = False
        o.delivered = True
        self.held = None
        o.z = self.world.support_height(o.x, o.y, ignore=o) if self.world.elevated else 0.0
        in_zone = self.world.in_zone(o.x, o.y) and o.z < 0.02
        verdict = self.mission.on_drop(self, o, z_here) if self.mission is not None else None
        yield from self._hand_to(here, grip_open, 0.0, 8)
        yield from self._hand_to(here, grip_open, clear, 10)
        yield from self._hand_to((STOW_SIDE, STOW_AHEAD), grip_open, clear, 14)
        yield from self._hand_to((STOW_SIDE, STOW_AHEAD), GRIP_STOWED, 0.0, 10)
        if self.mission is not None:              # missions: the private evaluator decides; usually more to do
            if verdict is not None:
                self._finish(verdict)
            else:
                self.state = "Locate"
            return {"ok": True, "in_zone": self.world.zone_at(o.x, o.y) is not None and o.z < 0.02}
        if getattr(self, "near", None) is not None:   # "put it next to the red cube"
            refs = [q for q in self.world.objects if q.id in self.near and q is not o]
            verdict = grade_near(o, refs, self.targets)
        else:
            verdict = grade(o, in_zone, self.targets)
        if self.interactive:                      # graded, but the robot stays usable for the task
            self.outcome = verdict
            self.state = "Locate"
        else:
            self._finish(verdict)
        return {"ok": True, "in_zone": in_zone}

    def give_up(self):
        if self.state != "Done":
            self._finish("fail_not_reached")

    def verdict(self, passed):
        if self.outcome == "needs_manual":
            self.outcome = "pass" if passed else "fail_wrong_object"
        return self.outcome

    # ---------------------------------------------------- held-object render
    def held_pose(self):
        """World pose for drawing the held object: (x, y, lift in metres)."""
        if self.held is None:
            return None
        hx, hy = self.hand_world()
        return hx, hy, self.hand_z + self.z


def _speed(k):
    """Optional speed factor for 'slowly' commands; never above the spec speed."""
    try:
        k = float(k)
    except (TypeError, ValueError):
        return 1.0
    return max(MIN_SPEED, min(1.0, k)) if math.isfinite(k) else 1.0


def heading_to(x, y, tx, ty):
    """Absolute clockwise heading from (x, y) to (tx, ty). Used by tests only."""
    return math.degrees(math.atan2(-(ty - y), tx - x))
