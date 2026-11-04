import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import argparse
from agent.admin_client import AdminClient
from agent.loop import run_episode

def main():
    parser = argparse.ArgumentParser(description="Membrane Robot Episode Harness")
    parser.add_argument("command", nargs="?", default="Bring the round red thing", help="Command to run")
    parser.add_argument("--provider", default="antigravity", choices=["antigravity", "anthropic", "pollinations"], help="Model provider (default: antigravity)")
    parser.add_argument("--model", default=None, help="Model name override")
    parser.add_argument("--api-key", default=None, help="API key override")
    parser.add_argument("--base-url", default=None, help="Proxy base URL override")
    args = parser.parse_args()

    command = args.command
    print(f"==================================================")
    print(f"[HARNESS] MEMBRANE FETCH HARNESS: '{command}'")
    print(f"   Provider: {args.provider}")
    print(f"==================================================")

    if args.provider == 'antigravity':
        from agent.antigravity_agent import run_antigravity_episode
        model = args.model or "claude-sonnet-4-6"
        base_url = args.base_url or os.environ.get("ANTIGRAVITY_BASE_URL", "http://127.0.0.1:8317/v1")
        api_key = args.api_key or os.environ.get("ANTIGRAVITY_API_KEY", "12345")
        res = run_antigravity_episode(command, base_url=base_url, api_key=api_key, model=model)
        return res

    admin = AdminClient('http://localhost:8081')

    # 1. Reset scene
    print("1. Resetting engine scene...")
    try:
        admin.reset()
    except Exception as e:
        print(f"[ERROR] Failed to connect to admin port 8081: {e}")
        print("Please ensure 'npm start' is running and the browser engine tab is open at http://localhost:8080.")
        sys.exit(1)

    # 2. Set command in engine (so engine transitions from Idle to Locate and records command)
    print(f"2. Setting command in engine: '{command}'...")
    admin.set_command(command)

    # 3. Run Claude Agent Loop
    print("3. Executing agent loop...")
    kwargs = {}
    if args.model:
        kwargs['model'] = args.model
    summary = run_episode(command, **kwargs)

    # 4. If step limit reached, signal give up
    if summary['steps'] >= 60:
        print("[WARN] Maximum 60 steps reached. Signaling giveUp to engine...")
        admin.give_up()

    # 5. Fetch Ground Truth Outcome from Engine Grader
    res = admin.result()
    print("==================================================")
    print("EPISODE EVALUATION RESULT:")
    print(f"   Outcome:      {res.get('outcome')}")
    print(f"   Steps:        {res.get('steps')}")
    print(f"   Sim Time:     {res.get('simSeconds')} s")
    print(f"   Collisions:   {res.get('collisions')}")
    print(f"   Path Length:  {res.get('pathLength')} m")
    print(f"   Logs saved:   {summary['run_dir']}")
    print("==================================================")

if __name__ == '__main__':
    main()
