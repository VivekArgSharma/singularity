"""Drive the dashboard the way a person would, to demonstrate live task replacement.

    .venv\\Scripts\\python engine\\sim.py          (window + dashboard on :8081)
    .venv\\Scripts\\python tools\\demo_dashboard.py

Sends instructions to /admin/instruct exactly like typing them in the dashboard:
a fetch that is replaced half-way, a delivery of the object then held, a double
orbit with status questions asked while it runs.
"""
import json
import os
import time
import urllib.request

ADMIN = "http://127.0.0.1:" + os.environ.get("ADMIN_PORT", "8081")


def call(method, path, body=None):
    req = urllib.request.Request(ADMIN + path, data=json.dumps(body).encode() if body is not None else None,
                                 method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def say(text):
    r = call("POST", "/admin/instruct", {"text": text})
    print(f"> {text}\n  [{r['kind']}] {r.get('reply', '')}")
    return r


def wait_done(tid, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = call("GET", "/admin/status")
        t = next((h for h in s["history"] if h["id"] == tid), None)
        if t and t["status"] not in ("Starting", "Running", "Paused"):
            print(f"  -> {t['status']}: {t['result']}  ({t['actions']} actions)")
            return t
        time.sleep(0.3)


def wait_action(op, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if (call("GET", "/admin/status")["engine"]["action"] or "").startswith(op):
            return
        time.sleep(0.05)


def main():
    for _ in range(80):
        try:
            call("GET", "/admin/status")
            break
        except OSError:
            time.sleep(0.5)
    call("POST", "/admin/reset", {"level": "level1", "seed": 0})
    a = say("Bring the red cube to the green square.")
    wait_action("move")
    time.sleep(0.6)                                  # part-way to the red cube
    b = say("Go and pick up the blue sphere instead.")
    wait_done(b["task_id"])
    c = say("Bring the blue sphere to the green square.")
    wait_done(c["task_id"])
    d = say("Circle around the yellow cylinder twice.")
    wait_action("arc")
    time.sleep(2.0)
    say("What are you doing?")
    say("How far have you travelled?")
    wait_done(d["task_id"])
    time.sleep(8)                                    # let the last video finish encoding
    for r in call("GET", "/admin/runs")["runs"][:4]:
        print(f"  {r['run_id']}: {r['command']}  {r['videos']}")
    print(f"videos in {call('GET', '/admin/runs')['dir']}")


if __name__ == "__main__":
    main()
