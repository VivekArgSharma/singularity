"""Camera-only perception for the local planner: colour blobs in the robot's own
640x480 JPEG, a shape guess, and a ground-plane range/bearing estimate from the
published camera geometry. It never sees ground truth: only what /look returns.

Camera (problem statement): 90 deg horizontal / 73.74 deg vertical FOV, at the robot
front (0.2 m ahead of centre), 0.5 m high, pitched 10 deg down.
"""
import io
import math
import os

import numpy as np
from PIL import Image

W, H = 640, 480
CAM_H, TILT, CAM_AHEAD = float(os.environ.get("FETCH_CAM_H", "0.5")), math.radians(10.0), 0.2   # spec 0.5 m
TAN_H, TAN_V = 1.0, 0.75                    # tan(45), tan(36.87)
DS = 2                                      # analyse at 320x240

# hue ranges on PIL's 0-255 hue scale; saturation/value floors keep floor, walls, shadows out
COLOURS = {
    "red": [(0, 8), (243, 256)],
    "orange": [(10, 24)],
    "yellow": [(28, 48)],
    "green": [(62, 110)],
    "blue": [(140, 178)],
    "purple": [(182, 204)],      # mission platform
    "magenta": [(206, 238)],     # maze exit tile
}
FLAT = {"green", "magenta"}      # floor markings (drop square, exit tile), not objects
MISSION_COLOURS = {"purple", "magenta"}   # only looked for in the missions that use them: in the fetch room
MISSION_MODE = False                      # the shaded sides of red and blue objects fall in these hues


