"""Panda3D scene, robot cameras and the operator window. Rendering only."""
import io
import math
import os

import numpy as np
from PIL import Image
from panda3d.core import (AmbientLight, BitMask32, Camera, DirectionalLight,
                          FrameBufferProperties, NodePath, OrthographicLens, PerspectiveLens,
                          PNMImage, SamplerState, Texture, TextNode, Vec4)

import meshes

LOOK_W, LOOK_H = 640, 480
LOWMEM = os.environ.get("FETCH_LOWMEM", "") in ("1", "true", "yes")
CLIP_W, CLIP_H = (960, 720) if LOWMEM else (1440, 1080)
SIDE_W, SIDE_H = (640, 480) if LOWMEM else (960, 720)          # people-only chase and top-down views
H_FOV = 90.0
V_FOV = math.degrees(2 * math.atan(math.tan(math.radians(H_FOV / 2)) * 3 / 4))  # 73.74
CAM_H = float(os.environ.get("FETCH_CAM_H", "0.5"))      # problem statement: 0.5 m (other values are experiments only)
CAM_TILT = 10.0
SKY = Vec4(0.80, 0.82, 0.85, 1)

MASK_OVERVIEW = BitMask32.bit(1)
MASK_ROBOT = BitMask32.bit(2)
MASK_SHADOW = BitMask32.bit(3)


def panda_h(heading):
    """Clockwise world heading -> Panda3D H (counter-clockwise, 0 = +Y)."""
    return -heading - 90.0


