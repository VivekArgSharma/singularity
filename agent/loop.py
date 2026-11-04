import os
import json
import base64
import anthropic
from .engine_client import EngineClient
from .tools import SYSTEM_PROMPT, TOOLS
from .history import keep_only_latest_image, trim_history_if_long
from .logger import RunLogger
from .budget import RunCounter

DEFAULT_MODEL = os.environ.get('CLAUDE_MODEL', 'claude-3-5-sonnet-20241022')
MAX_STEPS = 60

ACTIONS = {
    'move': lambda e, a: e.move(a['distance']),
    'turn': lambda e, a: e.turn(a['degrees']),
    'pickup': lambda e, a: e.pickup(),
    'drop': lambda e, a: e.drop(),
    'read_depth': lambda e, a: e.depth(),
}

LOOK_AFTER = {'move', 'turn'}

def image_block(jpeg_bytes: bytes):
    return {
        'type': 'image',
        'source': {
            'type': 'base64',
            'media_type': 'image/jpeg',
            'data': base64.b64encode(jpeg_bytes).decode('utf-8')
        }
    }

def run_episode(command: str, model: str = DEFAULT_MODEL):
    counter = RunCounter()
    counter.check_can_run()

    client = anthropic.Anthropic()
    engine = EngineClient('http://localhost:8080')
    logger = RunLogger()

    # Initial frame
    frame = engine.look()
    logger.save_frame(0, frame)

    messages = [{
        'role': 'user',
        'content': [
            {'type': 'text', 'text': f'Command: {command}'},
            image_block(frame)
        ]
    }]

    print(f"\n[Agent] Starting run for command: '{command}' (Model: {model})")
    last_step = 0

    for step in range(1, MAX_STEPS + 1):
        last_step = step
        keep_only_latest_image(messages)
        trim_history_if_long(messages, max_steps=24)

        try:
            reply = client.messages.create(
                model=model,
                max_tokens=800,
                tools=TOOLS,
                system=[{
                    'type': 'text',
                    'text': SYSTEM_PROMPT,
                    'cache_control': {'type': 'ephemeral'}
                }],
                tool_choice={'type': 'any', 'disable_parallel_tool_use': True},
                messages=messages
            )
        except Exception as e:
            print(f"[ERROR] Claude API Error at step {step}: {e}")
            break

        call = next((b for b in reply.content if b.type == 'tool_use'), None)
        if not call:
            print(f"[WARN] Step {step}: No tool_use in Claude response. Finishing.")
            break

        messages.append({'role': 'assistant', 'content': reply.content})

        # Structured Thinking inspection
        target = call.input.get('target', 'none')
        conf = call.input.get('confidence', 0.0)
        print(f"  Step {step:02d} | Tool: {call.name:<10} | Target: '{target}' (Conf: {conf:.2f})")

        if call.name == 'done':
            logger.log_step(step, call.name, call.input, {}, None)
            print(f"[Agent] Step {step}: Claude issued 'done'.")
            break

        # Execute physical action on Engine
        action_fn = ACTIONS.get(call.name)
        if not action_fn:
            result = {'error': f'Unknown action {call.name}'}
        else:
            result = action_fn(engine, call.input)

        content = [{'type': 'text', 'text': json.dumps(result)}]
        frame_path = None

        # Look only after motion (move/turn), never after pickup or drop
        if call.name in LOOK_AFTER:
            frame = engine.look()
            frame_path = logger.save_frame(step, frame)
            content.append(image_block(frame))

        logger.log_step(step, call.name, call.input, result, frame_path)
        messages.append({
            'role': 'user',
            'content': [{
                'type': 'tool_result',
                'tool_use_id': call.id,
                'content': content
            }]
        })

    counter.record_run(last_step)
    summary = logger.summary()
    print(f"[Agent] Episode finished in {summary['steps']} steps. Logs saved to: {summary['log_file']}\n")
    return summary
