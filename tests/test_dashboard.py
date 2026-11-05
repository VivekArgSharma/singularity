"""Dashboard / operator interface checks, plus the instruction classifier.

Run:  .venv\\Scripts\\python tests\\test_dashboard.py
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
from proc_util import kill_tree  # noqa: E402

sys.path.insert(0, str(ROOT / "engine"))
from controller import classify  # noqa: E402

AP, OP = 8160, 8161
AGENT, ADMIN = f"http://127.0.0.1:{AP}", f"http://127.0.0.1:{OP}"
fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({info})" if info and not cond else ""), flush=True)
    if not cond:
        fails.append(name)


def get(base, path, method="GET", body=None):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                 method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def classifier():
    cases = {
        "Bring the red cube to the green square.": "task",
        "Go and pick up the blue sphere instead.": "task",
        "Actually, go around the blue sphere.": "task",
        "No, pick up the orange cone.": "task",
        "Can you circle the yellow cylinder twice?": "task",
        "What are you doing?": "status",
        "How far have you travelled?": "status",
        "where are you": "status",
        "Are you holding anything?": "status",
        "stop": "stop", "Stop now!": "stop", "cancel that": "stop",
        "pause": "pause", "wait a moment": "pause",
        "resume": "resume", "continue": "resume", "keep going": "resume",
        "reset the environment": "reset",
    }
    for text, want in cases.items():
        got = classify(text, has_live_task=True)
        check(f"classify {text!r} -> {want}", got == want, got)
    check("classify 'slower' with a live task -> refine", classify("slower", True) == "refine")
    check("classify 'make it three times' -> refine", classify("make it three times", True) == "refine")
    check("classify 'slower' with no task -> task", classify("slower", False) == "task")
    check("classify 'closer to the red cube instead' -> task", classify("closer to the red cube instead", True) == "task")


def main():
    classifier()
    eng = subprocess.Popen([sys.executable, str(ROOT / "engine" / "sim.py"), "--headless", "--port", str(AP),
                            "--admin-port", str(OP), "--brain", "local"], cwd=str(ROOT),
                           env={**os.environ, "FETCH_RUNS_DIR": str(ROOT / "outputs" / "test_runs")},
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(120):
            try:
                get(ADMIN, "/admin/status")
                break
            except OSError:
                time.sleep(0.25)
        code, ctype, html = get(ADMIN, "/")
        page = html.decode()
        check("title screen served at / on the operator port", code == 200 and "text/html" in ctype)
        for el in ('id="play"', 'id="options"', 'id="instructions"', 'id="credits"', "/dashboard/console.html"):
            check(f"title screen has {el}", el in page)
        code, ctype, html = get(ADMIN, "/dashboard/console.html")
        page = html.decode()
        check("console served", code == 200 and "text/html" in ctype)
        for el in ('id="prompt"', 'id="pause"', 'id="resume"', 'id="stop"', 'id="v-chase"', 'id="v-look"', 'id="v-top"',
                   'id="map"', 'id="log-lines"', '/admin/mission', '/admin/instruct', 'data-view="objectives"', 'data-view="runs"'):
            check(f"console has {el}", el in page)
        for asset in ("title-screen.jpg", "title.png", "logo-robot.png", "ico-send.png", "corner-left.png"):
            code, ctype, _ = get(ADMIN, f"/dashboard/assets/{asset}")
            check(f"asset {asset} served", code == 200 and "image" in ctype)
        code, ctype, html = get(ADMIN, "/dashboard/engineering.html")
        page = html.decode()
        for el in ('id="map"', 'id="cov"', 'id="stats"', 'id="route"', 'id="robot"', 'id="exec"', 'id="runs"', 'id="player"',
                   "/admin/map", "/admin/status", "/admin/runs", "/dashboard/console.html"):
            check(f"engineering page has {el}", el in page)
        code, _, body = get(ADMIN, "/admin/status")
        s = json.loads(body)
        for k in ("mode", "brain", "task", "last_instruction", "interrupted", "history", "feed", "robot", "engine", "actions"):
            check(f"status has '{k}'", k in s)
        check("status robot block has pose, holding, retries, collisions", {"x", "y", "heading", "holding", "retries",
              "collisions", "sim_seconds"} <= set(s["robot"]))
        check("status mode waiting at start", s["mode"] == "waiting")
        code, _, _ = get(ADMIN, "/admin/instruct", "POST", {"text": ""})
        check("empty instruction rejected (400)", code == 400)
        code, _, body = get(ADMIN, "/admin/instruct", "POST", {"text": "What are you doing?"})
        r = json.loads(body)
        check("status question when idle answered, no task started", r["kind"] == "status" and "waiting" in r["reply"], r)
        code, ctype, body = get(ADMIN, "/admin/view/chase")
        check("live chase view JPEG", code == 200 and ctype == "image/jpeg" and body[:2] == b"\xff\xd8")
        code, ctype, body = get(ADMIN, "/admin/view/look")
        check("live robot camera JPEG", code == 200 and body[:2] == b"\xff\xd8")
        code, _, body = get(ADMIN, "/admin/runs")
        check("runs listing", code == 200 and "runs" in json.loads(body))
        code, _, _ = get(ADMIN, "/admin/runs/..%2f..%2fengine/sim.py")
        check("run file path traversal refused", code in (400, 404))
        # agent port: no dashboard, no operator endpoints, strict validation
        for path in ("/", "/admin/status", "/admin/view/top", "/admin/runs"):
            code, _, _ = get(AGENT, path)
            check(f"agent port has no {path}", code == 404, code)
        code, _, _ = get(AGENT, "/arc", "POST", {"radius": 0.0, "degrees": 90})
        check("arc radius below 0.1 m rejected", code == 400)
        code, _, _ = get(AGENT, "/move", "POST", {"distance": 0.1, "speed": 2.0})
        check("speed above 1.0 rejected (never faster than the spec)", code == 400)
        code, _, body = get(AGENT, "/state")
        check("agent /state keeps the problem-statement keys only",
              set(json.loads(body)) == {"name", "x", "y", "heading", "holding", "retries"})
    finally:
        kill_tree(eng)
    print(f"\n{len(fails)} failed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
