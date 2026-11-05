"""HTTP client for the agent API (port 8080 only). Every request carries the task id;
if the operator has given the robot a newer task the engine answers 409 'stale task'
and this client raises Stale, which ends the agent at once."""
import json
import time
from pathlib import Path
import urllib.error
import urllib.request

ENDPOINTS = {"state", "look", "depth", "scan", "move", "turn", "arc", "pickup", "drop"}


class Stale(Exception):
    pass


class Client:
    def __init__(self, url, task_id, emit, log_dir=None):
        self.url = url.rstrip("/")
        self.task_id = task_id
        self.emit = emit
        self.actions = 0
        # problem-statement log: one JSONL line per step (step, timestamp, tool, args, result, frame_path),
        # and every image the agent saw saved as PNG next to it (look_frames/step_NNNN.png)
        self.step = 0
        self.log_dir = Path(log_dir) if log_dir else None
        if self.log_dir is not None:
            (self.log_dir / "look_frames").mkdir(parents=True, exist_ok=True)

    def _log(self, tool, args, result, frame_path=None):
        self.step += 1
        if self.log_dir is None:
            return
        line = {"step": self.step, "timestamp": round(time.time(), 3), "tool": tool, "args": args or {},
                "result": result, "frame_path": frame_path}
        with open(self.log_dir / "steps.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")

    def _req(self, method, path, body=None, raw=False):
        assert path.split("?")[0].strip("/") in ENDPOINTS
        data = json.dumps(body).encode() if body is not None else None
        for _ in range(200):
            headers = {"Content-Type": "application/json"}
            if self.task_id:
                headers["X-Task-Id"] = self.task_id
            req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=900) as r:
                    payload = r.read()
                    return payload if raw else json.loads(payload)
            except urllib.error.HTTPError as e:
                msg = e.read().decode(errors="replace")
                if e.code == 409 and "stale" in msg:
                    raise Stale(msg) from None
                if e.code == 409:               # another action still finishing
                    time.sleep(0.05)
                    continue
                raise RuntimeError(f"{path}: HTTP {e.code} {msg}") from None
        raise RuntimeError(f"{path}: engine stayed busy")

    def state(self):
        res = self._req("GET", "/state")
        self._log("state", {}, res)
        return res

    def look(self):
        jpeg = self._req("GET", "/look", raw=True)
        frame = None
        if self.log_dir is not None:
            frame = f"look_frames/step_{self.step + 1:04d}.png"
            try:
                from PIL import Image
                import io
                Image.open(io.BytesIO(jpeg)).save(self.log_dir / frame)
            except Exception:  # noqa: BLE001  (a log problem must never stop the robot)
                frame = None
        self._log("look", {}, {"image": "jpeg 640x480", "bytes": len(jpeg)}, frame)
        return jpeg

    def depth(self):
        d = self._req("GET", "/depth")["depth"]
        self._log("depth", {}, {"depth": d})
        return d

    def scan(self, n=61):
        r = self._req("GET", f"/scan?n={int(n)}")["ranges"]
        self._log("scan", {"n": int(n)}, {"ranges": r})
        return r

    def _act(self, tool, path, body=None):
        self.actions += 1
        self.emit("action", tool=tool, args=body or {})
        res = self._req("POST", path, body)
        self.emit("result", tool=tool, result=res)
        self._log(tool, body or {}, res)
        return res

    def move(self, distance, speed=1.0):
        body = {"distance": round(float(distance), 3)}
        if speed < 1.0:
            body["speed"] = round(speed, 2)
        return self._act("move", "/move", body)

    def turn(self, degrees, speed=1.0):
        body = {"degrees": round(float(degrees), 2)}
        if speed < 1.0:
            body["speed"] = round(speed, 2)
        return self._act("turn", "/turn", body)

    def arc(self, radius, degrees, speed=1.0):
        body = {"radius": round(float(radius), 3), "degrees": round(float(degrees), 2)}
        if speed < 1.0:
            body["speed"] = round(speed, 2)
        return self._act("arc", "/arc", body)

    def pickup(self):
        return self._act("pickup", "/pickup")

    def drop(self):
        return self._act("drop", "/drop")
