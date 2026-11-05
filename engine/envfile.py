"""Read the project's .env file (KEY=VALUE lines) into the environment at start-up.

The file is git-ignored: the API key lives only on the machine that runs the robot. Values already set in the
real environment win over the file, so a CI job or a shell can override anything. No extra package needed.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_env(path=None):
    p = Path(path) if path else ROOT / ".env"
    loaded = []
    if not p.exists():
        return loaded
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ and v != "":
            os.environ[k] = v
            loaded.append(k)
    return loaded
