"""Motion conditions while the robot moves, turns, picks up and carries.

Checks: exact speeds via the sim clock (1 m/s, 90 deg/s), clock frozen while idle,
409 when a second action arrives, wall clamp, 0.1 m back-off, the carried object
colliding before the body would, the +/-30 deg pickup limit, one object at a time,
drop with nothing held, and no motion after Done.

Run:  .venv\\Scripts\\python tests\\test_motion.py
"""
import json
import math
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
from proc_util import kill_tree  # noqa: E402

PORT, ADMIN = 8120, 8121
fails = []


def req(port, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def agent(m, p, b=None):
    return req(PORT, m, p, b)[1]


def admin(m, p, b=None):
    return req(ADMIN, m, p, b)[1]


def sim_t():
    return admin("GET", "/admin/result")["sim_seconds"]


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({info})" if info else ""))
    if not cond:
        fails.append(name)


def start(level, run_id):
    admin("POST", "/admin/reset", {"level": level, "seed": 0})
    admin("POST", "/admin/command", {"text": "Bring the yellow ball.", "run_id": run_id})


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

        # --- speeds and the frozen clock (calib level: robot at origin facing +x)
        start("calib", "motion_speed")
        t0 = sim_t()
        time.sleep(1.0)
        check("clock frozen while idle", sim_t() == t0, f"{t0} -> {sim_t()}")
        agent("POST", "/turn", {"degrees": 90})
        t1 = sim_t()
        check("turn 90 deg takes 1.00 s (90 deg/s)", abs((t1 - t0) - 1.0) < 0.03, f"{t1 - t0:.3f}")
        check("turn +90 is clockwise: heading 90", agent("GET", "/state")["heading"] == 90.0)
        m = agent("POST", "/move", {"distance": 1.0})            # heading 90 = toward -y, open floor
        t2 = sim_t()
        check("move 1.0 m moves exactly 1.0 m", m == {"moved": 1.0, "collided": False}, m)
        check("move 1 m takes 1.00 s (1 m/s)", abs((t2 - t1) - 1.0) < 0.03, f"{t2 - t1:.3f}")
        s = agent("GET", "/state")
        check("heading 90 moves toward -y", abs(s["x"]) < 0.01 and abs(s["y"] + 1.0) < 0.01, s)

        # --- 409 while busy, wall clamp, back-off
        agent("POST", "/turn", {"degrees": 90})                   # heading 180 = toward -x wall
        codes = []
        th = threading.Thread(target=lambda: codes.append(req(PORT, "POST", "/move", {"distance": 5.0})))
        th.start()
        time.sleep(0.15)
        second = req(PORT, "POST", "/turn", {"degrees": 10})
        th.join()
        check("second action while moving -> 409", second[0] == 409, second)
        mv = codes[0][1]
        s = agent("GET", "/state")
        check("wall stops the move early", mv["collided"] and mv["moved"] < 5.0, mv)
        check("robot stays inside the walls", s["x"] >= -2.8 - 1e-6, s)
        check("backed off 0.1 m from the contact point", abs((s["x"] + 2.8) - 0.1) < 0.02, s)
        check("collision -> Blocked, retries 1", s["name"] == "Blocked" and s["retries"] == 1, s)
        agent("POST", "/turn", {"degrees": 180})
        check("next action resumes from Blocked", agent("GET", "/state")["name"] != "Blocked")

        # --- pickup angle limit, single object, drop with nothing held (level1)
        start("level1", "motion_pick")
        check("drop with nothing held -> ok false", agent("POST", "/drop") == {"ok": False, "in_zone": False})
        agent("POST", "/move", {"distance": 0.75})                # robot to (-1.65, 0)
        cone_bearing = math.degrees(math.atan2(0.2, 0.45))        # cone at (-1.2, -0.2)
        agent("POST", "/turn", {"degrees": cone_bearing + 46})
        check("object 46 deg off heading -> no pickup", agent("POST", "/pickup") == {"ok": False})
        agent("POST", "/turn", {"degrees": -46})
        check("object centred within reach -> pickup", agent("POST", "/pickup") == {"ok": True})
        check("holding is true", agent("GET", "/state")["holding"] is True)
        check("second pickup while holding -> ok false", agent("POST", "/pickup") == {"ok": False})

        # --- the carried object collides before the body would (calib level)
        start("calib", "motion_carry")
        agent("POST", "/move", {"distance": 2.5 - 0.15 - 0.2 - 0.25})   # yellow ball ahead at 2.5 m
        check("pickup ahead", agent("POST", "/pickup") == {"ok": True})
        agent("POST", "/turn", {"degrees": -90})                  # face +y wall, object on the right
        mv = agent("POST", "/move", {"distance": 5.0})
        s = agent("GET", "/state")
        gap = 3.0 - s["y"]
        check("carried object hits the wall first (body never reaches it)",
              mv["collided"] and gap > 0.6, f"body {gap:.2f} m from wall")

        # --- nothing moves after Done
        admin("POST", "/admin/giveup")
        before = agent("GET", "/state")
        mv = agent("POST", "/move", {"distance": 1.0})
        check("no motion after Done", mv["moved"] == 0.0 and agent("GET", "/state") == before, mv)
    finally:
        kill_tree(proc)
        proc.communicate(timeout=10)
    print(f"\n{len(fails)} failed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
