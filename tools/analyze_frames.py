"""Can a recorded run's frames be turned into a coverage map? (operator analysis tool)

    .venv\\Scripts\\python tools\\analyze_frames.py --run demo --replay demo_replay

1. Inventory: frame count, resolution, timestamps, meta.json, pose log.
2. Redundancy between consecutive frames (pixel difference) and identical poses.
3. Pose association: old runs have no pose log. The scripted demo is deterministic,
   so a replay with the current engine (which logs a pose per frame) is compared
   pixel by pixel; if they match, the replay's poses belong to the old frames.
4. Coverage from the frames alone: for every image column, walk up from the bottom
   while the pixels look like floor; the last floor pixel, projected with the camera
   geometry, is where that column's view of the floor ends (occlusion from RGB only).
   Scored against the real visible floor at the same poses (ground truth, operator only).
Writes outputs/frame_analysis/<run>/report.json, report.md and coverage.png.
"""
import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
from world import World, forward  # noqa: E402

W, H = 360, 270                     # analysis size (frames are 1440x1080, same 4:3 camera)
TAN_H, TAN_V = 1.0, 0.75
CAM_H, TILT, AHEAD = 0.5, math.radians(10.0), 0.2
CELL, MAX_RANGE = 0.05, 4.0
CAM_NEAR = CAM_H / math.tan(math.radians(10.0 + 36.87))


def small(path):
    return np.asarray(Image.open(path).convert("RGB").resize((W, H), Image.BILINEAR))


def floor_mask(rgb):
    hsv = np.asarray(Image.fromarray(rgb).convert("HSV")).astype(int)
    grey_floor = (hsv[..., 1] < 45) & (hsv[..., 2] > 125) & (hsv[..., 2] < 235)
    green_square = (hsv[..., 0] >= 62) & (hsv[..., 0] < 110) & (hsv[..., 1] > 70) & (hsv[..., 2] > 45)   # floor too
    return grey_floor | green_square


def row_distance(v):
    """Image row (analysis size) -> floor distance ahead of the camera, along the column's ray."""
    yn = (H / 2 - (v + 0.5)) / (H / 2) * TAN_V
    up = yn * math.cos(TILT) - math.sin(TILT)
    fwd = math.cos(TILT) + yn * math.sin(TILT)
    return None if up >= 0 else CAM_H / -up * fwd


def rgb_view(rgb, x, y, heading):
    """Floor points (world) the frame shows, per column, stopping at the first non-floor pixel."""
    fl = floor_mask(rgb)
    pts = []
    for u in range(0, W, 2):
        xn = (u + 0.5 - W / 2) / (W / 2) * TAN_H
        col = fl[:, u]
        far = None
        miss = 0
        for v in range(H - 1, -1, -1):          # bottom (near) to top (far)
            if col[v]:
                far, miss = v, 0
            else:
                miss += 1
                if miss > 2:                    # thin grout lines are not obstacles
                    break
        if far is None:
            continue
        dmax = row_distance(far)
        if dmax is None:
            continue
        b = math.degrees(math.atan(xn))
        dist = min(dmax * math.hypot(1, xn), MAX_RANGE)
        pts.append((b, dist))
    fx, fy = forward(heading)
    ox, oy = x + fx * AHEAD, y + fy * AHEAD
    out = []
    for b, dist in pts:
        dx, dy = forward(heading + b)
        ds = np.arange(CAM_NEAR, dist, CELL / 2)
        out.append((ox + dx * ds, oy + dy * ds))
    return out


def gt_view(world, x, y, heading):
    fx, fy = forward(heading)
    ox, oy = x + fx * AHEAD, y + fy * AHEAD
    out = []
    for k in range(181):
        b = -45 + 90 * k / 180
        d = world.ray(ox, oy, heading + b)
        d = 10.0 if d is None else d
        dx, dy = forward(heading + b)
        ds = np.arange(CAM_NEAR, min(d, MAX_RANGE), CELL / 2)
        out.append((ox + dx * ds, oy + dy * ds))
    return out


