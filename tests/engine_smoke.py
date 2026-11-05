"""Engine smoke test: starts the engine headless and drives it through the HTTP API.

Uses level1.json positions to script a fetch. That is test-only knowledge;
the agent never reads level files.

Run:  .venv\\Scripts\\python tests\\engine_smoke.py
"""
import io
import json
import math
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
from proc_util import kill_tree  # noqa: E402

PORT, ADMIN = 8090, 8091
LEVEL = json.loads((ROOT / "engine" / "levels" / "level1.json").read_text())
STATE_KEYS = {"name", "x", "y", "heading", "holding", "retries"}
fails = []


def req(port, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            raw = resp.read()
            return resp.status, (raw if path == "/look" else json.loads(raw))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def agent(method, path, body=None):
    return req(PORT, method, path, body)[1]


def admin(method, path, body=None):
    return req(ADMIN, method, path, body)[1]


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({info})" if info and not cond else ""))
    if not cond:
        fails.append(name)


def obj(oid):
    return next(o for o in LEVEL["objects"] if o["id"] == oid)


def face(tx, ty):
    s = agent("GET", "/state")
    target = math.degrees(math.atan2(-(ty - s["y"]), tx - s["x"]))
    delta = (target - s["heading"] + 180) % 360 - 180
    agent("POST", "/turn", {"degrees": delta})
    s = agent("GET", "/state")
    return math.hypot(tx - s["x"], ty - s["y"])


def scripted_fetch(oid, command, run_id):
    admin("POST", "/admin/reset", {"level": "level1", "seed": 0})
    admin("POST", "/admin/command", {"text": command, "run_id": run_id})
    o = obj(oid)
    ox, oy = o["pos"]
    r_obj = o.get("size", 0) / 2 if o["shape"] in ("cube", "sphere") else o["radius"]
    dist = face(ox, oy)
    m = agent("POST", "/move", {"distance": dist - r_obj - 0.2 - 0.15})
    face(ox, oy)
    p = agent("POST", "/pickup")
    agent("POST", "/move", {"distance": face(-0.6, 0.6)})   # waypoint clear of other objects
    zx, zy = LEVEL["zone"]["center"]
    dist = face(zx, zy)
    agent("POST", "/move", {"distance": dist - 0.65})
    d = agent("POST", "/drop")
    return m, p, d, admin("GET", "/admin/result")


def main():
    proc = subprocess.Popen([sys.executable, str(ROOT / "engine" / "sim.py"), "--headless",
                             "--port", str(PORT), "--admin-port", str(ADMIN)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            try:
                agent("GET", "/state")
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        t0 = time.time()

        # contract shapes
        s = agent("GET", "/state")
        check("state keys exact", set(s) == STATE_KEYS, s)
        check("starts Idle", s["name"] == "Idle", s)
        img = Image.open(io.BytesIO(agent("GET", "/look")))
        check("look is 640x480 JPEG", img.format == "JPEG" and img.size == (640, 480), img.size)
        dep = agent("GET", "/depth")
        check("depth key", set(dep) == {"depth"}, dep)
        code, _ = req(PORT, "POST", "/move", {"distance": "abc"})
        check("bad distance -> 400", code == 400, code)

        # correct fetch
        m, p, d, res = scripted_fetch("o1", "Bring the red cube to the green square.", "smoke_pass")
        check("move reply keys", set(m) == {"moved", "collided"}, m)
        check("pickup ok", p == {"ok": True}, p)
        check("drop in zone", d == {"ok": True, "in_zone": True}, d)
        check("outcome pass", res["outcome"] == "pass", res)
        check("state Done", agent("GET", "/state")["name"] == "Done")
        meta = ROOT / "clips" / "smoke_pass" / "meta.json"
        check("clip frames recorded", meta.exists() and json.loads(meta.read_text())["frames"] > 30)

        # wrong object
        _, _, _, res = scripted_fetch("o1", "Bring the blue sphere.", "smoke_wrong")
        check("outcome fail_wrong_object", res["outcome"] == "fail_wrong_object", res)

        # collisions -> Blocked, then fail_collision on the 4th
        admin("POST", "/admin/reset", {"level": "level1", "seed": 0})
        admin("POST", "/admin/command", {"text": "Bring the red cube.", "run_id": "smoke_coll"})
        m = agent("POST", "/move", {"distance": 3.0})
        s = agent("GET", "/state")
        check("collision stops early", m["collided"] and m["moved"] < 1.2, m)
        check("Blocked + retry 1", s["name"] == "Blocked" and s["retries"] == 1, s)
        for _ in range(3):
            agent("POST", "/move", {"distance": 1.0})
        res = admin("GET", "/admin/result")
        check("4th collision -> fail_collision", res["outcome"] == "fail_collision", res)

        # pickup out of reach
        admin("POST", "/admin/reset", {"level": "level1", "seed": 0})
        admin("POST", "/admin/command", {"text": "Bring the red cube.", "run_id": "smoke_reach"})
        check("pickup out of reach -> ok false", agent("POST", "/pickup") == {"ok": False})

        # busy -> 409 is exercised implicitly; leak scan of replies
        blob = json.dumps([agent("GET", "/state"), agent("GET", "/depth")]).lower()
        words = ["red", "blue", "yellow", "orange", "cube", "sphere", "cylinder", "cone", "o1", "zone"]
        check("no object words in replies", not any(w in blob for w in words), blob)

        print(f"\n{len(fails)} failed, wall time {time.time() - t0:.1f}s")
    finally:
        kill_tree(proc)
        out = proc.communicate(timeout=10)[0].decode(errors="replace")
        if fails:
            print(out[-2000:])
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
