"""Claude vision agent: one tool call per step, camera image inside each tool result.

The system prompt describes only the robot and its tools; nothing about the objects
or the room leaves the engine. The image is sent after move/turn/arc (the camera
changes only when the robot moves). Needs ANTHROPIC_API_KEY.
"""
import base64
import json
import os

import anthropic

from budget import Budget

MODEL = os.environ.get("AGENT_MODEL", "claude-sonnet-5-5")
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "60"))
KEEP_IMAGES = 1            # the problem statement: only the latest camera image is sent
EFFORT = os.environ.get("AGENT_EFFORT", "medium")   # one small action per step: medium keeps each step quick
# Replacing older images edits earlier turns, which invalidates the thinking blocks that came after them.
# drop_block tells the API to leave those blocks out instead of rejecting the request.
BETAS = ["thinking-binding-controls-2026-08-01"]
THINKING_CONFIG = {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}

SYSTEM = """You control a small wheeled robot in a simulation. You see through one camera on the robot's front (90 degree horizontal field of view, 0.5 m high, tilted 10 degrees down). You also get the robot's state and a depth reading: the distance in metres from the robot's front to the first thing straight ahead, or null.

Units: metres and degrees. Heading 0 points along +x; positive turns are clockwise seen from above (to the right). The robot is a circle of radius 0.2 m. move drives at 1 m/s, turn rotates at 90 deg/s; both take an optional speed factor between 0.1 and 1.0 for careful motion.

Picking up: pickup succeeds when the thing's surface is within 0.5 m of the robot's front and its centre within 30 degrees of the heading. Drive until depth is about 0.1-0.2 m with the object centred, then pickup. A carried object rides at the front-right of the robot and appears at the bottom-right of the image. drop sets it on the floor there.

arc(radius, degrees) drives along a true circle whose centre is radius metres to the robot's right (degrees > 0, clockwise) or left (degrees < 0); the heading stays tangent to the circle. To circle around something: get to a point at the chosen radius from its centre (object radius + 0.2 m robot + at least 0.25 m clearance), turn so the object is exactly 90 degrees to your right, then arc(radius, 360) once per loop. When carrying an object, circle anticlockwise (object on the left) so the carried object stays outside the circle.

If a move reports collided, the robot has already backed off 0.1 m. More than 3 collisions ends the run, so plan around obstacles.

Instructions may be indirect or need reasoning about what the person means; work it out from what you see.

To search: turn 45 degrees and look, up to 8 times; if nothing fits after a full turn, drive about 2 m towards open space and search again. Once a likely target is seen: turn until it is in the middle of the image, check the depth, drive part of the way, then look again. Confirm it is the right thing at close range before pickup. Stay direct once the target is confirmed.

Work step by step: look at the image, decide one action, call one tool. Every call reports what you see (`observations`: each distinct thing in view and where it is in the image), what you are going for (`target`, or "none yet") and how sure you are (`confidence`, 0 to 1). When the instruction is fully carried out, or cannot be, call done."""


THINKING = {
    "observations": {"type": "array", "items": {"type": "string"},
                     "description": "Each distinct thing in view now, and where it is in the image."},
    "target": {"type": "string", "description": "What you are going for and why, or 'none yet'."},
    "confidence": {"type": "number", "description": "0 to 1: how sure you are about the target."},
}


def _tool(name, desc, props, required):
    return {"name": name, "description": desc, "strict": True,
            "input_schema": {"type": "object", "properties": {**props, **THINKING},
                             "required": [*required, *THINKING], "additionalProperties": False}}


NUM = {"type": "number"}
TOOLS = [
    _tool("move", "Drive straight; negative distance drives backwards.",
          {"distance": NUM, "speed": NUM}, ["distance", "speed"]),
    _tool("turn", "Rotate in place; positive = clockwise (right).", {"degrees": NUM, "speed": NUM},
          ["degrees", "speed"]),
    _tool("arc", "Drive along a circle with the centre radius m to the right (degrees > 0) or left (< 0).",
          {"radius": NUM, "degrees": NUM, "speed": NUM}, ["radius", "degrees", "speed"]),
    _tool("pickup", "Grasp the object in reach in front of the robot.", {}, []),
    _tool("drop", "Set the carried object down on the floor.", {}, []),
    _tool("look", "Take a fresh camera image without moving.", {}, []),
    _tool("done", "Finish: the instruction is complete (success true) or impossible (false).",
          {"success": {"type": "boolean"}, "summary": {"type": "string"}}, ["success", "summary"]),
]