def cells(pts, half, n):
    if not pts:
        return np.zeros(0, int)
    xs = np.concatenate([p[0] for p in pts])
    ys = np.concatenate([p[1] for p in pts])
    i, j = ((xs + half) / CELL).astype(int), ((ys + half) / CELL).astype(int)
    ok = (i >= 0) & (i < n) & (j >= 0) & (j < n)
    return np.unique(j[ok] * n + i[ok])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="demo")
    ap.add_argument("--replay", default=None, help="a replay of the same scripted run, recorded with pose logging")
    ap.add_argument("--level", default="level1")
    a = ap.parse_args()
    run = ROOT / "clips" / a.run
    frames = sorted((run / "frames").glob("*.jpg"))
    meta = json.loads((run / "meta.json").read_text()) if (run / "meta.json").exists() else {}
    first = Image.open(frames[0])
    rep = {"run": a.run, "frames": len(frames), "resolution": list(first.size), "meta": meta,
           "first_file_time": datetime.fromtimestamp(frames[0].stat().st_mtime).isoformat(timespec="seconds"),
           "last_file_time": datetime.fromtimestamp(frames[-1].stat().st_mtime).isoformat(timespec="seconds"),
           "pose_log": (run / "frames.jsonl").exists(),
           "logs": sorted(p.name for p in run.glob("*.jsonl")),
           "camera": "robot camera: clip buffer mounted on the robot (views.py), same 90 x 73.74 deg lens "
                     "as the 640x480 /look image, 1440x1080; not an external camera"}

    imgs = [small(f) for f in frames]
    grey = [im.mean(axis=2) for im in imgs]
    d = np.array([np.abs(p - q).mean() for p, q in zip(grey, grey[1:])])
    rep["consecutive_diff"] = {"median": round(float(np.median(d)), 2), "p90": round(float(np.percentile(d, 90)), 2),
                               "near_duplicates_lt1": int((d < 1).sum()), "lt3": int((d < 3).sum()), "pairs": len(d)}

    poses = None
    if (run / "frames.jsonl").exists():
        poses = [json.loads(l) for l in (run / "frames.jsonl").read_text().splitlines()]
    elif a.replay:
        rp = ROOT / "clips" / a.replay
        rf = sorted((rp / "frames").glob("*.jpg"))
        diffs = [float(np.abs(small(x).astype(float) - small(y).astype(float)).mean()) for x, y in zip(frames, rf)]
        match = len(rf) == len(frames) and max(diffs) < 1.0
        rep["replay_match"] = {"replay": a.replay, "frames": len(rf), "max_diff": round(max(diffs), 3),
                               "median_diff": round(float(np.median(diffs)), 3), "match": match}
        if match:
            poses = [json.loads(l) for l in (rp / "frames.jsonl").read_text().splitlines()]
            with open(run / "frames.jsonl", "w", encoding="utf-8") as f:
                for p in poses:
                    f.write(json.dumps({**p, "source": f"deterministic replay '{a.replay}', pixel-matched"}) + "\n")
    if poses is None:
        rep["coverage"] = "no poses: frames cannot be placed in the world"
        print(json.dumps(rep, indent=2))
        return
    same = sum(1 for p, q in zip(poses, poses[1:]) if abs(p["x"] - q["x"]) < 1e-4 and abs(p["y"] - q["y"]) < 1e-4
               and abs(p["heading"] - q["heading"]) < 1e-3)
    rep["identical_consecutive_poses"] = same

    world = World(a.level, 0)
    n = int(round(2 * world.half / CELL))
    xs = -world.half + (np.arange(n) + 0.5) * CELL
    X, Y = np.meshgrid(xs, xs)
    floor = np.ones_like(X, bool)
    for o in world.objects:
        floor &= ~np.vectorize(lambda p, q, o=o: o.dist_to_point(p, q) <= 0.0)(X, Y)
    seen_rgb = np.zeros(n * n, bool)
    seen_gt = np.zeros(n * n, bool)
    per = []
    for im, p in zip(imgs, poses):
        c_rgb = cells(rgb_view(im, p["x"], p["y"], p["heading"]), world.half, n)
        c_gt = cells(gt_view(world, p["x"], p["y"], p["heading"]), world.half, n)
        c_gt = c_gt[floor.ravel()[c_gt]]
        new = int((~seen_gt[c_gt]).sum())
        per.append({"frame": p["frame"], "new_m2": round(new * CELL ** 2, 3), "view_m2": round(len(c_gt) * CELL ** 2, 3)})
        seen_rgb[c_rgb] = True
        seen_gt[c_gt] = True
    fl = floor.ravel()
    tp = (seen_rgb & seen_gt).sum()
    views = sum(q["view_m2"] for q in per)
    useful = sum(1 for q in per if q["new_m2"] >= 0.05)
    rep["coverage"] = {
        "true_visible_floor_pct": round(float(seen_gt[fl].mean() * 100), 1),
        "rgb_only_floor_pct": round(float((seen_rgb & fl).sum() / fl.sum() * 100), 1),
        "rgb_precision": round(float(tp / max(1, seen_rgb.sum())), 3),
        "rgb_recall": round(float(tp / max(1, seen_gt.sum())), 3),
        "redundant_pct": round(100 * (1 - sum(q["new_m2"] for q in per) / max(views, 1e-9)), 1),
        "frames_adding_new_floor": useful, "frames": len(per)}
    out = ROOT / "outputs" / "frame_analysis" / a.run
    out.mkdir(parents=True, exist_ok=True)
    img = np.zeros((n, n, 3), np.uint8)
    img[fl.reshape(n, n)] = (40, 42, 48)
    img[seen_gt.reshape(n, n)] = (60, 110, 85)
    img[(seen_rgb & ~seen_gt).reshape(n, n)] = (220, 80, 70)
    img[(seen_rgb & seen_gt).reshape(n, n)] = (120, 200, 150)
    img[~fl.reshape(n, n)] = (235, 235, 240)
    for p in poses[::10]:
        i, j = int((p["x"] + world.half) / CELL), int((p["y"] + world.half) / CELL)
        img[max(0, j - 1):j + 2, max(0, i - 1):i + 2] = (242, 177, 52)
    Image.fromarray(img[::-1]).resize((480, 480), Image.NEAREST).save(out / "coverage.png")
    (out / "report.json").write_text(json.dumps({**rep, "per_frame": per}, indent=2))
    print(json.dumps(rep, indent=2))


if __name__ == "__main__":
    main()
