import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import json
import base64
import argparse
import requests
from agent.engine_client import EngineClient
from agent.admin_client import AdminClient
from agent.logger import RunLogger

OLLAMA_API = 'http://127.0.0.1:11434/api/generate'
DEFAULT_MODEL = 'moondream:latest'

ACTIONS = {
    'move': lambda e, a: e.move(a.get('distance', 0.4)),
    'turn': lambda e, a: e.turn(a.get('degrees', 45)),
    'pickup': lambda e, a: e.pickup(),
    'drop': lambda e, a: e.drop(),
    'read_depth': lambda e, a: e.depth(),
}

LOOK_AFTER = {'move', 'turn'}

def run_ollama_episode(command: str, model: str = DEFAULT_MODEL, max_steps: int = 25):
    engine = EngineClient('http://localhost:8080')
    admin = AdminClient('http://localhost:8081')
    logger = RunLogger()

    print(f"\n==================================================")
    print(f"[Ollama Agent] Launching run with model: {model}")
    print(f"               Command: \"{command}\"")
    print(f"==================================================")

    # 1. Reset & Set command
    admin.reset()
    admin.set_command(command)

    # Initial frame
    frame = engine.look()
    logger.save_frame(0, frame)

    for step in range(1, max_steps + 1):
        # Read current sensor readings
        state = engine.state()
        depth_data = engine.depth()
        current_depth = depth_data.get('depth')
        is_holding = state.get('holding', False)

        prompt = f"""You are the eyes of a mobile robot.
User mission: "{command}"
Currently holding object: {'YES (carrying object to green zone)' if is_holding else 'NO (looking for target)'}

Describe what objects, colors, and shapes you see in this camera view.
Explicitly state:
1. Do you see the target object matching "{command}"? If yes, is it on the LEFT, CENTER, or RIGHT of your view?
2. Do you see the green square drop zone on the floor? If yes, where is it (left, center, right)?"""

        # Call Ollama
        payload = {
            'model': model,
            'prompt': prompt,
            'stream': False,
            'images': [base64.b64encode(frame).decode('utf-8')],
            'options': {
                'temperature': 0.1,
                'num_predict': 200
            }
        }

        parsed = None
        raw_text = ''
        try:
            resp = requests.post(OLLAMA_API, json=payload, timeout=40)
            if resp.status_code == 200:
                raw_text = resp.json().get('response', '')
                print(f"  [VLM RAW OUTPUT Step {step:02d}]:")
                print(f"  >>> \"{raw_text.strip()}\"")
                import re
                match = re.search(r'\{[\s\S]*\}', raw_text)
                if match:
                    parsed = json.loads(match.group(0))
        except Exception as e:
            print(f"  [Step {step}] Ollama call warning: {e}")

        # Natural Language Understanding Fallback
        if not parsed or 'tool' not in parsed:
            lower = raw_text.lower()
            cmd_lower = command.lower()

            target_detected = False
            if 'red' in cmd_lower and 'red' in lower: target_detected = True
            elif 'blue' in cmd_lower and 'blue' in lower: target_detected = True
            elif 'yellow' in cmd_lower and ('yellow' in lower or 'can' in lower or 'drink' in lower): target_detected = True
            elif 'orange' in cmd_lower and ('orange' in lower or 'cone' in lower): target_detected = True
            elif 'cone' in cmd_lower and 'cone' in lower: target_detected = True
            elif 'sphere' in cmd_lower and ('sphere' in lower or 'ball' in lower or 'circle' in lower): target_detected = True
            elif 'cube' in cmd_lower and ('cube' in lower or 'box' in lower): target_detected = True

            if not is_holding:
                if target_detected:
                    if 'left' in lower:
                        parsed = {"observations": [raw_text.strip()], "target": command, "confidence": 0.85, "tool": "turn", "args": {"degrees": -25}}
                    elif 'right' in lower:
                        parsed = {"observations": [raw_text.strip()], "target": command, "confidence": 0.85, "tool": "turn", "args": {"degrees": 25}}
                    else:
                        if current_depth is not None and current_depth <= 0.55:
                            parsed = {"observations": [raw_text.strip()], "target": command, "confidence": 0.95, "tool": "pickup", "args": {}}
                        else:
                            dist = min(max(0.3, (current_depth or 1.0) * 0.5), 1.2)
                            parsed = {"observations": [raw_text.strip()], "target": command, "confidence": 0.85, "tool": "move", "args": {"distance": dist}}
                else:
                    parsed = {"observations": [raw_text.strip() or 'Scanning room'], "target": command, "confidence": 0.4, "tool": "turn", "args": {"degrees": 45}}
            else:
                if 'green' in lower or 'zone' in lower or 'square' in lower:
                    if 'left' in lower:
                        parsed = {"observations": [raw_text.strip()], "target": "green zone", "confidence": 0.9, "tool": "turn", "args": {"degrees": -25}}
                    elif 'right' in lower:
                        parsed = {"observations": [raw_text.strip()], "target": "green zone", "confidence": 0.9, "tool": "turn", "args": {"degrees": 25}}
                    else:
                        parsed = {"observations": [raw_text.strip()], "target": "green zone", "confidence": 0.95, "tool": "drop", "args": {}}
                else:
                    parsed = {"observations": [raw_text.strip() or 'Searching green zone'], "target": "green zone", "confidence": 0.5, "tool": "turn", "args": {"degrees": 50}}

        tool_name = parsed.get('tool', 'turn')
        tool_args = parsed.get('args', {})
        target = parsed.get('target', command)[:20]
        conf = parsed.get('confidence', 0.5)

        print(f"  Step {step:02d} | Tool: {tool_name:<10} | Target: '{target}' (Conf: {conf:.2f}) | Args: {tool_args}\n")

        if tool_name == 'done':
            logger.log_step(step, tool_name, parsed, {}, None)
            print(f"[Ollama Agent] Step {step}: Model signaled 'done'.")
            break

        action_fn = ACTIONS.get(tool_name, ACTIONS['turn'])
        action_res = action_fn(engine, tool_args)

        frame_path = None
        if tool_name in LOOK_AFTER:
            frame = engine.look()
            frame_path = logger.save_frame(step, frame)

        logger.log_step(step, tool_name, parsed, action_res, frame_path)

    # Check Ground Truth Outcome from Engine Grader
    res = admin.result()
    print("==================================================")
    print("🎯 OLLAMA EPISODE RESULT:")
    print(f"   Outcome:      {res.get('outcome')}")
    print(f"   Steps:        {res.get('steps')}")
    print(f"   Sim Time:     {res.get('simSeconds')} s")
    print(f"   Collisions:   {res.get('collisions')}")
    print(f"   Path Length:  {res.get('pathLength')} m")
    print("==================================================")
    return res

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Run local Ollama vision agent")
    parser.add_argument("command", nargs="?", default="Bring the round red thing", help="Command to fetch")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Ollama model to use")
    args = parser.parse_args()

    run_ollama_episode(args.command, model=args.model)
