"""Render each mission scene (top-down, chase, robot camera) to outputs/missions/<mission>_<view>.png.

    .venv\\Scripts\\python tools\\mission_shots.py [--seed 0]
"""
import argparse
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    from sim import Engine
    e = Engine(headless=True)
    out = ROOT / "outputs" / "missions"
    out.mkdir(parents=True, exist_ok=True)
    for name in ("brain_puzzle", "colour_sort", "maze"):
        e.op_mission(name, a.seed)
        for view in ("top", "chase", "look"):
            Image.fromarray(e.views.grab(view)).save(out / f"{name}_{view}.png")
        print("saved", name)


if __name__ == "__main__":
    main()
