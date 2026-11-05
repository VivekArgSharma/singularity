"""Offline check of the Claude agent loop with a scripted stand-in for the model and
the robot (no API key, no network): tool dispatch, image only inside tool results
after moves/turns/arcs, old images trimmed, done ends the task, stale stops it.

Run:  .venv\\Scripts\\python tests\\test_claude_loop.py
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
import claude_brain  # noqa: E402
from budget import Budget, BudgetExceeded  # noqa: E402
from client import Stale  # noqa: E402

BUDGET = Path(__file__).resolve().parents[1] / "outputs" / "test_runs" / "budget_test.json"

fails = []


def check(name, cond, info=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({info})" if info and not cond else ""))
    if not cond:
        fails.append(name)


class Block(SimpleNamespace):
    def model_dump(self, exclude_none=True):
        return {k: v for k, v in vars(self).items() if v is not None}


def tool(name, **inp):
    return SimpleNamespace(stop_reason="tool_use", content=[
        Block(type="tool_use", id=f"t{len(inp)}{name}", name=name,
              input={"observations": [f"o-{name}"], "target": f"t-{name}", "confidence": 0.8, **inp})])


class FakeModel:
    def __init__(self, script):
        self.script, self.requests = list(script), []
        self.messages = self
        self.beta = self              # the agent calls client.beta.messages.create

    def create(self, **kw):
        self.requests.append(kw)
        return self.script.pop(0)


class FakeRobot:
    def __init__(self, stale_after=None):
        self.calls, self.stale_after = [], stale_after

    def _log(self, name, *a):
        self.calls.append((name, a))
        if self.stale_after is not None and len(self.calls) > self.stale_after:
            raise Stale("stale task")

    def state(self):
        return {"name": "Locate", "x": 0, "y": 0, "heading": 0, "holding": False, "retries": 0}

    def depth(self):
        return 1.0

    def look(self):
        self._log("look")
        return b"\xff\xd8fakejpeg"

    def move(self, d, speed=1.0):
        self._log("move", d, speed)
        return {"moved": d, "collided": False}

    def turn(self, d, speed=1.0):
        self._log("turn", d, speed)
        return {"heading": d}

    def arc(self, r, d, speed=1.0):
        self._log("arc", r, d, speed)
        return {"moved": 1.0, "heading": 0, "collided": False}

    def pickup(self):
        self._log("pickup")
        return {"ok": True}

    def drop(self):
        self._log("drop")
        return {"ok": True, "in_zone": True}


def make(script, robot, context=""):
    events = []
    b = claude_brain.ClaudeBrain.__new__(claude_brain.ClaudeBrain)
    b.cl, b.emit, b.command, b.context = robot, lambda k, **kw: events.append((k, kw)), "Circle the cube twice.", context
    b.client = FakeModel(script)
    b.budget = Budget(path=BUDGET, limit=1.0)
    return b, events


def main():
    script = [tool("turn", degrees=30, speed=1.0), tool("move", distance=0.5, speed=0.4),
              tool("arc", radius=0.7, degrees=720, speed=1.0), tool("pickup"), tool("turn", degrees=10, speed=1.0),
              tool("done", success=True, summary="circled twice")]
    robot = FakeRobot()
    b, events = make(script, robot, context="You are currently holding an object.")
    ok, summary = b.run()
    check("done(success) ends the task", ok is True and summary == "circled twice")
    check("tools dispatched in order with their arguments",
          [c for c in robot.calls if c[0] != "look"] == [("turn", (30, 1.0)), ("move", (0.5, 0.4)),
                                                         ("arc", (0.7, 720, 1.0)), ("pickup", ()),
                                                         ("turn", (10, 1.0))], robot.calls)
    req = b.client.requests[-1]
    check("one tool per step (parallel tool use off)", req["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True})
    check("trimmed images do not break the request: drop_block with its beta header",
          req["thinking"] == {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
          and "thinking-binding-controls-2026-08-01" in req["betas"], (req.get("thinking"), req.get("betas")))
    import re
    text = (req["system"][0]["text"] + " " + json.dumps(claude_brain.TOOLS)).lower()
    leak = re.findall(r"\b(red|blue|yellow|orange|green|grey|cube|sphere|cylinder|cone|can|zone|square|center|"
                      r"corner|room|wall|six|6 objects|objects)\b", text)
    check("system prompt has no object or room data", not leak, leak)
    first = req["messages"][0]["content"][0]["text"]
    check("operator context passed to the model", "holding an object" in first, first)
    def has_img(blocks):
        return any(isinstance(x, dict) and x.get("type") == "image" for x in blocks)
    fresh = {n: has_img(b._execute(n, a)) for n, a in (("move", {"distance": 0.1, "speed": 1.0}),
             ("turn", {"degrees": 5, "speed": 1.0}), ("arc", {"radius": 0.5, "degrees": 10, "speed": 1.0}),
             ("pickup", {}), ("drop", {}))}
    check("fresh image after move/turn/arc, none after pickup/drop",
          fresh == {"move": True, "turn": True, "arc": True, "pickup": False, "drop": False}, fresh)
    total = sum(1 for m in req["messages"] if isinstance(m["content"], list) for blk in m["content"]
                for x in ([blk] + (blk.get("content") if isinstance(blk.get("content"), list) else []))
                if isinstance(x, dict) and x.get("type") == "image")
    check(f"only the newest {claude_brain.KEEP_IMAGES} images kept", total <= claude_brain.KEEP_IMAGES, total)
    th = [kw for k, kw in events if k == "thinking"]
    check("structured observations/target/confidence surfaced to the log",
          th and th[0]["observations"] == ["o-turn"] and th[0]["target"] == "t-turn" and th[0]["confidence"] == 0.8, th[:1])
    check("every tool requires observations, target and confidence",
          all({"observations", "target", "confidence"} <= set(t["input_schema"]["required"]) for t in claude_brain.TOOLS))
    check("system prompt is cached", req["system"][0].get("cache_control") == {"type": "ephemeral"})
    check("tools are strict", all(t["strict"] for t in claude_brain.TOOLS))

    robot = FakeRobot(stale_after=2)
    b, _ = make([tool("move", distance=1.0, speed=1.0)] * 5, robot)
    try:
        b.run()
        stopped = False
    except Stale:
        stopped = True
    check("a stale reply from the engine stops the agent at once", stopped and len(robot.calls) == 3, robot.calls)

    b, _ = make([SimpleNamespace(stop_reason="refusal", content=[])], FakeRobot())
    check("refusal ends the task as failed", b.run()[0] is False)

    BUDGET.write_text(json.dumps({"spent_usd": 5.0, "calls": 1, "runs": 1, "input_tokens": 0, "output_tokens": 0}))
    b, _ = make([tool("move", distance=1.0, speed=1.0)], FakeRobot())
    try:
        b.run()
        blocked = False
    except BudgetExceeded:
        blocked = True
    check("budget guard: no model call once the limit is spent", blocked and not b.client.requests)
    usage = SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=5000, cache_creation_input_tokens=0)
    BUDGET.unlink()
    bg = Budget(path=BUDGET, limit=1.0)
    cost = bg.add("claude-sonnet-5-5", usage)
    check("budget prices a call (1k in, 200 out, 5k cached = $0.005)", abs(cost - 0.005) < 1e-9, cost)
    BUDGET.unlink()
    print(f"\n{len(fails)} failed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
