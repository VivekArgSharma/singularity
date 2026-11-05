"""Compose one MP4 from a --debug-frames recording: chase (large), robot camera and
top-down side by side, with the current step as a caption.

    .venv\\Scripts\\python tools\\make_multiview.py --run choreo
"""
import argparse
import json
from pathlib import Path

import imageio.v2 as imageio
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
W, H, FPS = 1920, 1080, 30


def font(size):
    try:
        return ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", size)
    except OSError:
        return ImageFont.load_default()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    a = ap.parse_args()
    run = ROOT / "clips" / a.run
    pov = sorted((run / "frames").glob("*.jpg"))
    caps = json.loads((run / "captions.json").read_text()) if (run / "captions.json").exists() else []
    big, small = font(30), font(20)
    out = run / f"{a.run}_multiview.mp4"
    with imageio.get_writer(out, format="FFMPEG", fps=FPS, codec="libx264", pixelformat="yuv420p",
                            macro_block_size=1, ffmpeg_log_level="error", output_params=["-an"]) as wr:
        for p in pov:
            n = int(p.stem)
            canvas = Image.new("RGB", (W, H), (19, 18, 23))
            chase = run / "chase" / p.name
            top = run / "top" / p.name
            if chase.exists():
                canvas.paste(Image.open(chase).resize((1280, 960)), (0, 60))
            canvas.paste(Image.open(p).resize((640, 480)), (1280, 60))
            if top.exists():
                canvas.paste(Image.open(top).resize((640, 480)), (1280, 540))
            d = ImageDraw.Draw(canvas)
            text = next((c["text"] for c in reversed(caps) if c["frame"] <= n), "")
            d.text((24, 14), text, font=big, fill=(255, 255, 255))
            d.text((24, 1030), "Chase view - people only", font=small, fill=(180, 180, 186))
            d.text((1296, 1030), "Top-down - people only", font=small, fill=(180, 180, 186))
            d.text((1296, 18), "Robot camera - what Claude sees", font=small, fill=(180, 180, 186))
            d.text((W - 150, 1030), f"{n / FPS:6.1f} s", font=small, fill=(180, 180, 186))
            wr.append_data(np.asarray(canvas))
    print(f"{out}: {len(pov) / FPS:.1f} s, {len(pov)} frames")


if __name__ == "__main__":
    main()
