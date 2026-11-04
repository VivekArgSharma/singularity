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

# Easy CLI Desktop Proxy Configuration (Antigravity limits)
DEFAULT_BASE_URL = os.environ.get("ANTIGRAVITY_BASE_URL", "http://127.0.0.1:8317/v1")
DEFAULT_API_KEY = os.environ.get("ANTIGRAVITY_API_KEY", "12345")
DEFAULT_MODEL = "claude-sonnet-4-6"

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "move",
            "description": "Drive robot forward (positive distance) or backward (negative) by any exact distance in metres. Scale: 1 floor block tile = 0.5m (e.g. 1 block = 0.5m, 2 blocks = 1.0m, 4 blocks = 2.0m). Automatically stops safely before colliding with obstacles.",
            "parameters": {
                "type": "object",
                "properties": {
                    "distance": {"type": "number", "description": "Exact distance to travel in metres (e.g. 0.4, 0.8, 1.5)"},
                    "observations": {"type": "string", "description": "Visual scene analysis and obstacle locations"},
                    "target": {"type": "string", "description": "Target entity being approached or avoided"},
                    "confidence": {"type": "number", "description": "Confidence from 0.0 to 1.0"}
                },
                "required": ["distance", "observations", "target", "confidence"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "turn",
            "description": "Rotate robot in place by exact degrees. Positive = clockwise (turn right), negative = counter-clockwise (turn left). Camera FOV is 90°, so ~7 pixels per degree horizontally.",
            "parameters": {
                "type": "object",
                "properties": {
                    "degrees": {"type": "number", "description": "Exact degrees to turn (e.g. -25 for left, +30 for right, 180 for reverse)"},
                    "observations": {"type": "string", "description": "Visual scene analysis"},
                    "target": {"type": "string", "description": "Target heading or object"},
                    "confidence": {"type": "number", "description": "Confidence from 0.0 to 1.0"}
                },
                "required": ["degrees", "observations", "target", "confidence"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_depth",
            "description": "Measures center horizontal laser ray distance straight ahead up to 10 metres. Always use this to verify distance before pickup or moving.",
            "parameters": {
                "type": "object",
                "properties": {
                    "observations": {"type": "string", "description": "Reason for depth check"},
                    "target": {"type": "string", "description": "Target object"},
                    "confidence": {"type": "number", "description": "Confidence from 0.0 to 1.0"}
                },
                "required": ["observations", "target", "confidence"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "pickup",
            "description": "Pick up the object directly in front of the robot. IMPORTANT: Only succeeds when distance to target is <= 0.55m.",
            "parameters": {
                "type": "object",
                "properties": {
                    "observations": {"type": "string", "description": "Confirmed target within 0.55m grasp distance"},
                    "target": {"type": "string", "description": "Target object being grasped"},
                    "confidence": {"type": "number", "description": "Confidence from 0.0 to 1.0"}
                },
                "required": ["observations", "target", "confidence"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "drop",
            "description": "Release the held object onto the floor. Target should be the green square drop zone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "observations": {"type": "string", "description": "Visual confirmation of green drop zone"},
                    "target": {"type": "string", "description": "Green drop zone"},
                    "confidence": {"type": "number", "description": "Confidence from 0.0 to 1.0"}
                },
                "required": ["observations", "target", "confidence"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Declare mission completion once the requested object has been successfully deposited in the green zone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "observations": {"type": "string", "description": "Summary of mission success"},
                    "target": {"type": "string", "description": "Mission completion"},
                    "confidence": {"type": "number", "description": "1.0"}
                },
                "required": ["observations", "target", "confidence"]
            }
        }
    }
]

SYSTEM_PROMPT = """You are the autonomous navigation controller of a physical mobile robot in a 6.0m x 6.0m arena.
Your physical specifications:
- Forward Camera: 90° horizontal FOV, 640x480 resolution. Center reticle is at x=320, y=240.
  Rotation scale: ~7 pixels horizontally per 1° degree of rotation.
  Clockwise (right) is positive degrees (+), counter-clockwise (left) is negative degrees (-).
- Laser Rangefinder: Measures exact line-of-sight distance straight ahead at center reticle.
- Floor Grid: Floor tiles are 0.5m x 0.5m squares ("blocks"). 2 blocks = 1.0m, 4 blocks = 2.0m.
- Gripper: Effective grasp reach is 0.55m. Attempting pickup when distance > 0.55m will FAIL.

Navigation & Obstacle Avoidance Protocol:
1. Scan & Locate: Look at the 3D first-person image. Note where the target object is.
2. Occlusion Bypass: If an obstacle (e.g. orange traffic cone) sits directly in front of your path to the target, DO NOT ram it. Steer slightly sideways (e.g. turn -30° or +30°), drive forward 0.6m, then turn back toward the target to bypass it with clear line of sight.
3. Approach: Use laser depth to gauge distance. Advance smoothly until depth reading is between 0.40m and 0.50m.
4. Grasp: Call pickup() when depth is <= 0.55m and target is directly centered.
5. Deposit: Once holding the object, rotate until the flat glowing green square drop zone on the floor is centered, advance onto it, call drop(), and then done()."""

ACTIONS = {
    'move': lambda e, a: e.move(float(a.get('distance', a.get('amount', 0.5)))),
    'turn': lambda e, a: e.turn(float(a.get('degrees', a.get('angle', 45)))),
    'pickup': lambda e, a: e.pickup(),
    'drop': lambda e, a: e.drop(),
    'read_depth': lambda e, a: e.depth(),
}

def prune_older_images(messages):
    """Retain only the most recent image in messages to avoid token blowup and multi-second latency."""
    seen = False
    for msg in reversed(messages):
        content = msg.get('content')
        if isinstance(content, list):
            for i in range(len(content) - 1, -1, -1):
                item = content[i]
                if isinstance(item, dict) and item.get('type') == 'image_url':
                    if seen:
                        content[i] = {'type': 'text', 'text': '[earlier frame omitted]'}
                    seen = True

def run_antigravity_episode(command: str, base_url: str = DEFAULT_BASE_URL, api_key: str = DEFAULT_API_KEY, model: str = DEFAULT_MODEL, max_steps: int = 30):
    engine = EngineClient('http://localhost:8080')
    admin = AdminClient('http://localhost:8081')
    logger = RunLogger()

    print(f"\n==================================================")
    print(f"⚡ [Antigravity / Easy CLI Agent] Starting Vision Run")
    print(f"   Proxy:   {base_url}")
    print(f"   Model:   {model}")
    print(f"   Command: \"{command}\"")
    print(f"==================================================")

    # Reset scene & set command
    admin.reset()
    admin.set_command(command)

    frame = engine.look()
    logger.save_frame(0, frame)

    chat_url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"Mission: \"{command}\". Here is your starting onboard camera view:"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{base64.b64encode(frame).decode('utf-8')}",
                        "detail": "low"
                    }
                }
            ]
        }
    ]

    for step in range(1, max_steps + 1):
        prune_older_images(messages)
        depth_reading = engine.depth()
        state = engine.state()
        print(f"\n--- [Step {step}/{max_steps}] Pose: ({state['x']:.2f}, {state['y']:.2f}, {state['heading']:.1f}°), Laser Depth: {depth_reading}m, Holding: {state.get('holding')} ---")

        req_body = {
            "model": model,
            "messages": messages,
            "tools": TOOLS_SCHEMA,
            "tool_choice": "auto",
            "temperature": 0.1,
            "max_tokens": 1000
        }

        try:
            resp = requests.post(chat_url, headers=headers, json=req_body, timeout=45)
        except Exception as e:
            print(f"❌ Connection error to Easy CLI Proxy: {e}")
            break

        if resp.status_code != 200:
            print(f"❌ Proxy error ({resp.status_code}): {resp.text}")
            break

        res_json = resp.json()
        choice = res_json['choices'][0]
        msg = choice['message']
        messages.append(msg)

        tool_calls = msg.get('tool_calls', [])
        if not tool_calls:
            # Fallback if model answered in text
            print(f"🤖 Model thought: {msg.get('content')}")
            # Try parsing JSON from content
            content = msg.get('content', '')
            import re
            m = re.search(r'\{[\s\S]*\}', content)
            if m:
                try:
                    p = json.loads(m.group(0))
                    tool_calls = [{'id': f'call_{step}', 'type': 'function', 'function': {'name': p.get('tool', 'read_depth'), 'arguments': json.dumps(p.get('args', {}))}}]
                except Exception:
                    pass

        if not tool_calls:
            print("⚠️ No tool call returned. Asking model to choose an action...")
            messages.append({"role": "user", "content": "Please invoke a tool function (turn, move, read_depth, pickup, drop, done) to take action."})
            continue

        for call in tool_calls:
            call_id = call.get('id', f'call_{step}')
            func = call['function']
            func_name = func['name']
            try:
                args = json.loads(func['arguments']) if isinstance(func['arguments'], str) else func['arguments']
            except Exception:
                args = {}

            print(f"⚡ [Action]: {func_name}({args})")
            if 'observations' in args:
                print(f"   👀 Observation: {args['observations']}")

            if func_name == 'done':
                print(f"\n🎉 [Done] Mission marked complete by agent!")
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": func_name,
                    "content": json.dumps({"status": "completed"})
                })
                # Check ground-truth grading
                verdict = admin.grade()
                print(f"🏆 Grader Result: {verdict.get('result', 'UNKNOWN')} ({verdict})")
                return verdict

            fn = ACTIONS.get(func_name)
            if not fn:
                print(f"⚠️ Unknown action {func_name}")
                continue

            action_result = fn(engine, args)
            print(f"   ➡️ Result: {action_result}")

            # Capture fresh frame if moved or turned
            if func_name in ('move', 'turn'):
                frame = engine.look()
                logger.save_frame(step, frame)

            post_depth = engine.depth()
            post_state = engine.state()

            tool_response_content = {
                "result": action_result,
                "current_laser_depth": post_depth,
                "holding_object": post_state.get('holding', False)
            }

            if func_name == 'pickup' and not action_result.get('ok', False):
                tool_response_content["hint"] = f"Pickup failed. Center depth is {post_depth}m. You must be <= 0.55m from the object to grasp it."

            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "name": func_name,
                "content": json.dumps(tool_response_content)
            })

            # Add updated camera view after physical motion
            if func_name in ('move', 'turn'):
                messages.append({
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"Camera view after {func_name}: (Center depth: {post_depth}m, holding: {post_state.get('holding')})"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64.b64encode(frame).decode('utf-8')}",
                                "detail": "low"
                            }
                        }
                    ]
                })

    print("\n⏱️ Reached max steps.")
    verdict = admin.grade()
    print(f"🏆 Final Grader Result: {verdict}")
    return verdict

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Run autonomous physical fetch agent with Antigravity / Easy CLI proxy")
    parser.add_argument("--command", default="Bring the round red thing", help="Natural language mission")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Proxy base URL (default: http://127.0.0.1:8317/v1)")
    parser.add_argument("--api-key", default=DEFAULT_API_KEY, help="Proxy API key (default: 12345)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Model ID (e.g. claude-sonnet-4-6, claude-opus-4-6-thinking, gemini-3.8-flash-high)")
    parser.add_argument("--steps", type=int, default=25, help="Max steps per episode")

    args = parser.parse_args()
    run_antigravity_episode(args.command, base_url=args.base_url, api_key=args.api_key, model=args.model, max_steps=args.steps)

