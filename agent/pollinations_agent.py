import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import json
import base64
import argparse

# Try reading .env file if present
env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '.env'))
if os.path.exists(env_path):
    with open(env_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k not in os.environ and v:
                    os.environ[k] = v

import requests
from agent.engine_client import EngineClient
from agent.admin_client import AdminClient
from agent.logger import RunLogger

# Pollinations API Configuration
DEFAULT_BASE_URL = "https://gen.pollinations.ai/v1"
DEFAULT_MODEL = "openai/gpt-4o-mini" # or anthropic/claude-haiku-5.5, google/gemini-2.5-flash-lite, openai/gpt-5.4-nano

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "move",
            "description": "Drive robot forward (positive distance) or backward (negative) by any exact distance in metres. Scale: 1 floor block tile = 0.5m (e.g. 2 blocks = 1.0m, 4 blocks = 2.0m). Stops safely before hitting any obstacle and reports collision status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "distance": {"type": "number", "description": "Exact distance to travel in metres (e.g. 0.5, 1.0, 2.0)"},
                    "observations": {"type": "string", "description": "What you see in the current frame"},
                    "target": {"type": "string", "description": "Target object you are heading towards"},
                    "confidence": {"type": "number", "description": "0.0 to 1.0 confidence"}
                },
                "required": ["distance", "observations", "target", "confidence"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "turn",
            "description": "Rotate robot in place by any exact degrees. Positive = clockwise (right), negative = counter-clockwise (left). 90° FOV camera means approx 7 pixels per degree of rotation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "degrees": {"type": "number", "description": "Exact degrees to turn (e.g. 15, -25, 45, 90, 180)"},
                    "observations": {"type": "string", "description": "What you see in the current frame"},
                    "target": {"type": "string", "description": "Target object you are heading towards"},
                    "confidence": {"type": "number", "description": "0.0 to 1.0 confidence"}
                },
                "required": ["degrees", "observations", "target", "confidence"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_depth",
            "description": "Measures the center horizontal ray distance straight ahead up to 10 metres. Returns distance in metres or null if nothing is hit.",
            "parameters": {
                "type": "object",
                "properties": {
                    "observations": {"type": "string", "description": "What you see in the current frame"},
                    "target": {"type": "string", "description": "Target object"},
                    "confidence": {"type": "number", "description": "0.0 to 1.0"}
                },
                "required": ["observations", "target", "confidence"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "pickup",
            "description": "Attempt to pick up the target object within 0.5 metres straight ahead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "observations": {"type": "string", "description": "Confirmed target at close range"},
                    "target": {"type": "string", "description": "Target object being picked up"},
                    "confidence": {"type": "number", "description": "0.0 to 1.0"}
                },
                "required": ["observations", "target", "confidence"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "drop",
            "description": "Release the held object onto the floor. Returns whether it landed inside the green drop zone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "observations": {"type": "string", "description": "Confirmed inside green drop zone"},
                    "target": {"type": "string", "description": "Green drop zone"},
                    "confidence": {"type": "number", "description": "0.0 to 1.0"}
                },
                "required": ["observations", "target", "confidence"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "End the episode once the object has been successfully deposited in the green zone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "observations": {"type": "string", "description": "Summary of completed fetch mission"},
                    "target": {"type": "string", "description": "Completed target"},
                    "confidence": {"type": "number", "description": "1.0"}
                },
                "required": ["observations", "target", "confidence"]
            }
        }
    }
]

SYSTEM_PROMPT = """You control a simulated mobile robot equipped with a forward-facing camera (90° horizontal field of view, 640x480 resolution) and a center horizontal depth laser sensor.
Units: metres and degrees. Clockwise (right) is positive degrees, counter-clockwise (left) is negative degrees.
Scale: 1 floor block tile = 0.5 metres.
Available actions:
- move(distance): Drives forward by ANY exact distance in metres (e.g. 0.5 for 1 block, 1.0 for 2 blocks, 2.0 for 4 blocks). Stops safely before obstacles.
- turn(degrees): Rotates in place by ANY exact degree amount (e.g. -15° for slight left, +30° for right, 180° for full turnaround). 1° corresponds to ~7 horizontal pixels.
- read_depth(): Measures center laser distance straight ahead in metres. Center the target first to get an exact distance reading.
- pickup(): Grasp object when centered and distance <= 0.55m.
- drop(): Release object onto green drop zone.
- done(): Complete mission once dropped.

Strategy:
1. Locate target: rotate using calibrated turn degrees until the target is centered.
2. If blocked by a foreground obstacle (like an orange cone), steer sideways 35°, step forward 0.5m, and face back toward the target to bypass it.
3. Call read_depth() to measure exact distance, move forward by (depth - 0.45m), then call pickup().
4. Spot the green drop zone on the floor, navigate onto it, call drop(), then done()."""

ACTIONS = {
    'move': lambda e, a: e.move(float(a.get('distance', a.get('amount', 0.5)))),
    'turn': lambda e, a: e.turn(float(a.get('degrees', a.get('angle', 45)))),
    'pickup': lambda e, a: e.pickup(),
    'drop': lambda e, a: e.drop(),
    'read_depth': lambda e, a: e.depth(),
}

LOOK_AFTER = {'move', 'turn'}

