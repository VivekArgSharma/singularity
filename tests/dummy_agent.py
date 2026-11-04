import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import math
import requests
import time

AGENT_BASE = 'http://localhost:8080'
ADMIN_BASE = 'http://localhost:8081'

def run_scripted_test(target_is_correct=True):
    print(f"\n[TEST] Running Scripted Dummy Agent Test (Target Correct: {target_is_correct})...")

    # 1. Reset scene & set command
    cmd = "Bring the round red thing"
    requests.post(f"{ADMIN_BASE}/admin/reset", timeout=5)
    requests.post(f"{ADMIN_BASE}/admin/command", json={"text": cmd}, timeout=5)

    if target_is_correct:
        # Avoid obj_6 at (-1.2, -0.2) by waypoint above it
        # Waypoint 1: turn +20 deg, move 1.4m -> lands near (-1.08, 0.48)
        requests.post(f"{AGENT_BASE}/turn", json={"degrees": 20.0})
        requests.post(f"{AGENT_BASE}/move", json={"distance": 1.4})

        # Waypoint 2: steer towards obj_2 (red sphere at 0.8, -0.4)
        st = requests.get(f"{AGENT_BASE}/state").json()
        dx = 0.8 - st['x']
        dy = -0.4 - st['y']
        target_deg = (math.atan2(dy, dx) * 180 / math.pi + 360) % 360
        turn_angle = (target_deg - st['heading'] + 180) % 360 - 180
        requests.post(f"{AGENT_BASE}/turn", json={"degrees": turn_angle})

        dist_to_obj = math.hypot(dx, dy)
        requests.post(f"{AGENT_BASE}/move", json={"distance": max(0.1, dist_to_obj - 0.40)})

        # Check pickup
        r_pick = requests.post(f"{AGENT_BASE}/pickup").json()
        print("  Pickup result:", r_pick)
        assert r_pick['ok'], "Pickup should succeed when in range"

        # Turn towards drop zone center (2.0, 1.8)
        st = requests.get(f"{AGENT_BASE}/state").json()
        cur_h = st['heading']
        dx = 2.0 - st['x']
        dy = 1.8 - st['y']
        target_deg = (math.atan2(dy, dx) * 180 / math.pi + 360) % 360
        turn_angle = (target_deg - cur_h + 180) % 360 - 180

        requests.post(f"{AGENT_BASE}/turn", json={"degrees": turn_angle})
        dist = math.hypot(dx, dy)
        requests.post(f"{AGENT_BASE}/move", json={"distance": max(0.1, dist - 0.2)})

        # Drop object into zone
        r_drop = requests.post(f"{AGENT_BASE}/drop").json()
        print("  Drop result:", r_drop)
        assert r_drop['ok'] and r_drop['in_zone'], "Drop should succeed inside green zone"

        # Check evaluation outcome
        res = requests.get(f"{ADMIN_BASE}/admin/result").json()
        print("  Grader Outcome:", res)
        assert res['outcome'] == 'pass', f"Expected 'pass', got '{res['outcome']}'"
        print("  [SUCCESS] SCRIPTED PASS TEST SUCCEEDED!")

    else:
        # Scripted trajectory to WRONG object: obj_1 (red cube at -0.5, -1.5)
        # dx = 1.9, dy = -1.5 -> atan2(-1.5, 1.9) = -38 deg
        requests.post(f"{AGENT_BASE}/turn", json={"degrees": -38.3})
        requests.post(f"{AGENT_BASE}/move", json={"distance": 1.95})

        r_pick = requests.post(f"{AGENT_BASE}/pickup").json()
        print("  Pickup result on wrong object:", r_pick)
        assert r_pick['ok'], "Pickup on wrong object should still physically attach"

        # Drive to drop zone
        st = requests.get(f"{AGENT_BASE}/state").json()
        dx = 2.0 - st['x']
        dy = 1.8 - st['y']
        target_deg = (math.atan2(dy, dx) * 180 / math.pi + 360) % 360
        turn_angle = (target_deg - st['heading'] + 180) % 360 - 180
        requests.post(f"{AGENT_BASE}/turn", json={"degrees": turn_angle})
        requests.post(f"{AGENT_BASE}/move", json={"distance": math.hypot(dx, dy) - 0.2})

        r_drop = requests.post(f"{AGENT_BASE}/drop").json()
        print("  Drop result:", r_drop)

        res = requests.get(f"{ADMIN_BASE}/admin/result").json()
        print("  Grader Outcome:", res)
        assert res['outcome'] == 'fail_wrong_object', f"Expected 'fail_wrong_object', got '{res['outcome']}'"
        print("  [SUCCESS] SCRIPTED FAIL_WRONG_OBJECT TEST SUCCEEDED!")

if __name__ == '__main__':
    run_scripted_test(target_is_correct=True)
    run_scripted_test(target_is_correct=False)
