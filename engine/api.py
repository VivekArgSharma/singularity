"""HTTP servers: agent API on :8080 and operator API on :8081.

Agent replies use exactly the problem-statement shapes; every reply passes
through a key whitelist so nothing else can leave the engine on :8080.
"""
import logging
import math
import os
import re
import threading
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.serving import make_server

AGENT_KEYS = {
    "state": ("name", "x", "y", "heading", "holding", "retries", "z"),   # z only exists in ramp missions
    "depth": ("depth",),
    "scan": ("ranges", "fov"),
    "move": ("moved", "collided"),
    "turn": ("heading",),
    "arc": ("moved", "heading", "collided"),
    "pickup": ("ok",),
    "drop": ("ok", "in_zone"),
}
RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def scrub(op, result):
    return {k: result[k] for k in AGENT_KEYS[op] if k in result}


def _number(body, key):
    v = (body or {}).get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def _speed(body):
    """Optional speed factor (0.1-1.0). Absent -> 1.0 (the spec speed); invalid -> None."""
    if "speed" not in (body or {}):
        return 1.0
    v = _number(body, "speed")
    return v if v is not None and 0 < v <= 1.0 else None


def _task_header():
    t = request.headers.get("X-Task-Id")
    return t if t and RUN_ID.match(t) else None


def make_agent_app(engine):
    """Port 8080. The only things an agent can reach. Replies keep the problem-statement
    shapes; an optional X-Task-Id header ties each request to a task (stale -> 409)."""
    app = Flask("agent_api")

    def call(op, **args):
        status, result = engine.call(op, task=_task_header(), **args)
        if status != 200:
            return jsonify({"error": result}), status
        return jsonify(scrub(op, result))

    @app.get("/state")
    def state():
        return call("state")

    @app.get("/look")
    def look():
        status, result = engine.call("look", task=_task_header())
        if status != 200:
            return jsonify({"error": result}), status
        return Response(result, mimetype="image/jpeg")

    @app.get("/depth")
    def depth():
        return call("depth")

    @app.get("/scan")
    def scan():
        """Optional extension: /depth across the field of view (n rays, left to right)."""
        try:
            n = int(request.args.get("n", 61))
        except ValueError:
            return jsonify({"error": "n must be an integer"}), 400
        return call("scan", n=n)

    @app.post("/move")
    def move():
        b = request.get_json(silent=True)
        d, v = _number(b, "distance"), _speed(b)
        if d is None or v is None:
            return jsonify({"error": "distance must be a finite number (speed optional, 0-1)"}), 400
        return call("move", distance=d, speed=v)

    @app.post("/turn")
    def turn():
        b = request.get_json(silent=True)
        d, v = _number(b, "degrees"), _speed(b)
        if d is None or v is None:
            return jsonify({"error": "degrees must be a finite number (speed optional, 0-1)"}), 400
        return call("turn", degrees=d, speed=v)

    @app.post("/arc")
    def arc():
        """Optional extension: drive along a circle of `radius` m, changing heading by
        `degrees` (clockwise > 0, centre on the right). Robot-relative; no world data."""
        b = request.get_json(silent=True)
        r, d, v = _number(b, "radius"), _number(b, "degrees"), _speed(b)
        if r is None or d is None or v is None or not 0.1 <= r <= 3.0:
            return jsonify({"error": "radius 0.1-3.0 m and degrees required (speed optional, 0-1)"}), 400
        return call("arc", radius=r, degrees=d, speed=v)

    @app.post("/pickup")
    def pickup():
        return call("pickup")

    @app.post("/drop")
    def drop():
        return call("drop")

    return app