class ClaudeBrain:
    def __init__(self, cl, emit, command, context=""):
        self.cl, self.emit = cl, emit
        self.command, self.context = command, context
        self.client = anthropic.Anthropic()
        self.budget = Budget()

    def _obs(self, with_image=True):
        s = self.cl.state()
        text = {"type": "text", "text": json.dumps({"state": s, "depth": self.cl.depth()})}
        if not with_image:
            return [text]
        img = base64.standard_b64encode(self.cl.look()).decode()
        return [text, {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img}}]

    @staticmethod
    def _trim(messages):
        """Keep only the newest camera images (newest first); older ones become a short note."""
        seen = 0
        for m in reversed(messages):
            if m["role"] != "user" or not isinstance(m["content"], list):
                continue
            lists = [b["content"] for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"
                     and isinstance(b.get("content"), list)] + [m["content"]]
            for lst in lists:
                for k in range(len(lst) - 1, -1, -1):
                    if isinstance(lst[k], dict) and lst[k].get("type") == "image":
                        seen += 1
                        if seen > KEEP_IMAGES:
                            lst[k] = {"type": "text", "text": "[older camera image removed]"}

    def run(self):
        intro = f"Instruction: {self.command}"
        if self.context:
            intro += f"\nNote from the operator: {self.context}"
        messages = [{"role": "user", "content": [{"type": "text", "text": intro}, *self._obs()]}]
        nudged = False
        self.budget.start_run()
        system = [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}]
        for _ in range(MAX_STEPS):
            self._trim(messages)
            self.budget.check()
            resp = self.client.beta.messages.create(
                model=MODEL, max_tokens=8000, system=system, tools=TOOLS, messages=messages,
                betas=BETAS, thinking=THINKING_CONFIG, output_config={"effort": EFFORT},
                tool_choice={"type": "auto", "disable_parallel_tool_use": True})
            if getattr(resp, "usage", None) is not None:
                cost = self.budget.add(MODEL, resp.usage)
                self.emit("cost", usd=round(cost, 5), total=self.budget.data["spent_usd"])
            messages.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in resp.content]})
            if resp.stop_reason == "refusal":
                return False, "the model declined this instruction"
            calls = [b for b in resp.content if b.type == "tool_use"]
            if not calls:
                if nudged:
                    return False, "the model stopped without calling done"
                nudged = True
                messages.append({"role": "user", "content": "Call one tool, or done if finished."})
                continue
            call = calls[0]
            args = dict(call.input)
            seen = args.pop("observations", []) or []
            target, conf = args.pop("target", ""), args.pop("confidence", None)
            self.emit("thinking", text=f"target: {target} ({conf})" + (f" | sees: {'; '.join(seen)}" if seen else ""),
                      observations=seen, target=target, confidence=conf)
            if call.name == "done":
                return bool(args.get("success")), str(args.get("summary", ""))
            result = self._execute(call.name, args)
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": call.id, "content": result}]})
        return False, f"stopped after {MAX_STEPS} steps"

    def _execute(self, name, a):
        speed = max(0.1, min(1.0, float(a.get("speed", 1.0) or 1.0)))
        if name == "move":
            res = self.cl.move(a["distance"], speed)
        elif name == "turn":
            res = self.cl.turn(a["degrees"], speed)
        elif name == "arc":
            res = self.cl.arc(max(0.1, min(3.0, a["radius"])), a["degrees"], speed)
        elif name == "pickup":
            res = self.cl.pickup()
        elif name == "drop":
            res = self.cl.drop()
        else:                                      # look
            return self._obs()
        out = [{"type": "text", "text": json.dumps({name: res})}]
        return out + self._obs(with_image=name in ("move", "turn", "arc"))
