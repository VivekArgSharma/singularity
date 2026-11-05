"""Scripted demo fetch for watching the engine window (no Claude involved).

Uses level1.json positions, so it is a demo/test driver only, never the agent.
Start the engine first:  .venv\\Scripts\\python engine\\sim.py
Then:                    .venv\\Scripts\\python tools\\demo_fetch.py
"""
import json
import math
import os
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEVEL = json.loads((ROOT / "engine" / "levels" / "level1.json").read_text())
AGENT = "http://127.0.0.1:" + os.environ.get("AGENT_PORT", "8080")
ADMIN = "http://127.0.0.1:" + os.environ.get("ADMIN_PORT", "8081")


def call(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=60) as resp:
        return json.loads(resp.read())


def face(tx, ty):
    s = call(AGENT, "GET", "/state")
    target = math.degrees(math.atan2(-(ty - s["y"]), tx - s["x"]))
    delta = (target - s["heading"] + 180) % 360 - 180
    print("turn", round(delta, 1), call(AGENT, "POST", "/turn", {"degrees": delta}))
    time.sleep(0.4)
    s = call(AGENT, "GET", "/state")
    return math.hypot(tx - s["x"], ty - s["y"])


def main():
    for _ in range(60):
        try:
            call(AGENT, "GET", "/state")
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    call(ADMIN, "POST", "/admin/reset", {"level": "level1", "seed": 0})
    call(ADMIN, "POST", "/admin/command", {"text": "Bring the red cube to the green square.",
                                           "run_id": os.environ.get("RUN_ID", "demo")})
    time.sleep(1.0)
    for deg in (-45, 90, -45):                     # look around first
        print("scan", call(AGENT, "POST", "/turn", {"degrees": deg}))
        time.sleep(0.4)
    cube = next(o for o in LEVEL["objects"] if o["id"] == "o1")
    d = face(*cube["pos"])
    print("move", call(AGENT, "POST", "/move", {"distance": d - 0.15 - 0.2 - 0.15}))
    face(*cube["pos"])
    print("pickup", call(AGENT, "POST", "/pickup"))
    time.sleep(0.5)
    print("move", call(AGENT, "POST", "/move", {"distance": face(-0.6, 0.6)}))
    zx, zy = LEVEL["zone"]["center"]
    print("move", call(AGENT, "POST", "/move", {"distance": face(zx, zy) - 0.65}))
    print("drop", call(AGENT, "POST", "/drop"))
    print("result", call(ADMIN, "GET", "/admin/result"))


if __name__ == "__main__":
    main()
