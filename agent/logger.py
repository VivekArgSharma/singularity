import os
import json
import io
from datetime import datetime
from PIL import Image

class RunLogger:
    def __init__(self, base_dir='logs'):
        timestamp = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
        self.run_dir = os.path.join(base_dir, timestamp)
        os.makedirs(self.run_dir, exist_ok=True)

        self.log_file = os.path.join(self.run_dir, 'run.jsonl')
        self.steps_logged = 0

    def save_frame(self, step: int, jpeg_bytes: bytes) -> str:
        """Saves JPEG frame as PNG with neutral filename step_XXX.png (no scene words)."""
        filename = f"step_{step:03d}.png"
        filepath = os.path.join(self.run_dir, filename)
        img = Image.open(io.BytesIO(jpeg_bytes))
        img.save(filepath, format='PNG')
        return filename

    def log_step(self, step: int, tool: str, tool_input: dict, result: dict, frame_path: str = None):
        """Writes one JSONL line per step."""
        self.steps_logged += 1
        now = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')

        # Separate structured Claude reasoning from raw tool arguments
        claude_thinking = {
            'observations': tool_input.get('observations', []),
            'target': tool_input.get('target', ''),
            'confidence': tool_input.get('confidence', 0.0)
        }

        # Filter out thinking fields from functional args
        clean_args = {k: v for k, v in tool_input.items() if k not in ['observations', 'target', 'confidence']}

        record = {
            'step': step,
            'timestamp': now,
            'tool': tool,
            'args': clean_args,
            'claude': claude_thinking,
            'result': result,
            'frame_path': frame_path
        }

        with open(self.log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record) + '\n')

    def summary(self):
        return {
            'run_dir': self.run_dir,
            'log_file': self.log_file,
            'steps': self.steps_logged
        }

