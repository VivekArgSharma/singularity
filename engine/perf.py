"""Engine profiler: frame pacing, simulation rate and where the main-loop time goes.

Everything that could make the window stutter runs on the Panda3D main thread, so the
engine times each part of its tick (view captures, simulation steps, scene sync, clip
recording, HUD) and the gap between ticks (= the frame time the operator sees).
GET /admin/perf returns the summary; POST /admin/perf {"reset": true} starts a new window.
"""
import ctypes
import math
import os
import time
from collections import defaultdict, deque


def _rss_mb():
    if os.name != "nt":
        try:
            with open("/proc/self/statm") as f:
                return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2 ** 20
        except OSError:
            return None

    class PMC(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
    try:
        c = PMC()
        c.cb = ctypes.sizeof(PMC)
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.K32GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        if not k32.K32GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
            return None
        return c.WorkingSetSize / 2 ** 20
    except Exception:  # noqa: BLE001
        return None


def _pct(xs, p):
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, int(math.ceil(p / 100 * len(s))) - 1)]


class _Section:
    __slots__ = ("perf", "name", "t0")

    def __init__(self, perf, name):
        self.perf, self.name = perf, name

    def __enter__(self):
        self.t0 = time.perf_counter()

    def __exit__(self, *exc):
        dt = time.perf_counter() - self.t0
        self.perf.total[self.name] += dt
        self.perf.count[self.name] += 1


class Perf:
    def __init__(self, keep=3600):
        self.keep = keep
        self.reset()

    def reset(self):
        self.frames = deque(maxlen=self.keep)          # wall seconds between ticks
        self.moving_step = deque(maxlen=self.keep)     # robot displacement per rendered frame while moving (m)
        self.substeps = defaultdict(int)
        self.total = defaultdict(float)
        self.count = defaultdict(int)
        self.sim_steps = 0
        self.t_start = time.perf_counter()
        self.cpu_start = time.process_time()
        self.last = None
        self.last_pose = None

    def section(self, name):
        return _Section(self, name)

    def tick(self):
        now = time.perf_counter()
        if self.last is not None:
            self.frames.append(now - self.last)
        self.last = now

    def stepped(self, n, pose=None):
        self.sim_steps += n
        self.substeps[n] += 1
        if pose is not None:
            if self.last_pose is not None and n:
                self.moving_step.append(math.hypot(pose[0] - self.last_pose[0], pose[1] - self.last_pose[1]))
            self.last_pose = pose
        else:
            self.last_pose = None

    def summary(self):
        wall = max(1e-6, time.perf_counter() - self.t_start)
        f = [x * 1000 for x in self.frames]
        n = len(f)
        out = {
            "window_s": round(wall, 2),
            "frames": n,
            "fps": round(n / wall, 1),
            "frame_ms": {"mean": round(sum(f) / n, 2) if n else None, "p50": _r(_pct(f, 50)),
                         "p95": _r(_pct(f, 95)), "p99": _r(_pct(f, 99)), "max": _r(max(f) if f else None)},
            "frames_over_20ms": round(sum(1 for x in f if x > 20) / n, 3) if n else None,
            "frames_over_33ms": round(sum(1 for x in f if x > 33.4) / n, 3) if n else None,
            "sim_steps_per_s": round(self.sim_steps / wall, 1),
            "substeps_per_frame": dict(sorted(self.substeps.items())),
            "cpu_pct": round(100 * (time.process_time() - self.cpu_start) / wall, 1),
            "rss_mb": _r(_rss_mb()),
            "main_thread_ms_per_s": {k: round(1000 * v / wall, 2) for k, v in sorted(self.total.items())},
            "ms_per_call": {k: round(1000 * v / max(1, self.count[k]), 2) for k, v in sorted(self.total.items())},
            "calls": dict(sorted(self.count.items())),
        }
        m = [x * 100 for x in self.moving_step]
        if m:
            out["robot_cm_per_frame_while_moving"] = {"p50": _r(_pct(m, 50)), "p95": _r(_pct(m, 95)),
                                                      "max": _r(max(m))}
        return out


def _r(x):
    return None if x is None else round(x, 2)