def run_pollinations_episode(command: str, api_key: str, model: str = DEFAULT_MODEL, max_steps: int = 30):
    if not api_key:
        raise ValueError("Pollinations API Key is required! Pass --api-key or set POLLINATIONS_API_KEY environment variable.")

    engine = EngineClient('http://localhost:8080')
    admin = AdminClient('http://localhost:8081')
    logger = RunLogger()

    print(f"\n==================================================")
    print(f"🚀 [Pollinations AI] Starting vision agent")
    print(f"   Model:   {model}")
    print(f"   Command: \"{command}\"")
    print(f"==================================================")

    # Reset scene & set command in engine
    admin.reset()
    admin.set_command(command)

    frame = engine.look()
    logger.save_frame(0, frame)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"Mission: {command}. Here is your initial camera view:"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{base64.b64encode(frame).decode('utf-8')}",
                        "detail": "high"
                    }
                }
            ]
        }
    ]

    last_step = 0

    for step in range(1, max_steps + 1):
        last_step = step

        # Keep context fast: preserve initial instruction + latest image
        # Truncate older images to maintain high responsiveness
        for msg in messages[:-1]:
            if isinstance(msg.get("content"), list):
                for part in msg["content"]:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        part["image_url"]["url"] = "data:image/jpeg;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

        payload = {
            "model": model,
            "messages": messages,
            "tools": TOOLS_SCHEMA,
            "tool_choice": "auto",
            "temperature": 0.1
        }

        try:
            resp = requests.post(
                f"{DEFAULT_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                json=payload,
                timeout=60
            )
            if not resp.ok:
                print(f"❌ [Pollinations API Error]: HTTP {resp.status_code} - {resp.text}")
                break
            resp_data = resp.json()
        except Exception as e:
            print(f"❌ [Pollinations Request Error]: {e}")
            break

        choice = resp_data.get("choices", [{}])[0]
        msg = choice.get("message", {})
        messages.append(msg)

        tool_calls = msg.get("tool_calls", [])
        if not tool_calls:
            raw_content = msg.get("content", "")
            print(f"  Step {step:02d} | Model thought: {raw_content[:80] if raw_content else 'None'}")
            # Try to see if model replied in JSON
            tool_name = "turn"
            tool_args = {"degrees": 45}
            try:
                import re
                m = re.search(r'\{[\s\S]*\}', raw_content)
                if m:
                    j = json.loads(m.group(0))
                    tool_name = j.get("tool", "turn")
                    tool_args = j.get("args", {})
            except Exception:
                pass
            tool_call_id = f"call_{step}"
        else:
            tool_call = tool_calls[0]
            tool_name = tool_call["function"]["name"]
            tool_call_id = tool_call.get("id", f"call_{step}")
            tool_args = {}
            try:
                tool_args = json.loads(tool_call["function"]["arguments"])
            except Exception:
                pass

        target_str = tool_args.get("target", command)
        conf = tool_args.get("confidence", 1.0)
        obs = tool_args.get("observations", "")
        print(f"  Step {step:02d} | Tool: {tool_name:<10} | Target: '{target_str[:22]}' (Conf: {conf:.2f}) | {obs[:45]}")

        if tool_name == "done":
            logger.log_step(step, tool_name, tool_args, {}, None)
            print(f"🏁 [Step {step}] Model signaled 'done'. Mission complete!")
            break

        action_fn = ACTIONS.get(tool_name, ACTIONS['turn'])
        result = action_fn(engine, tool_args)

        frame_path = None
        new_frame_b64 = None

        if tool_name in LOOK_AFTER:
            frame = engine.look()
            frame_path = logger.save_frame(step, frame)
            new_frame_b64 = base64.b64encode(frame).decode('utf-8')

        logger.log_step(step, tool_name, tool_args, result, frame_path)

        # Standard OpenAI API: tool message content MUST be a string
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": json.dumps(result)
        })

        # Provide updated camera view and sensor state in user message
        post_depth = engine.depth().get('depth')
        post_state = engine.state()
        is_holding = post_state.get('holding', False)
        feedback = f"Result of {tool_name}: {json.dumps(result)}. Current center depth: {post_depth:.2f}m, holding: {is_holding}." if post_depth is not None else f"Result of {tool_name}: {json.dumps(result)}. Current center depth: open space, holding: {is_holding}."
        if tool_name == 'pickup' and not result.get('ok', False):
            feedback += " [ATTENTION: Pickup failed! Target must be directly ahead and within 0.55m. You are too far away! Move closer first.]"

        user_content = [{"type": "text", "text": feedback}]
        if new_frame_b64:
            user_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{new_frame_b64}",
                    "detail": "high"
                }
            })

        messages.append({
            "role": "user",
            "content": user_content
        })

    eval_result = admin.result()
    print("==================================================")
    print("🎯 EPISODE EVALUATION RESULT:")
    print(f"   Outcome:      {eval_result.get('outcome')}")
    print(f"   Steps:        {eval_result.get('steps')}")
    print(f"   Sim Time:     {eval_result.get('simSeconds')} s")
    print(f"   Collisions:   {eval_result.get('collisions')}")
    print(f"   Path Length:  {eval_result.get('pathLength')} m")
    print("==================================================")
    return eval_result

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pollinations AI Autonomous Vision Agent")
    parser.add_argument("command", nargs="?", default="Bring the round red thing", help="Command to execute")
    parser.add_argument("--api-key", default=os.environ.get("POLLINATIONS_API_KEY", ""), help="Pollinations API Key")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Vision model ID (e.g. openai/gpt-4o, google/gemini-2.5-flash)")
    args = parser.parse_args()

    run_pollinations_episode(args.command, args.api_key, model=args.model)
