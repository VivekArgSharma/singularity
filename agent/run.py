"""Agent process for one task. Started by the operator controller; talks only to the
agent API. Prints one JSON event per line on stdout for the dashboard.

    python agent/run.py --task-id T --command "Bring the red cube to the green square." \
        [--brain claude|local] [--context "..."] [--url http://127.0.0.1:8080]

Exit codes: 0 done, 1 failed, 3 superseded by a newer task (stale).
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from client import Client, Stale  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--command", required=True)
    ap.add_argument("--context", default="")
    ap.add_argument("--brain", default="auto", choices=("auto", "claude", "local"))
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--map-file", default=None, help="the robot's own map, kept between tasks")
    ap.add_argument("--log-dir", default=None, help="where steps.jsonl and look_frames/*.png go (the run folder)")
    a = ap.parse_args()

    def emit(kind, **kw):
        print(json.dumps({"type": kind, "task_id": a.task_id, **kw}, default=str), flush=True)

    brain = a.brain
    if brain == "auto":
        brain = "claude" if os.environ.get("ANTHROPIC_API_KEY") else "local"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))
    from envfile import load_env
    load_env()                                     # standalone runs read .env too (the engine already did)
    cl = Client(a.url, a.task_id, emit, log_dir=a.log_dir)
    try:
        if brain == "claude":
            from claude_brain import ClaudeBrain
            ok, summary = ClaudeBrain(cl, emit, a.command, a.context).run()
        else:
            from local_brain import LocalBrain
            ok, summary = LocalBrain(cl, emit, a.command, a.context, map_file=a.map_file).run()
    except Stale:
        return 3                                   # a newer task owns the robot; say nothing
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ == "BudgetExceeded":
            emit("done", ok=False, summary=f"stopped: {e}")
        elif type(e).__name__ == "AuthenticationError":
            emit("done", ok=False, summary="Claude API key rejected (401): check ANTHROPIC_API_KEY in .env, "
                                           "or switch to the free local planner in Settings")
        else:
            import traceback
            traceback.print_exc()                  # -> agent_stderr.log in the run folder
            emit("done", ok=False, summary=f"agent error: {type(e).__name__}: {e}")
        return 1
    emit("done", ok=ok, summary=summary)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
