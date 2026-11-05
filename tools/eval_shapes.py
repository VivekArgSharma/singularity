"""Shape-classifier confusion table from the robot camera (ground truth used only to score).

    .venv\\Scripts\\python tools\\eval_shapes.py [--level level1] [--seeds 0,1,2]
"""
import argparse
import io
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "engine"), str(ROOT / "agent")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", default="level1")
    ap.add_argument("--seeds", default="0,1,2")
    a = ap.parse_args()
    from PIL import Image
    from sim import Engine
    import perception as P
    from world import forward
    e = Engine(headless=True)
    conf = defaultdict(Counter)
    rows = []
    for seed in [int(s) for s in a.seeds.split(",")]:
        e.op_reset(a.level, seed)
        r, w = e.robot, e.robot.world
        for o in w.objects:
            for ang in range(0, 360, 30):
                for d in (0.4, 0.7, 1.0, 1.5, 2.2, 3.0):
                    t = math.radians(ang)
                    dist = d + o.r + r.r
                    x, y = o.x + math.cos(t) * dist, o.y + math.sin(t) * dist
                    if abs(x) > w.half - 0.3 or abs(y) > w.half - 0.3 or w.circle_blocked(x, y, r.r + 0.05):
                        continue
                    for b in (0.0, 20.0):
                        h = math.degrees(math.atan2(-(o.y - y), o.x - x)) - b
                        r.x, r.y, r.heading = x, y, h
                        e.views.sync(r)
                        buf = io.BytesIO()
                        Image.fromarray(e.views.grab("look")).save(buf, format="JPEG", quality=90)
                        dets = P.detect(buf.getvalue())
                        fx, fy = forward(h)
                        cam = (x + fx * r.r, y + fy * r.r)
                        tb = (math.degrees(math.atan2(-(o.y - cam[1]), o.x - cam[0])) - h + 180) % 360 - 180
                        hit = [dd for dd in dets if dd["color"] == o.color_name and abs(dd["bearing"] - tb) < 12]
                        if not hit:
                            continue
                        dd = max(hit, key=lambda q: q["area"])
                        got = dd["shape"] if dd["shape"] not in ("zone",) else "zone"
                        conf[o.shape][str(got)] += 1
                        rows.append({"obj": f"{o.color_name} {o.shape}", "d": d, "ang": ang, "b": b, "got": got,
                                     "height": dd.get("height"), "feats": dd.get("feats"), "edge": dd["edge"],
                                     "cut_bottom": dd["cut_bottom"], "bbox": dd["bbox"]})
    print("true shape -> classified as (counts)")
    for k, c in conf.items():
        tot = sum(c.values())
        print(f"  {k:9} {tot:4d}  " + "  ".join(f"{s}:{n}" for s, n in c.most_common()) + f"   right {c[k] / tot:.0%}")
    out = ROOT / "outputs" / "eval" / "shapes.json"
    out.write_text(json.dumps(rows))
    print("rows ->", out)


if __name__ == "__main__":
    main()