def decode(jpeg):
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    small = img.resize((W // DS, H // DS), Image.BILINEAR)
    return np.asarray(small.convert("HSV")).astype(np.int16)


def ground_point(u, v):
    """Full-res pixel -> (right, ahead) metres from the robot centre on the floor, or None."""
    xn = (u - W / 2) / (W / 2) * TAN_H
    yn = (H / 2 - v) / (H / 2) * TAN_V
    up = yn * math.cos(TILT) - math.sin(TILT)
    fwd = math.cos(TILT) + yn * math.sin(TILT)
    if up >= -1e-4:
        return None
    t = CAM_H / -up
    return xn * t, fwd * t + CAM_AHEAD


def bearing(u):
    """Degrees clockwise from the heading (positive = to the right)."""
    return math.degrees(math.atan((u - W / 2) / (W / 2) * TAN_H))


def _label(mask):
    """4-connected components on a small boolean mask -> list of (ys, xs) arrays."""
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    blobs = []
    ys, xs = np.nonzero(mask)
    for y0, x0 in zip(ys, xs):
        if seen[y0, x0]:
            continue
        stack = [(y0, x0)]
        seen[y0, x0] = True
        py, px = [], []
        while stack:
            y, x = stack.pop()
            py.append(y)
            px.append(x)
            for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        blobs.append((np.array(py), np.array(px)))
    return blobs


def _shape(ys, xs, val):
    """cube / sphere / cone / cylinder from silhouette and shading."""
    y0, y1, x0, x1 = ys.min(), ys.max(), xs.min(), xs.max()
    bh, bw = y1 - y0 + 1, x1 - x0 + 1
    rows = np.bincount(ys - y0, minlength=bh)
    top = rows[: max(1, bh // 4)].mean()
    bot = rows[-max(1, bh // 4):].mean()
    m = np.zeros((bh, bw), bool)
    m[ys - y0, xs - x0] = True
    v = np.zeros((bh, bw), np.int16)
    v[ys - y0, xs - x0] = val
    both = m[:, 2:] & m[:, :-2]
    dv = np.abs(v[:, 2:] - v[:, :-2])[both]
    bothv = m[2:, :] & m[:-2, :]
    dvv = np.abs(v[2:, :] - v[:-2, :])[bothv]
    flat = float(((dv <= 2).mean() + (dvv <= 2).mean()) / 2) if dv.size and dvv.size else 0.0
    feats = {"aspect": round(bh / bw, 2), "taper": round(top / max(bot, 1), 2), "flat": round(flat, 2),
             "fill": round(len(ys) / (bh * bw), 2)}
    if feats["taper"] < 0.33 and feats["aspect"] > 0.7:
        shape = "cone"
    elif feats["aspect"] > 1.6:
        shape = "cylinder"
    elif flat > 0.6 or feats["fill"] >= 0.85:
        shape = "cube"                 # flat faces, or a silhouette that fills its box (a disc fills ~0.79)
    else:
        shape = "sphere"
    return shape, feats


def detect(jpeg, holding=False):
    """All coloured things in view. The carried object (bottom-right corner) is ignored."""
    hsv = decode(jpeg)
    hue, sat, val = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    found = []
    for name, ranges in COLOURS.items():
        if name in MISSION_COLOURS and not MISSION_MODE:
            continue
        hm = np.zeros(hue.shape, bool)
        for a, b in ranges:
            hm |= (hue >= a) & (hue < b)
        mask = hm & (sat > (70 if name == "green" else 110)) & (val > 45)
        if holding:
            mask[int(0.62 * mask.shape[0]):, int(0.72 * mask.shape[1]):] = False
        for ys, xs in _label(mask):
            if len(ys) < (40 if name == "green" else 12):
                continue
            u = float(xs.mean() * DS + DS / 2)
            vb = float(ys.max() * DS + DS)
            bw = (xs.max() - xs.min() + 1) * DS
            det = {"color": name, "u": round(u, 1), "v_bottom": round(vb, 1),
                   "bbox": [int(xs.min() * DS), int(ys.min() * DS), int(xs.max() * DS + DS), int(ys.max() * DS + DS)],
                   "area": int(len(ys) * DS * DS), "bearing": round(bearing(u), 1),
                   "cut_bottom": bool(ys.max() >= mask.shape[0] - 2),
                   "edge": bool(xs.min() <= 1 or xs.max() >= mask.shape[1] - 2 or ys.max() >= mask.shape[0] - 2)}
            # where the blob's pixels land on the floor (right for floor markings; for a solid thing
            # it is roughly its footprint seen from here). Missions use it with the range scan.
            step = max(1, len(ys) // 60)
            fp = [ground_point(x * DS + 1, y * DS + 1) for y, x in zip(ys[::step], xs[::step])]
            fp = [q for q in fp if q]
            if fp:
                det["floor_rel"] = [round(float(np.mean([q[0] for q in fp])), 3), round(float(np.mean([q[1] for q in fp])), 3)]
            if True:
                det["shape"], det["feats"] = _shape(ys, xs, val[ys, xs])
                if det["edge"]:
                    det["shape"] = None          # cut by the frame: centre it before trusting the shape
                gp = None if det["cut_bottom"] else ground_point(u, vb)
                if gp:
                    near = math.hypot(*gp)
                    cam_d = max(0.2, gp[1] - CAM_AHEAD)
                    width = bw / (W / 2) * TAN_H * cam_d
                    rad = max(0.08, min(0.3, width / 2))
                    k = (near + rad * 0.8) / near        # near-surface point -> centre estimate
                    det["centre_rel"] = [round(gp[0] * k, 3), round(gp[1] * k, 3)]
                    det["radius"] = round(rad, 3)
                    if ys.min() > 1:                     # top in view: height from the top pixel's ray
                        vt = float(ys.min() * DS)
                        yn = (H / 2 - vt) / (H / 2) * TAN_V
                        up = yn * math.cos(TILT) - math.sin(TILT)
                        fw = math.cos(TILT) + yn * math.sin(TILT)
                        d_top = cam_d + {"sphere": rad, "cone": rad, "cube": 2 * rad}.get(det["shape"], 0.0)   # top pixel = far top edge for a cube
                        det["height"] = round(max(0.0, CAM_H + d_top * up / fw), 3)
            if name in FLAT:
                # a floor marking by default (the drop square, the exit tile); the object reading is kept
                # so a caller with a range scan can tell a green box from the green square
                det["obj"] = {k: det.pop(k) for k in ("shape", "centre_rel", "radius", "height", "feats") if k in det}
                det["shape"] = "zone" if name == "green" else "tile"
                if fp:
                    det["centre_rel"] = det["floor_rel"]
            found.append(det)
    return found


def to_world(rel, x, y, heading):
    """(right, ahead) robot-frame metres -> world (x, y). Heading clockwise, 0 = +x."""
    h = math.radians(heading)
    fx, fy = math.cos(h), -math.sin(h)
    rx, ry = math.cos(h + math.pi / 2), -math.sin(h + math.pi / 2)
    return x + fx * rel[1] + rx * rel[0], y + fy * rel[1] + ry * rel[0]
