"""Fresh-laptop / rehearsal check. Run before a demo:

    .venv\\Scripts\\python tools\\check_setup.py

Checks Python, every pinned package, FFmpeg, free ports, writable output folders, a Panda3D offscreen
render, and whether an API key is set (never prints it). Exit code 1 if anything blocking fails.
"""
import importlib.metadata as md
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
bad = 0


def report(ok, name, info="", blocking=True):
    global bad
    print(("OK   " if ok else ("FAIL " if blocking else "WARN ")) + name + (f"  - {info}" if info else ""))
    if not ok and blocking:
        bad += 1


report(sys.version_info >= (3, 11), "Python >= 3.11", sys.version.split()[0])
for line in (ROOT / "requirements.txt").read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    name, _, want = line.partition("==")
    try:
        have = md.version(name)
        report(not want or have == want, f"package {name}", f"have {have}" + (f", pinned {want}" if want and have != want else ""))
    except md.PackageNotFoundError:
        report(False, f"package {name}", "not installed: .venv\\Scripts\\python -m pip install -r requirements.txt")
try:
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=20).stdout.splitlines()[0]
    report(True, "FFmpeg (bundled with imageio-ffmpeg)", out[:60])
except Exception as e:  # noqa: BLE001
    report(False, "FFmpeg", str(e))
for port in (8080, 8081):
    with socket.socket() as s:
        s.settimeout(0.3)
        busy = s.connect_ex(("127.0.0.1", port)) == 0
    report(not busy, f"port {port} free", "an engine (or another program) is already running" if busy else "", blocking=False)
for d in ("outputs/runs", "clips", "outputs/eval"):
    p = ROOT / d
    try:
        p.mkdir(parents=True, exist_ok=True)
        (p / ".write_test").write_text("x")
        (p / ".write_test").unlink()
        report(True, f"writable {d}")
    except OSError as e:
        report(False, f"writable {d}", str(e))
free_gb = shutil.disk_usage(ROOT).free / 2 ** 30
report(free_gb > 2, "disk space", f"{free_gb:.1f} GB free (clips ~ 20-60 MB per run)", blocking=False)
r = subprocess.run([sys.executable, "-c", (
    "import sys; sys.path.insert(0, r'%s');"
    "from sim import Engine; e = Engine(headless=True); a = e.views.grab('look'); print(a.shape, int(a.mean()))") % (ROOT / "engine")],
    capture_output=True, text=True, timeout=120)
shape = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
report(r.returncode == 0 and "(480, 640, 3)" in shape, "Panda3D offscreen render (robot camera 640x480)", shape or r.stderr[-200:])
sys.path.insert(0, str(ROOT / "engine"))
from envfile import load_env  # noqa: E402
env_file = ROOT / ".env"
load_env()
ignored = ".env" in [x.strip() for x in (ROOT / ".gitignore").read_text().splitlines()] if (ROOT / ".gitignore").exists() else False
report(env_file.exists(), ".env file present", "copy .env.example to .env and fill it in" if not env_file.exists() else
       ("git-ignored" if ignored else "NOT in .gitignore: add it before committing"), blocking=False)
if env_file.exists() and not ignored:
    report(False, ".env is git-ignored", "add a line '.env' to .gitignore")
key = os.environ.get("ANTHROPIC_API_KEY")
report(bool(key), "ANTHROPIC_API_KEY set", "set (value not shown)" if key else
       "empty: the free local planner runs; put the key in .env for the Claude agent", blocking=False)
use = os.environ.get("USE_CLAUDE_API", "false").strip().lower() in ("1", "true", "yes", "on")
report(not (use and not key), "agent at start-up", (f"USE_CLAUDE_API={'true' if use else 'false'} -> "
       + ("Claude, model " + os.environ.get("AGENT_MODEL", "claude-haiku-5-5") if use and key else
          "free local planner" + (" (key missing!)" if use else ""))), blocking=False)
print(f"\n{bad} blocking problem(s)")
sys.exit(1 if bad else 0)
