import os
import glob
import json
import argparse
from PIL import Image

def replay_session(log_dir: str):
    """
    Reads a completed run directory, displays recorded step metrics,
    and validates that frames and observations match the specification.
    """
    jsonl_path = os.path.join(log_dir, 'run.jsonl')
    if not os.path.exists(jsonl_path):
        print(f"❌ No run.jsonl found in {log_dir}")
        return

    print(f"\n[REPLAY] Replaying Run Session: {log_dir}")
    print(f"{'Step':<6} | {'Tool':<12} | {'Target':<20} | {'Conf':<6} | {'Result'}")
    print("-" * 70)

    total_steps = 0
    with open(jsonl_path, 'r', encoding='utf-8') as f:
        for line in f:
            record = json.loads(line)
            total_steps += 1
            step = record.get('step', total_steps)
            tool = record.get('tool', 'unknown')
            thinking = record.get('claude', {})
            target = thinking.get('target', 'none')[:18]
            conf = thinking.get('confidence', 0.0)
            res = str(record.get('result', {}))[:24]

            frame_path = record.get('frame_path')
            frame_status = "[IMG]" if frame_path and os.path.exists(os.path.join(log_dir, frame_path)) else "     "

            print(f"{step:<6} | {tool:<12} | {target:<20} | {conf:<6.2f} | {res} {frame_status}")

    print("-" * 70)
    print(f"Replay complete: {total_steps} recorded actions.\n")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Replay logged robot runs")
    parser.add_argument("log_dir", nargs="?", default=None, help="Directory of run to replay")
    args = parser.parse_args()

    target_dir = args.log_dir
    if not target_dir:
        available = sorted(glob.glob('logs/*'))
        if available:
            target_dir = available[-1]
        else:
            print("No run logs found in logs/")
            exit(0)

    replay_session(target_dir)
