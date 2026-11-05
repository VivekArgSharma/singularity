"""Camera calibration: checks the /look image against the fixed camera spec.

Uses the test-only level `calib` (robot at the origin facing +x, spheres at
known bearings). Expected pixel positions come from a pinhole projection of
the spec camera: 640x480, 90 deg horizontal FOV, robot front, 0.5 m, 10 deg down.

Run:  .venv\\Scripts\\python tests\\test_calibration.py
"""
import io
import json
import math
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
from proc_util import kill_tree  # noqa: E402

PORT, ADMIN = 8094, 8095
LEVEL = json.loads((ROOT / "engine" / "levels" / "calib.json").read_text())
W, H, HFOV, CAM_H, TILT = 640, 480, 90.0, 0.5, 10.0
VHALF = math.atan(math.tan(math.radians(HFOV / 2)) * H / W)
TOL = 6.0
fails = []


def call(port, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=60) as resp:
        raw = resp.read()
        return raw if path == "/look" else json.loads(raw)


def project(px, py, pz, rx, ry, heading, robot_r):
    """World point -> expected (col, row) for the spec camera."""
    h = math.radians(heading)
    f2 = (math.cos(h), -math.sin(h))
    right2 = (math.cos(h + math.pi / 2), -math.sin(h + math.pi / 2))  # clockwise +90
    cx, cy, cz = rx + f2[0] * robot_r, ry + f2[1] * robot_r, CAM_H
    v = (px - cx, py - cy, pz - cz)
    t = math.radians(TILT)
    fwd = (f2[0] * math.cos(t), f2[1] * math.cos(t), -math.sin(t))
    up = (f2[0] * math.sin(t), f2[1] * math.sin(t), math.cos(t))
    rgt = (right2[0], right2[1], 0.0)
    dot = lambda a, b: sum(i * j for i, j in zip(a, b))  # noqa: E731
    zf, zr, zu = dot(v, fwd), dot(v, rgt), dot(v, up)
    col = W / 2 + (W / 2) * (zr / zf) / math.tan(math.radians(HFOV / 2))
    row = H / 2 - (H / 2) * (zu / zf) / math.tan(VHALF)
    return col, row


def centroid(img, rgb):
    a = np.asarray(img).astype(int)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    if rgb == "red":
        m = (r > 120) & (g < 70) & (b < 70)
    elif rgb == "blue":
        m = (b > 120) & (r < 70) & (g < 90)
    else:
        m = (r > 120) & (g > 120) & (b < 80)
    ys, xs = np.nonzero(m)
    if len(xs) < 20:
        return None
    return float(xs.mean()), float(ys.mean())


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({info})" if info else ""))
    if not cond:
        fails.append(name)


def main():
    proc = subprocess.Popen([sys.executable, str(ROOT / "engine" / "sim.py"), "--headless",
                             "--level", "calib", "--port", str(PORT), "--admin-port", str(ADMIN)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            try:
                call(PORT, "GET", "/state")
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        call(ADMIN, "POST", "/admin/reset", {"level": "calib", "seed": 0})
        call(ADMIN, "POST", "/admin/command", {"text": "calibration", "run_id": "calib"})
        rr = LEVEL["robot"]["radius"]
        img = Image.open(io.BytesIO(call(PORT, "GET", "/look"))).convert("RGB")
        check("frame size 640x480", img.size == (W, H), img.size)

        for o, label in zip(LEVEL["objects"], ("red", "blue", "yellow")):
            ox, oy = o["pos"]
            exp = project(ox, oy, o["size"] / 2, 0.0, 0.0, 0.0, rr)
            got = centroid(img, label)
            ok = got is not None and abs(got[0] - exp[0]) <= TOL and abs(got[1] - exp[1]) <= TOL
            check(f"{label} sphere position", ok,
                  f"expected ({exp[0]:.0f},{exp[1]:.0f}) got {None if got is None else tuple(round(v) for v in got)}")

        red, blue = centroid(img, "red"), centroid(img, "blue")
        check("+y appears on the left (not mirrored)", blue and red and blue[0] < W / 2 < red[0])

        call(PORT, "POST", "/turn", {"degrees": 30})
        img2 = Image.open(io.BytesIO(call(PORT, "GET", "/look"))).convert("RGB")
        red2 = centroid(img2, "red")
        check("turn +30 (right) centres the +30 deg target", red2 and abs(red2[0] - W / 2) <= TOL,
              f"red at col {None if red2 is None else round(red2[0])}")
        d = call(PORT, "GET", "/depth")["depth"]
        exp_d = math.hypot(1.732 - 0.2 * math.cos(math.radians(30)),
                           -1.0 + 0.2 * math.sin(math.radians(30))) - 0.15
        check("depth to centred target", d is not None and abs(d - exp_d) < 0.03, f"{d} vs {exp_d:.3f}")

        # 45 deg to the right of the heading must land on the right edge of the frame
        call(PORT, "POST", "/turn", {"degrees": -45})               # heading 30 -> -15: red target now at +45
        img3 = Image.open(io.BytesIO(call(PORT, "GET", "/look"))).convert("RGB")
        st = call(PORT, "GET", "/state")
        ox, oy = LEVEL["objects"][0]["pos"]
        rr_ = LEVEL["objects"][0]["size"] / 2
        h = math.radians(st["heading"])
        camx, camy = st["x"] + math.cos(h) * rr, st["y"] - math.sin(h) * rr
        bearing = math.degrees(math.atan2(-(oy - camy), ox - camx)) - st["heading"]
        centre_col = project(ox, oy, rr_, st["x"], st["y"], st["heading"], rr)[0]
        dx, dy = ox - camx, oy - camy
        n = math.hypot(dx, dy)
        lx, ly = ox + (-dy / n) * rr_, oy + (dx / n) * rr_          # silhouette's left edge (+y side)
        left_col = project(lx, ly, rr_, st["x"], st["y"], st["heading"], rr)[0]
        a3 = np.asarray(img3).astype(int)
        m = (a3[..., 0] > 120) & (a3[..., 1] < 70) & (a3[..., 2] < 70)
        xs = np.nonzero(m)[1]
        # a ray 45 deg right / left of the optical axis (in the camera's tilted plane), 2 m out
        t = math.radians(TILT)
        f3 = (math.cos(h) * math.cos(t), -math.sin(h) * math.cos(t), -math.sin(t))
        r3 = (math.cos(h + math.pi / 2), -math.sin(h + math.pi / 2), 0.0)
        cols = []
        for side in (1, -1):
            d3 = [2 * (f3[i] * math.cos(math.radians(45)) + side * r3[i] * math.sin(math.radians(45))) for i in range(3)]
            cols.append(project(camx + d3[0], camy + d3[1], CAM_H + d3[2], st["x"], st["y"], st["heading"], rr)[0])
        check("45 deg right of the axis = right edge, 45 deg left = left edge (90 deg lens)",
              abs(cols[0] - W) < 0.5 and abs(cols[1]) < 0.5, [round(c, 2) for c in cols])
        check("target at %.1f deg from the camera projects past the edge (centre col %.0f)" % (bearing, centre_col),
              centre_col > W)
        check("rendered target is cut by the right edge where predicted",
              len(xs) > 20 and xs.max() >= W - 3 and abs(xs.min() - left_col) <= TOL,
              f"blob {xs.min() if len(xs) else None}..{xs.max() if len(xs) else None}, expected left edge {left_col:.0f}")
    finally:
        kill_tree(proc)
        proc.communicate(timeout=10)
    print(f"\n{len(fails)} failed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
