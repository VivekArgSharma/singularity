"""Clip recording: 1440x1080 frames from the robot camera, saved only while actions run.

Each recording is one folder: frames/NNNNNN.jpg, frames.jsonl (the robot pose for
every frame, so any frame can be placed in the world), meta.json. tools/make_clip.py
turns it into the MP4. Frames are written by a background thread so the main loop
never waits on the disk.
"""
import json
import os
import queue
import threading
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
CLIP_FPS = 30
LOWMEM = os.environ.get("FETCH_LOWMEM", "") in ("1", "true", "yes")   # small servers: 960x720 (still 720p), bounded queue
CLIP_W, CLIP_H = (960, 720) if LOWMEM else (1440, 1080)


class Recorder:
    def __init__(self, engine, debug_frames=False):
        self.eng = engine
        self.debug_frames = debug_frames         # also save chase and top-down frames
        self.rec = None
        self.last_step = None
        self.pending = None                       # clip frame waiting for this frame's render pass
        self.q = queue.Queue(maxsize=6 if LOWMEM else 0)   # bounded: the main loop waits rather than piling up frames
        threading.Thread(target=self._writer, daemon=True).start()

    def _writer(self):
        while True:
            arr, path = self.q.get()
            try:
                Image.fromarray(arr).save(path, quality=92)
            finally:
                self.q.task_done()

    def start(self, run_id, command, root=None):
        d = (Path(root) if root else ROOT / "clips") / run_id
        (d / "frames").mkdir(parents=True, exist_ok=True)
        for old in (d / "frames").glob("*.jpg"):
            old.unlink()
        self.rec = {"dir": d, "n": 0, "command": command, "run_id": run_id, "poses": []}
        self.frame()

    def collect(self):
        """Start of a tick: the clip frame asked for last tick was rendered in the normal pass."""
        if self.pending is not None:
            path = self.pending["path"]
            self.pending = None
            self.q.put((self.eng.views.read("clip"), path))
            self.eng.views.release("clip")

    def flush(self):
        """Render the pending clip frame now (only when the scene is about to change before the next
        render pass: two clip frames due in one tick, or the recording stops this tick)."""
        if self.pending is not None:
            self.eng.base.graphicsEngine.renderFrame()
            self.collect()

    def frame(self):
        if self.rec is None:
            return
        e, views, r = self.eng, self.eng.views, self.eng.robot
        if self.pending is not None:
            self.flush()
        self.last_step = e.sim_steps
        rec = self.rec
        rec["n"] += 1
        rec["poses"].append({"frame": rec["n"], "sim_step": e.sim_steps,
                             "sim_seconds": round(r.stats["sim_seconds"], 4), "x": round(r.x, 4),
                             "y": round(r.y, 4), "heading": round(r.heading, 3),
                             "holding": r.held is not None, "task": e.task_id})
        views.activate("clip")
        self.pending = {"path": str(rec["dir"] / "frames" / f"{rec['n']:06d}.jpg"), "step": e.sim_steps}
        if self.debug_frames:                     # people-only views for frame-by-frame checks
            for view in ("chase", "top"):
                d = rec["dir"] / view
                d.mkdir(exist_ok=True)
                self.q.put((views.grab(view), str(d / f"{rec['n']:06d}.jpg")))

    def stop(self):
        if self.rec is None:
            return None
        self.flush()
        self.q.join()
        rec = self.rec
        meta = {"run_id": rec["run_id"], "command": rec["command"], "fps": CLIP_FPS,
                "frames": rec["n"], "width": CLIP_W, "height": CLIP_H,
                "camera": {"source": "robot camera (clip buffer on the robot mount)", "hfov": 90.0,
                           "vfov": 73.74, "height": 0.5, "tilt_down": 10.0, "ahead_of_centre": self.eng.robot.r}}
        (rec["dir"] / "meta.json").write_text(json.dumps(meta, indent=2))
        with open(rec["dir"] / "frames.jsonl", "w", encoding="utf-8") as f:
            for p in rec["poses"]:
                f.write(json.dumps(p) + "\n")
        self.eng.views.set_clip_active(False)
        self.rec = None
        return {"dir": str(rec["dir"]), **meta}
