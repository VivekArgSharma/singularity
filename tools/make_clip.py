"""Turn a recorded run's JPEG frames into a captioned MP4 clip."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import imageio.v2 as imageio
import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFont


FPS = 30
SIZE = (1440, 1080)
MIN_FRAMES = 10 * FPS
MAX_FRAMES = 20 * FPS
FONT_PATH = Path(r"C:\Windows\Fonts\arial.ttf")
ROOT = Path(__file__).resolve().parents[1]


def command_font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(str(FONT_PATH), 44)
    except OSError:
        return ImageFont.load_default()


def wrap_command(draw: ImageDraw.ImageDraw, command: str, font: ImageFont.ImageFont) -> str:
    """Wrap at the available pixel width, including unusually long words."""
    max_width = SIZE[0] - 100
    lines: list[str] = []
    for paragraph in command.splitlines() or [""]:
        current = ""
        for word in paragraph.split():
            candidate = f"{current} {word}" if current else word
            if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current)
                current = ""
            for char in word:
                candidate = current + char
                if current and draw.textbbox((0, 0), candidate, font=font)[2] > max_width:
                    lines.append(current)
                    current = char
                else:
                    current = candidate
        lines.append(current)
    return "\n".join(lines)


def caption(frame: Image.Image, command: str, font: ImageFont.ImageFont) -> np.ndarray:
    overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    wrapped = wrap_command(draw, command, font)
    spacing = 8
    bounds = draw.multiline_textbbox((0, 0), wrapped, font=font, spacing=spacing)
    top = 35
    padding = 20
    band_height = bounds[3] - bounds[1] + 2 * padding
    draw.rectangle((0, top, frame.width, top + band_height), fill=(0, 0, 0, 165))
    draw.multiline_text(
        (50, top + padding - bounds[1]),
        wrapped,
        font=font,
        fill=(255, 255, 255, 255),
        spacing=spacing,
    )
    return np.asarray(Image.alpha_composite(frame.convert("RGBA"), overlay).convert("RGB"))


def load_frame(path: Path) -> Image.Image:
    with Image.open(path) as source:
        if source.size != SIZE:
            raise ValueError(f"{path}: expected {SIZE[0]}x{SIZE[1]}, got {source.width}x{source.height}")
        return source.convert("RGB")


def make_clip(run_id: str, root: Path | None = None, speedup: float = 1.0) -> int:
    """speedup > 1 keeps every n-th frame (a labelled time-lapse) so a long task still fits 20 s."""
    if not run_id or Path(run_id).name != run_id or run_id in (".", ".."):
        raise ValueError("--run must be a single run ID, without path separators")
    run_dir = (Path(root) if root else ROOT / "clips") / run_id
    metadata = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    paths = sorted((run_dir / "frames").glob("*.jpg"))
    if not paths:
        raise ValueError(f"No JPEG frames found in {run_dir / 'frames'}")
    if not isinstance(metadata.get("command"), str):
        raise ValueError("meta.json: command must be text")
    if metadata.get("fps") != FPS or isinstance(metadata.get("fps"), bool):
        raise ValueError(f"meta.json: fps must be {FPS}")
    if metadata.get("frames") != len(paths) or isinstance(metadata.get("frames"), bool):
        raise ValueError(f"meta.json: frames must match the {len(paths)} JPEG files")

    if speedup > 1:
        paths = [paths[min(len(paths) - 1, round(i * speedup))] for i in range(int(len(paths) / speedup))]
        metadata["command"] = f"{metadata['command']}  [time-lapse x{speedup:.1f}]"
    deficit = max(0, MIN_FRAMES - len(paths))
    first_hold = min(FPS, deficit)
    last_hold = min(round(1.5 * FPS), deficit - first_hold)
    last_extension = deficit - first_hold - last_hold
    timeline = [paths[0]] * first_hold + paths + [paths[-1]] * (last_hold + last_extension)
    name = run_id if speedup <= 1 else f"{run_id}_clip20s"
    output = run_dir / f"{name}.mp4"
    temporary = run_dir / f".{name}.partial.mp4"
    font = command_font()

    try:
        with imageio.get_writer(
            temporary,
            format="FFMPEG",
            fps=FPS,
            codec="libx264",
            pixelformat="yuv420p",
            macro_block_size=1,
            ffmpeg_log_level="error",
            output_params=["-an"],
        ) as writer:
            for index, path in enumerate(timeline):
                frame = load_frame(path)
                pixels = caption(frame, metadata["command"], font) if index < 2 * FPS else np.asarray(frame)
                writer.append_data(pixels)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)

    duration = len(timeline) / FPS
    print(f"{output}: {duration:.3f} s, {SIZE[0]}x{SIZE[1]}, {len(timeline)} frames")
    if len(timeline) > MAX_FRAMES:
        print("WARNING: clip exceeds 20 seconds; file was written.", file=sys.stderr)
        return 2
    return 0


def check_clip(path: Path) -> int:
    try:
        with imageio.get_reader(path, format="FFMPEG") as reader:
            info = reader.get_meta_data()
        frames, _ = imageio_ffmpeg.count_frames_and_secs(path)
        fps = info["fps"]
        width, height = info["size"]
        if not fps or not frames:
            raise ValueError("video has no readable frames or frame rate")
        duration = info.get("duration") or frames / fps
        passed = 10 <= duration <= 20 and height >= 720
        verdict = "PASS" if passed else "FAIL"
        print(f"{verdict}: {path}: {duration:.3f} s, {width}x{height}, {frames} frames")
        return 0 if passed else 1
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(f"FAIL: {path}: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--run", metavar="RUN_ID", help="create clips/RUN_ID/RUN_ID.mp4")
    choice.add_argument("--check", metavar="MP4", type=Path, help="verify duration and height")
    parser.add_argument("--root", type=Path, help="runs folder (default clips/; dashboard uses outputs/runs)")
    args = parser.parse_args()
    try:
        return make_clip(args.run, args.root) if args.run is not None else check_clip(args.check)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