class Views:
    def __init__(self, base, headless):
        self.base = base
        self.headless = headless
        base.disableMouse()
        base.setBackgroundColor(SKY)
        r = base.render
        amb = AmbientLight("amb")
        amb.setColor(Vec4(0.56, 0.56, 0.58, 1))
        # key light with real shadow maps, plus a soft fill from the other side
        sun = DirectionalLight("sun")
        sun.setColor(Vec4(0.72, 0.70, 0.66, 1))
        sun.setShadowCaster(True, 2048, 2048)
        sun.setCameraMask(MASK_SHADOW)
        sun.getLens().setFilmSize(9.5, 9.5)
        sun.getLens().setNearFar(1, 30)
        self.sun_np = r.attachNewNode(sun)
        self.sun_np.setPos(3.0, -2.5, 9.0)
        self.sun_np.lookAt(0, 0, 0)
        fill = DirectionalLight("fill")
        fill.setColor(Vec4(0.16, 0.16, 0.18, 1))
        fill_np = r.attachNewNode(fill)
        fill_np.setHpr(150, -35, 0)
        r.setLight(r.attachNewNode(amb))
        r.setLight(self.sun_np)
        r.setLight(fill_np)
        r.setShaderAuto()
        self.scene = None
        self.robot_np = None
        self._make_buffers()
        if not headless:
            self._make_window_views()

    # ------------------------------------------------------------ cameras
    def _lens(self, w, h):
        lens = PerspectiveLens()
        lens.setFov(H_FOV, math.degrees(2 * math.atan(math.tan(math.radians(H_FOV / 2)) * h / w)))
        lens.setNearFar(0.03, 30)
        return lens

    def _buffer(self, name, w, h, lens, mask, parent):
        b = self.base
        fbp = FrameBufferProperties()
        fbp.setRgbColor(True)
        fbp.setRgbaBits(8, 8, 8, 8)
        fbp.setDepthBits(24)
        fbp.setMultisamples(0 if LOWMEM else 4)
        tex = Texture(name)
        buf = b.win.makeTextureBuffer(name, w, h, tex, True, fbp)
        if buf is None:  # driver without multisampled buffers
            buf = b.win.makeTextureBuffer(name, w, h, tex, to_ram=True)
        buf.setClearColor(SKY)
        cam = b.makeCamera(buf, lens=lens)
        cam.node().setCameraMask(mask)
        cam.reparentTo(parent)
        buf.setActive(False)
        self.bufs[name] = (buf, tex, w, h)
        return cam

    def _make_buffers(self):
        self.mount = NodePath("cam_mount")            # robot camera, follows the robot
        self.chase_mount = self.base.render.attachNewNode("chase_mount")   # third-person, follows the robot
        self.top_mount = self.base.render.attachNewNode("top_mount")       # fixed overhead; never removed on reset
        self.bufs = {}
        # first person: what Claude sees (fixed spec) and the clip at the same framing
        self._buffer("look", LOOK_W, LOOK_H, self._lens(LOOK_W, LOOK_H), MASK_ROBOT, self.mount)
        self._buffer("clip", CLIP_W, CLIP_H, self._lens(CLIP_W, CLIP_H), MASK_ROBOT, self.mount)
        # people-only views (never sent to the agent): chase and top-down
        chase = PerspectiveLens()
        chase.setFov(70, 54.5)
        chase.setNearFar(0.05, 40)
        self._buffer("chase", SIDE_W, SIDE_H, chase, MASK_OVERVIEW, self.chase_mount)
        top = OrthographicLens()
        top.setFilmSize(8.0, 6.0)
        top.setNearFar(0.5, 30)
        cam = self._buffer("top", SIDE_W, SIDE_H, top, MASK_OVERVIEW, self.top_mount)
        cam.setPos(0, 0, 12)
        cam.setHpr(0, -90, 0)

    def _make_window_views(self):
        """Operator window: main view (chase or overview) on the left, the robot camera
        top-right at an exact 4:3, the top-down view bottom-right. People only."""
        b = self.base
        W, H = b.win.getXSize(), b.win.getYSize()
        main_w = W - 520
        fx = main_w / W
        pov_h = 390 / H
        for i in range(b.win.getNumDisplayRegions()):   # retire ShowBase's default view
            dr = b.win.getDisplayRegion(i)
            if dr.getCamera() == b.cam:
                dr.setActive(False)

        def region(l, r, bt, t, sort):
            dr = b.win.makeDisplayRegion(l, r, bt, t)
            dr.setSort(sort)
            dr.setClearColorActive(True)
            dr.setClearColor(SKY)
            dr.setClearDepthActive(True)
            return dr

        def persp(hfov, aspect):
            lens = PerspectiveLens()
            lens.setFov(hfov, math.degrees(2 * math.atan(math.tan(math.radians(hfov / 2)) / aspect)))
            lens.setNearFar(0.05, 40)
            return lens

        main_aspect = main_w / H
        self.dr_main = region(0, fx, 0, 1, 1)
        over = Camera("overview_win", persp(70, main_aspect))
        over.setCameraMask(MASK_OVERVIEW)
        self.overview_np = b.render.attachNewNode(over)
        self.overview_np.setPos(0, -6.4, 7.2)
        self.overview_np.lookAt(0, -0.3, 0)
        chase = Camera("chase_win", persp(72, main_aspect))
        chase.setCameraMask(MASK_OVERVIEW)
        self.chase_win_np = self.chase_mount.attachNewNode(chase)
        self.dr_main.setCamera(self.chase_win_np)

        dr_pov = region(fx, 1, 1 - pov_h, 1, 2)
        pov = Camera("pov_win", self._lens(4, 3))
        pov.setCameraMask(MASK_ROBOT)
        dr_pov.setCamera(self.mount.attachNewNode(pov))

        dr_top = region(fx, 1, 0, 1 - pov_h, 3)
        top_aspect = 520 / (H * (1 - pov_h))
        tlens = OrthographicLens()
        tlens.setFilmSize(6.4 * top_aspect, 6.4)
        tlens.setNearFar(0.5, 30)
        top = Camera("top_win", tlens)
        top.setCameraMask(MASK_OVERVIEW)
        tnp = self.top_mount.attachNewNode(top)
        tnp.setPos(0, 0, 12)
        tnp.setHpr(0, -90, 0)
        dr_top.setCamera(tnp)

        a = W / H

        def label(text, x_frac, y_frac, scale=0.038):
            tn = TextNode("label")
            tn.setText(text)
            tn.setTextColor(1, 1, 1, 1)
            tn.setCardColor(0.08, 0.08, 0.10, 0.72)
            tn.setCardAsMargin(0.35, 0.35, 0.25, 0.2)
            np_ = b.aspect2d.attachNewNode(tn)
            np_.setScale(scale)
            np_.setPos((x_frac * 2 - 1) * a + 0.03, 0, y_frac * 2 - 1)
            return tn

        self.main_label = label("Chase view - people only     [1] overview  [2] chase", 0, 0.025)
        label("Robot camera - sent to Claude", fx, 1 - 0.045)
        label("Top-down - people only", fx, 0.025)
        self.hud = TextNode("hud")
        self.hud.setTextColor(1, 1, 1, 1)
        self.hud.setCardColor(0.08, 0.08, 0.10, 0.72)
        self.hud.setCardAsMargin(0.4, 0.4, 0.3, 0.25)
        hud_np = b.aspect2d.attachNewNode(self.hud)
        hud_np.setScale(0.036)
        hud_np.setPos(-a + 0.04, 0, 0.93)
        b.accept("1", self._main_view, ["overview"])
        b.accept("2", self._main_view, ["chase"])

    def _main_view(self, which):
        if which == "overview":
            self.dr_main.setCamera(self.overview_np)
            self.main_label.setText("Overview - people only     [1] overview  [2] chase")
        else:
            self.dr_main.setCamera(self.chase_win_np)
            self.main_label.setText("Chase view - people only     [1] overview  [2] chase")

    def set_trail(self, pts):
        """Redraw the driven path (amber line on the floor), only when it has grown."""
        if len(pts) == self._trail_n or len(pts) < 2:
            return
        from panda3d.core import LineSegs
        self._trail_n = len(pts)
        self.trail_np.getChildren().detach()
        ls = LineSegs("trail")
        ls.setThickness(3)
        ls.setColor(0.95, 0.69, 0.20, 1)
        ls.moveTo(pts[0][0], pts[0][1], 0.012)
        for x, y in pts[1:]:
            ls.drawTo(x, y, 0.012)
        self.trail_np.attachNewNode(ls.create())

    def set_hud(self, text):
        if not self.headless:
            self.hud.setText(text)

    # --------------------------------------------------------------- scene
    def build(self, world):
        if self.scene is not None:
            self.scene.removeNode()
        r = self.base.render
        self.scene = r.attachNewNode("scene")
        s = world.half
        wall_h = world.wall_h

        # floor: light warm-grey tiles (0.5 m) with thin grout lines
        img = PNMImage(128, 128)
        img.fill(0.74, 0.72, 0.68)
        for i in range(128):
            for j in (0, 1, 64, 65):
                img.setXel(i, j, 0.60, 0.58, 0.55)
                img.setXel(j, i, 0.60, 0.58, 0.55)
        tex = Texture("floor")
        tex.load(img)
        tex.setWrapU(SamplerState.WM_repeat)
        tex.setWrapV(SamplerState.WM_repeat)
        tex.setMinfilter(SamplerState.FT_linear_mipmap_linear)
        tex.setAnisotropicDegree(8)
        floor = self.scene.attachNewNode(meshes.flat_rect(-s, -s, s, s, uv_scale=1.0, name="floor"))
        floor.setTexture(tex)

        # walls and ceiling (ceiling hidden from the overview camera)
        T, L = 0.1, 2 * s + 0.2
        for px, py, sx, sy in ((0, s + T / 2, L, T), (0, -s - T / 2, L, T),
                               (s + T / 2, 0, T, L), (-s - T / 2, 0, T, L)):
            w = self.scene.attachNewNode(meshes.box(sx, sy, wall_h, name="wall"))
            w.setPos(px, py, 0)
            w.setColor(0.64, 0.68, 0.74, 1)
        if world.cfg["room"].get("ceiling", True):
            ceil = self.scene.attachNewNode(meshes.flat_rect(-s, -s, s, s, name="ceiling"))
            ceil.setPos(0, 0, wall_h)
            ceil.setP(180)
            ceil.setTwoSided(True)
            ceil.setColor(0.88, 0.88, 0.86, 1)
            ceil.hide(MASK_OVERVIEW | MASK_SHADOW)

        # drop zone(s): the green square, or the mission's coloured corner squares; maze exit tile
        tiles = [(z["center"], z["size"], z["rgb"]) for z in world.zones if z["size"] > 0]
        if world.exit:
            tiles.append((world.exit["center"], world.exit["size"], world.exit["rgb"]))
        for (zx, zy), size, rgb in tiles:
            h = size / 2
            zone = self.scene.attachNewNode(meshes.flat_rect(-h, -h, h, h, name="zone"))
            zone.setPos(zx, zy, 0.002)
            zone.setColor(*rgb, 1)
            zone.setDepthOffset(2)
            zone.setLightOff(1)
            zone.setColorScale(1, 1, 1, 1)

        # obstacles (square boxes, or long maze walls) and platforms
        for ob in list(world.obstacles) + list(world.platforms):
            n = self.scene.attachNewNode(meshes.box(ob.sx, ob.sy, ob.h, name="obstacle"))
            n.setPos(ob.x, ob.y, 0)
            n.setH(-ob.yaw)
            n.setColor(*ob.rgb, 1)

        # objects: pivot at the object centre, geometry offset so the base sits at z=0
        self.obj_nodes = {}
        for o in world.objects:
            pivot = self.scene.attachNewNode(f"p_{o.id}")
            if o.shape == "ramp":
                g = meshes.wedge(o.sx, o.sy, o.h)
            elif o.shape == "cube":
                g = meshes.box(o.size, o.size, o.size)
            elif o.shape == "sphere":
                g = meshes.sphere(o.size / 2)
            elif o.shape == "cylinder":
                g = meshes.cylinder(o.radius, o.h)
            else:
                g = meshes.cone(o.radius, o.h)
            geom = pivot.attachNewNode(g)
            geom.setZ(-o.h / 2)
            geom.setColor(*o.rgb, 1)
            if o.shape == "ramp" and o.lip:                        # hinged lip plate past the high end
                lip = pivot.attachNewNode(meshes.box(o.lip + 0.02, o.sy * 0.9, 0.015, name="lip"))
                lip.setPos(o.hx + o.lip / 2, 0, o.h / 2 - 0.015)
                lip.setColor(0.62, 0.62, 0.64, 1)
            if o.lid:
                for z in (-o.h / 2, o.h / 2 - 0.022):          # silver rims: reads as a drink can
                    rim = pivot.attachNewNode(meshes.cylinder(o.radius * 1.03, 0.022, name="rim"))
                    rim.setZ(z)
                    rim.setColor(0.78, 0.79, 0.82, 1)
                lid = pivot.attachNewNode(meshes.cylinder(o.radius * 0.82, 0.015, name="lid"))
                lid.setZ(o.h / 2)
                lid.setColor(0.72, 0.72, 0.74, 1)
                tab = pivot.attachNewNode(meshes.box(0.05, 0.025, 0.012, name="tab"))
                tab.setPos(0, o.radius * 0.35, o.h / 2 + 0.015)
                tab.setColor(0.6, 0.6, 0.62, 1)
            shadow = self.scene.attachNewNode(meshes.disc(o.r * 1.15, name="shadow"))
            shadow.setColor(0, 0, 0, 0.28)
            shadow.setTransparency(1)
            shadow.setLightOff(1)
            shadow.setDepthOffset(3)
            shadow.setDepthWrite(False)
            shadow.hide()          # real shadow maps replace the contact discs
            self.obj_nodes[o.id] = (pivot, shadow)

        # robot (local +Y = forward, +X = right). The body is hidden from its own camera;
        # the gripper is not, so the robot sees its own claws at the bottom-right.
        from robot import CARRY_AHEAD, CARRY_SIDE, REACH, REACH_ANGLE
        self.robot_np = self.scene.attachNewNode("robot")
        bodyg = self.robot_np.attachNewNode("body_group")
        body = bodyg.attachNewNode(meshes.cylinder(world.robot_r, 0.34, name="body"))
        body.setZ(0.05)
        body.setColor(0.20, 0.22, 0.25, 1)
        top = bodyg.attachNewNode(meshes.sphere(world.robot_r * 0.9, name="dome"))
        top.setZ(0.22)
        top.setColor(0.26, 0.28, 0.32, 1)
        for side in (-1, 1):
            wh = bodyg.attachNewNode("wheel")             # pivot at the wheel's centre
            tyre = wh.attachNewNode(meshes.cylinder(0.07, 0.04, name="tyre"))
            tyre.setZ(-0.02)
            wh.setR(90)
            wh.setPos(side * (world.robot_r - 0.02), 0, 0.07)   # outer face flush with the collision circle
            wh.setColor(0.07, 0.07, 0.07, 1)
        head = bodyg.attachNewNode(meshes.box(0.10, 0.06, 0.06, name="camera_head"))
        head.setPos(0, world.robot_r - 0.03, CAM_H - 0.03)
        head.setColor(0.12, 0.12, 0.14, 1)
        bodyg.hide(MASK_ROBOT)

        # gripper "hand": a fixed shoulder on the right of the body, a boom that stretches
        # from the shoulder to the hand, and the hand itself (fork under the object, palm
        # behind it, two tall claws either side). The hand is posed every frame from the
        # robot's hand kinematics, so it reaches, grasps, carries, releases and folds.
        graphite, steel, rubber = (0.17, 0.18, 0.20, 1), (0.66, 0.68, 0.72, 1), (0.10, 0.10, 0.11, 1)
        self.shoulder_xy = (0.19, 0.05)
        shoulder = self.robot_np.attachNewNode(meshes.box(0.07, 0.12, 0.12, name="shoulder"))
        shoulder.setPos(self.shoulder_xy[0], self.shoulder_xy[1], 0.02)
        shoulder.setColor(*graphite)
        self.boom = self.robot_np.attachNewNode(meshes.box(0.045, 1.0, 0.04, name="boom"))
        self.boom.setColor(*graphite)
        self.hand_np = self.robot_np.attachNewNode("hand")
        self.fork = self.hand_np.attachNewNode(meshes.box(0.40, 0.12, 0.025, name="fork"))
        self.fork.hide()                              # grasped from above: no fork under the object
        self.fork.setZ(0.006)
        self.fork.setColor(*graphite)
        self.palm = self.hand_np.attachNewNode(meshes.box(0.40, 0.03, 0.14, name="palm"))
        self.palm.setColor(*steel)
        self.fingers = []
        for side in (-1, 1):
            f = self.hand_np.attachNewNode("claw")
            plate = f.attachNewNode(meshes.box(0.026, 0.18, 0.34, name="claw_plate"))
            plate.setColor(*steel)
            pad = f.attachNewNode(meshes.box(0.008, 0.14, 0.24, name="claw_pad"))
            pad.setPos(-side * 0.017, 0, 0.05)        # rubber pad on the inner face
            pad.setColor(*rubber)
            self.fingers.append((f, side))

        # people-only overlays (never in the robot camera, never cast shadows)
        overlay_mask = MASK_ROBOT | MASK_SHADOW
        self.trail_np = self.scene.attachNewNode("trail")        # where the robot has driven
        self.trail_np.hide(overlay_mask)
        self.trail_np.setLightOff(1)
        self._trail_n = -1
        self.ray_np = self.scene.attachNewNode(meshes.box(0.012, 1.0, 0.012, name="depth_ray"))
        self.ray_np.setColor(0.37, 0.90, 1.0, 1)
        self.ray_np.setLightOff(1)
        self.ray_np.hide(overlay_mask)
        self.hit_np = self.scene.attachNewNode(meshes.sphere(0.035, name="ray_hit"))
        self.hit_np.setColor(0.37, 0.90, 1.0, 1)
        self.hit_np.setLightOff(1)
        self.hit_np.hide(overlay_mask)
        wedge = self.robot_np.attachNewNode(meshes.fan(REACH, -REACH_ANGLE, REACH_ANGLE, name="reach"))
        wedge.setPos(0, world.robot_r, 0.004)
        wedge.setColor(1, 1, 1, 0.22)
        wedge.setTransparency(1)
        wedge.setLightOff(1)
        wedge.setDepthOffset(4)
        wedge.setTwoSided(True)
        wedge.hide(overlay_mask)
        self.mount.reparentTo(self.robot_np)
        self.mount.setPos(0, world.robot_r, CAM_H)
        self.mount.setHpr(0, -CAM_TILT, 0)
        from panda3d.core import Point3

    def _pose_hand(self, robot):
        """Place hand, claws, palm and the stretching boom from the robot's hand state."""
        from panda3d.core import Point3
        hx, hy = robot.hand                      # robot frame: +X right, +Y ahead
        hz, grip = robot.hand_z, robot.grip
        if robot.held is not None:
            depth = robot._half_width(robot.held)
        else:
            # behind the claws' span, but never back inside the robot's own body
            depth = min(max(grip - 0.006, 0.05), 0.22, max(0.05, hy - 0.30))
        self.hand_np.setPos(hx, hy, hz)
        for f, side in self.fingers:
            f.setPos(side * (grip + 0.013), 0, 0.02)
        self.palm.setSx((2 * grip + 0.05) / 0.40)
        self.palm.setPos(0, -depth - 0.03, 0.02)
        # boom: a strut from the shoulder to the back of the palm, tilting when raised
        start = Point3(self.shoulder_xy[0], self.shoulder_xy[1], 0.10)
        end = Point3(hx, hy - depth - 0.05, hz + 0.10)
        self.boom.setPos((start + end) / 2 - Point3(0, 0, 0.02))
        self.boom.lookAt(self.robot_np, end - Point3(0, 0, 0.02))
        self.boom.setScale(1, max((end - start).length(), 0.02), 1)

    def sync(self, robot):
        """Copy the simulation state onto the scene graph."""
        self._pose_hand(robot)
        # depth ray (people-only): from the camera point along the heading to the hit
        h = math.radians(robot.heading)
        fx0, fy0 = math.cos(h), -math.sin(h)
        ox, oy = robot.x + fx0 * robot.r, robot.y + fy0 * robot.r
        d = robot.depth()
        length = d if d is not None else 10.0
        self.ray_np.setPos(ox + fx0 * length / 2, oy + fy0 * length / 2, CAM_H + (robot.z if robot.world.elevated else 0.0))
        self.ray_np.setH(panda_h(robot.heading))
        self.ray_np.setSy(max(length, 0.01))
        self.hit_np.setPos(ox + fx0 * length, oy + fy0 * length, CAM_H + (robot.z if robot.world.elevated else 0.0))
        rz = robot.z if robot.world.elevated else 0.0
        self.robot_np.setPos(robot.x, robot.y, rz)
        self.robot_np.setH(panda_h(robot.heading))
        held = robot.held_pose()
        for o in robot.world.objects:
            pivot, shadow = self.obj_nodes[o.id]
            if o.held and held is not None:
                cx, cy, k = held
                pivot.setPos(cx, cy, o.h / 2 + k)
                pivot.setHpr(-(robot.heading + o.yaw_off), 0, 0)
                shadow.setPos(cx, cy, 0.002)
            else:
                pivot.setPos(o.x, o.y, o.z + o.h / 2)
                pivot.setHpr(-o.yaw, 0, 0)
                shadow.setPos(o.x, o.y, o.z + 0.002)
        # chase camera: behind and above the robot, kept inside the walls
        h = math.radians(robot.heading)
        fx, fy = math.cos(h), -math.sin(h)
        lim = robot.world.half - 0.15
        cx = max(-lim, min(lim, robot.x - fx * 1.6))
        cy = max(-lim, min(lim, robot.y - fy * 1.6))
        back = math.hypot(cx - robot.x, cy - robot.y)          # shorter when a wall is behind
        self.chase_mount.setPos(cx, cy, 1.7 + (1.6 - back) * 0.9)
        self.chase_mount.lookAt(robot.x + fx * 2.0, robot.y + fy * 2.0, 0.0)

    # ------------------------------------------------------------- capture
    def set_clip_active(self, on):
        self.bufs["clip"][0].setActive(on)

    # deferred capture: a buffer is switched on for the coming frame, Panda renders it in its
    # normal render pass (no extra renderFrame), and the image is read on the next tick
    def activate(self, name):
        want = self.__dict__.setdefault("_want", {})
        want[name] = want.get(name, 0) + 1
        self.bufs[name][0].setActive(True)

    def release(self, name):
        want = self.__dict__.setdefault("_want", {})
        want[name] = max(0, want.get(name, 0) - 1)
        if not want[name]:
            self.bufs[name][0].setActive(False)

    def read(self, name):
        return self.grab(name, render=False)

    def grab(self, name, render=True):
        buf, tex, w, h = self.bufs[name]
        if render:
            was = buf.isActive()
            buf.setActive(True)
            self.base.graphicsEngine.renderFrame()
            self.base.graphicsEngine.renderFrame()
            buf.setActive(was)
        data = tex.getRamImageAs("RGB")
        arr = np.frombuffer(bytes(data), dtype=np.uint8)
        arr = arr.reshape(tex.getYSize(), tex.getXSize(), 3)[::-1]
        return np.ascontiguousarray(arr[:h, :w])

    def look_jpeg(self):
        return self.view_jpeg("look")

    def window_jpeg(self):
        """The operator window as rendered (all its views), without the desktop."""
        self.base.graphicsEngine.renderFrame()
        tex = self.base.win.getScreenshot()
        data = tex.getRamImageAs("RGB")
        arr = np.frombuffer(bytes(data), dtype=np.uint8).reshape(tex.getYSize(), tex.getXSize(), 3)[::-1]
        buf = io.BytesIO()
        Image.fromarray(np.ascontiguousarray(arr)).save(buf, format="JPEG", quality=90)
        return buf.getvalue()

    def view_jpeg(self, name):
        buf = io.BytesIO()
        Image.fromarray(self.grab(name)).save(buf, format="JPEG", quality=90)
        return buf.getvalue()
