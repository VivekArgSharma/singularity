"""Frame-pacing profile of the windowed engine under five workloads.

    .venv\\Scripts\\python tools\\profile_engine.py [--label before] [--seconds 8]

Starts its own window on ports 8090/8091 (close other engines first so they don't share the GPU),
drives the robot (move/turn back and forth) under each workload, and reads /admin/perf:
  1 moving, not recording        2 + robot camera pulled like the dashboard POV (2.5 Hz)
  3 + mapping traffic (/look + /scan every 0.5 s, as the agent does per observation)
  4 moving while recording the clip
  5 everything: recording + dashboard views (chase 5.5 Hz, look 2.5 Hz, top 1.7 Hz) + mapping traffic
Results -> outputs/eval/perf_<label>.json and .md
"""
import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
from proc_util import kill_tree  # noqa: E402

AG, AD = "http://127.0.0.1:8090", "http://127.0.0.1:8091"


def req(url, body=None, method=None, raw=False):
    r = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                               method=method or ("POST" if body is not None else "GET"),
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=120) as f:
        data = f.read()
    return data if raw else json.loads(data)


def pullers(spec, stop):
    def loop(url, period):
        while not stop.is_set():
            t = time.time()
            try:
                req(url, raw=True)
            except Exception:  # noqa: BLE001
                pass
            stop.wait(max(0.0, period - (time.time() - t)))
    ts = [threading.Thread(target=loop, args=a, daemon=True) for a in spec]
    for t in ts:
        t.start()
    return ts


DASH = [(AD + "/admin/view/chase", 0.18), (AD + "/admin/view/look", 0.40), (AD + "/admin/view/top", 0.60),
        (AD + "/admin/status", 0.40)]
MAP = [(AG + "/look", 0.5), (AG + "/scan?n=91", 0.5)]
SCEN = [("1 moving, no recording", False, []),
        ("2 + POV camera (2.5 Hz)", False, [(AD + "/admin/view/look", 0.40)]),
        ("3 + mapping traffic", False, MAP),
        ("4 moving while recording", True, []),
        ("5 everything together", True, DASH + MAP)]


def run(seconds):
    out = []
    for name, record, spec in SCEN:
        req(AD + "/admin/reset", {"level": "level1", "seed": 0})
        req(AD + "/admin/command", {"text": "profile run", "run_id": "profile"})
        if not record:
            req(AD + "/admin/record_stop", {})
        req(AG + "/turn", {"degrees": 90})                     # face along open floor (+y side is clear of objects here)
        stop = threading.Event()
        ts = pullers(spec, stop)
        time.sleep(0.5)
        req(AD + "/admin/perf", {"reset": True})
        t0 = time.time()
        i = 0
        while time.time() - t0 < seconds:
            op = [("move", {"distance": 0.8}), ("turn", {"degrees": 180}), ("move", {"distance": 0.8}),
                  ("turn", {"degrees": -180})][i % 4]
            req(AG + "/" + op[0], op[1])
            i += 1
        p = req(AD + "/admin/perf")
        stop.set()
        for t in ts:
            t.join(timeout=3)
        p["scenario"] = name
        out.append(p)
        print(name, json.dumps({k: p[k] for k in ("fps", "frame_ms", "frames_over_20ms", "sim_steps_per_s",
                                                    "substeps_per_frame", "cpu_pct")}), flush=True)
    req(AD + "/admin/record_stop", {})
    return out


def md(rows, label):
    keys = ["fps", "mean", "p95", "p99", "max", ">20ms", "sim steps/s", "cm/frame p95", "cpu %", "MB"]
    lines = [f"# Engine frame pacing ({label})\n", "| scenario | " + " | ".join(keys) + " |",
             "|---|" + "---|" * len(keys)]
    for p in rows:
        f = p["frame_ms"]
        mv = p.get("robot_cm_per_frame_while_moving", {})
        vals = [p["fps"], f["mean"], f["p95"], f["p99"], f["max"], f"{100 * (p['frames_over_20ms'] or 0):.0f}%",
                p["sim_steps_per_s"], mv.get("p95"), p["cpu_pct"], p["rss_mb"]]
        lines.append(f"| {p['scenario']} | " + " | ".join(str(v) for v in vals) + " |")
    lines.append("\nMain-thread time per second of wall clock (ms/s), scenario 5:\n")
    last = rows[-1]
    lines.append("| part | ms per s | ms per call | calls |\n|---|---|---|---|")
    for k, v in sorted(last["main_thread_ms_per_s"].items(), key=lambda kv: -kv[1]):
        lines.append(f"| {k} | {v} | {last['ms_per_call'][k]} | {last['calls'][k]} |")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="now")
    ap.add_argument("--seconds", type=float, default=8.0)
    a = ap.parse_args()
    eng = subprocess.Popen([sys.executable, str(ROOT / "engine" / "sim.py"), "--port", "8090", "--admin-port", "8091",
                            "--brain", "local"], cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(160):
            try:
                req(AD + "/admin/perf")
                break
            except OSError:
                time.sleep(0.25)
        time.sleep(2.0)                                         # let the window settle
        rows = run(a.seconds)
    finally:
        kill_tree(eng)
    d = ROOT / "outputs" / "eval"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"perf_{a.label}.json").write_text(json.dumps(rows, indent=2))
    text = md(rows, a.label)
    (d / f"perf_{a.label}.md").write_text(text)
    print(text)


if __name__ == "__main__":
    main()
