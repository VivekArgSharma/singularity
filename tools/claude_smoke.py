"""Claude plumbing test (spends real API credit, a few cents): one command, cheap model, short step cap.

    .venv\\Scripts\\python tools\\claude_smoke.py                      # Haiku 5.5, 12 steps
    .venv\\Scripts\\python tools\\claude_smoke.py --model claude-sonnet-5-5 --steps 60 --command "Fetch the blue sphere."

Reads ANTHROPIC_API_KEY from .env. Starts its own headless engine (ports 8730/8731), runs the command with the
Claude agent, prints every step from steps.jsonl and the recorded spend (outputs/budget.json).
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
from envfile import load_env  # noqa: E402
from proc_util import kill_tree  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-haiku-5-5")
    ap.add_argument("--steps", type=int, default=12)
    ap.add_argument("--command", default="Bring the red cube to the green square.")
    a = ap.parse_args()
    load_env()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("No ANTHROPIC_API_KEY: put it in .env (copy .env.example) and run again.")
    runs = ROOT / "outputs" / "claude_smoke"
    env = {**os.environ, "USE_CLAUDE_API": "true", "AGENT_MODEL": a.model, "AGENT_MAX_STEPS": str(a.steps),
           "FETCH_RUNS_DIR": str(runs)}
    eng = subprocess.Popen([sys.executable, str(ROOT / "engine" / "sim.py"), "--headless", "--port", "8730",
                            "--admin-port", "8731"], cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    A = "http://127.0.0.1:8731"

    def call(m, p, b=None):
        req = urllib.request.Request(A + p, data=json.dumps(b).encode() if b is not None else None, method=m,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as f:
            return json.loads(f.read())
    try:
        for _ in range(120):
            try:
                s = call("GET", "/admin/status")
                break
            except OSError:
                time.sleep(0.25)
        print(f"agent: {s['brain']} · model {s['model']} · step cap {a.steps}")
        t = call("POST", "/admin/instruct", {"text": a.command})
        t0 = time.time()
        while time.time() - t0 < 600:
            s = call("GET", "/admin/status")
            h = next(x for x in s["history"] if x["id"] == t["task_id"])
            if h["status"] not in ("Starting", "Running"):
                break
            time.sleep(1)
        d = runs / t["task_id"]
        for line in (d / "steps.jsonl").read_text(encoding="utf-8").splitlines() if (d / "steps.jsonl").exists() else []:
            x = json.loads(line)
            print(f"  {x['step']:3d} {x['tool']:7} {json.dumps(x['args'])[:40]:42} {json.dumps(x['result'])[:60]} {x['frame_path'] or ''}")
        print(f"\nresult: {h['status']} · outcome {s['robot']['outcome']} · {h.get('result')}")
        b = ROOT / "outputs" / "budget.json"
        if b.exists():
            print("spend so far:", json.loads(b.read_text()).get("spent_usd"))
        print("run folder:", d)
    finally:
        kill_tree(eng)


if __name__ == "__main__":
    main()