DASHBOARD = Path(__file__).resolve().parents[1] / "dashboard"
RUN_FILES = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def make_admin_app(engine, ctl=None):
    """Port 8081: operator only (dashboard, harness). Never given to the agent.
    With CONSOLE_PASSWORD set (public deployments) every page and call needs that password (HTTP basic auth)."""
    app = Flask("operator_api")
    password = os.environ.get("CONSOLE_PASSWORD", "")

    if password:
        import hmac

        @app.before_request
        def _gate():
            if request.path == "/healthz":           # the host's health check: no data, no password
                return None
            a = request.authorization
            if not (a and a.password and hmac.compare_digest(a.password.encode(), password.encode())):
                return Response("Password required.", 401, {"WWW-Authenticate": 'Basic realm="Claude Bot console"'})

    def call(op, **args):
        status, result = engine.call(op, **args)
        return jsonify(result if status == 200 else {"error": result}), status

    @app.post("/admin/reset")
    def reset():
        b = request.get_json(silent=True) or {}
        level = str(b.get("level", "level1"))
        if not re.fullmatch(r"[A-Za-z0-9_]{1,32}", level):
            return jsonify({"error": "bad level"}), 400
        if ctl is not None:
            ctl.stop_agent("reset")
            ctl.clear_map()
        return call("reset", level=level, seed=int(b.get("seed", 0)))

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True})

    @app.post("/admin/mission")
    def mission():
        """{"mission": "brain_puzzle" | "colour_sort" | "maze", "seed": 0, "config": {...}, "start": true}
        Builds the mission scene; with start (default) the robot is given the mission instruction."""
        b = request.get_json(silent=True) or {}
        name = str(b.get("mission", ""))
        if not re.fullmatch(r"[a-z_]{1,32}", name):
            return jsonify({"error": "mission: brain_puzzle, colour_sort or maze"}), 400
        if ctl is not None:
            ctl.stop_agent("mission")
            ctl.clear_map()
        cfg = b.get("config") if isinstance(b.get("config"), dict) else None
        status, result = engine.call("mission", name=name, seed=int(b.get("seed", 0)), config=cfg)
        if status != 200:
            return jsonify({"error": result}), 400 if "ConfigError" in str(result) else status
        if ctl is not None and b.get("start", True):
            result["task"] = ctl.submit(result["instruction"])
        return jsonify(result)

    # ------------------------------------------------------------ dashboard
    @app.get("/")
    def dashboard():
        return send_from_directory(DASHBOARD, "index.html")

    @app.get("/dashboard/<path:name>")
    def dashboard_file(name):
        return send_from_directory(DASHBOARD, name)

    if ctl is not None:
        @app.get("/admin/status")
        def status():
            return jsonify(ctl.status())

        @app.post("/admin/instruct")
        def instruct():
            b = request.get_json(silent=True) or {}
            text = str(b.get("text", "")).strip()
            if not text or len(text) > 400:
                return jsonify({"error": "text (1-400 chars) required"}), 400
            return jsonify(ctl.submit(text))

        @app.post("/admin/stop")
        def stop():
            return jsonify(ctl.stop())

        @app.post("/admin/pause")
        def pause():
            return jsonify(ctl.pause())

        @app.post("/admin/resume")
        def resume():
            return jsonify(ctl.resume())

        @app.get("/admin/map")
        def map_view():
            return jsonify(ctl.map_status())

        @app.get("/admin/runs")
        def runs():
            return jsonify(ctl.list_runs())

        @app.get("/admin/runs/<run_id>/<name>")
        def run_file(run_id, name):
            if not RUN_ID.match(run_id) or not RUN_FILES.match(name):
                return jsonify({"error": "bad path"}), 400
            return send_from_directory(ctl.runs_dir / run_id, name, conditional=True)

        @app.post("/admin/open_folder")
        def open_folder():
            b = request.get_json(silent=True) or {}
            run_id = str(b.get("run_id", ""))
            d = ctl.runs_dir / run_id if run_id and RUN_ID.match(run_id) else ctl.runs_dir
            d.mkdir(parents=True, exist_ok=True)
            if hasattr(os, "startfile"):
                os.startfile(str(d))          # local operator machine only
            return jsonify({"ok": True, "path": str(d)})

    @app.post("/admin/command")
    def command():
        b = request.get_json(silent=True) or {}
        text, run_id = str(b.get("text", "")).strip(), str(b.get("run_id", ""))
        if not text or not RUN_ID.match(run_id):
            return jsonify({"error": "text and run_id ([A-Za-z0-9_-]) required"}), 400
        return call("command", text=text, run_id=run_id, keep_scene=bool(b.get("keep_scene", False)))

    @app.post("/admin/record_stop")
    def record_stop():
        return call("record_stop")

    @app.post("/admin/giveup")
    def giveup():
        return call("giveup")

    @app.post("/admin/verdict")
    def verdict():
        b = request.get_json(silent=True) or {}
        return call("verdict", passed=bool(b.get("pass", False)))

    @app.get("/admin/result")
    def result():
        return call("result")

    @app.route("/admin/perf", methods=["GET", "POST"])
    def perf():
        b = request.get_json(silent=True) or {}
        status, result = engine.call("perf", reset=bool(b.get("reset", False)))
        return jsonify(result), status

    @app.get("/admin/view/<name>")
    def view(name):
        if name not in ("look", "chase", "top", "window"):
            return jsonify({"error": "view must be look, chase, top or window"}), 400
        status, result = engine.call("view", name=name)
        if status != 200:
            return jsonify({"error": result}), status
        return Response(result, mimetype="image/jpeg")

    return app


def _port_free(port):
    """True if no other process holds the port. On Windows the HTTP server sets
    SO_REUSEADDR, which lets two engines share a port silently; this check stops that."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) != 0   # 0 = something is listening


def serve(app, port, host="127.0.0.1"):
    if not _port_free(port):
        raise SystemExit(f"port {port} is already in use (another engine, or Docker on 8081?)")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    srv = make_server(host, port, app, threaded=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv
