# Engine (Panda3D)

## Run

```
uv venv --python 3.12 .venv
uv pip install -r requirements.txt
.venv\Scripts\python engine\sim.py              # window + operator console at http://127.0.0.1:8081/
.venv\Scripts\python engine\sim.py --headless   # offscreen
.venv\Scripts\python tests\engine_smoke.py      # 17 API/physics/grading checks
.venv\Scripts\python tests\test_calibration.py  # 7 camera checks against the spec
.venv\Scripts\python tests\test_motion.py       # 20 motion checks: speeds, frozen clock, 409, walls, carry collisions
.venv\Scripts\python tests\test_hand.py         # 30 frame-by-frame checks: the hand never passes through objects
.venv\Scripts\python tests\test_arc.py          # 30 circle / smoothness / cancellation checks, step by step
.venv\Scripts\python tests\test_grader.py       # every grading rule, the four outcomes, manual verdict, 2 s stall rule
.venv\Scripts\python tests\test_scene.py        # scene matches the spec; carried object visible in the robot camera
.venv\Scripts\python tests\test_claude_loop.py  # Claude agent loop offline: leak words, latest image only, budget
.venv\Scripts\python tests\test_dashboard.py    # interface, port-isolation and classifier checks
.venv\Scripts\python tests\test_interrupt.py    # live pre-emption checks (engine + controller + agent processes)
.venv\Scripts\python tests\test_mapping.py      # coverage map, occlusion, next-best-view, mapping during tasks
.venv\Scripts\python tests\test_planner.py      # A* / Dijkstra optimality, footprint, smoothing on real physics, replanning
.venv\Scripts\python tests\test_missions.py     # missions: config validation, seeded scenes, ramp physics, evaluators
.venv\Scripts\python tools\eval_navigation.py   # measured comparison: grid planner vs A* vs Dijkstra (docs/NAVIGATION.md)
.venv\Scripts\python tools\eval_exploration.py  # measured comparison: sweep vs nearest-frontier vs next-best-view
.venv\Scripts\python tools\analyze_frames.py --run demo   # can a recorded run's frames become a coverage map?
.venv\Scripts\python tools\demo_fetch.py        # scripted fetch to watch in the window (not the agent)
.venv\Scripts\python tools\demo_dashboard.py    # types instructions into the dashboard: fetch replaced mid-way, orbit x2
```

The agent is chosen in `.env` (`USE_CLAUDE_API=true|false`, see the root README). Options: `--brain claude|local` (overrides `.env`, used by tests), `--level level1|level2|obstacles|narrow`, `--seed N` (0 = layout as written, N > 0 = shuffled positions), `--port 8080`, `--admin-port 8081`, `--debug-frames` (also save chase and top-down frames for every clip frame, for frame-by-frame review). The engine refuses to start if its port is already in use. On the team laptops, stop MiniTool ShadowMaker's `MTAgentService` (holds 8080) and any container publishing 8081 first.

## Agent API — 127.0.0.1:8080

| Endpoint | Body | Returns |
|---|---|---|
| `GET /state` | — | `{"name","x","y","heading","holding","retries"}` |
| `GET /look` | — | JPEG 640×480 |
| `GET /depth` | — | `{"depth": float}` or `{"depth": null}` |
| `POST /move` | `{"distance": m}` | `{"moved","collided"}` |
| `POST /turn` | `{"degrees": deg}` | `{"heading"}` |
| `POST /pickup` | — | `{"ok"}` |
| `POST /drop` | — | `{"ok","in_zone"}` |
| `POST /arc` *(extension)* | `{"radius": m, "degrees": deg}` | `{"moved","heading","collided"}` |
| `GET /scan?n=61` *(extension)* | — | `{"ranges": [m or null, ...], "fov": 90}` |

Every request may carry an `X-Task-Id` header. When it names a task that is no longer the active one, the engine answers `409 {"error": "stale task"}` and does nothing, so a late reply from a replaced agent can never act.

Extensions beyond the problem statement (confirm with the mentors before final runs; the seven endpoints above keep their exact shapes):
- `POST /arc` drives along a true circle. The centre sits `radius` m (0.1–3.0) to the right for `degrees > 0` (clockwise) or to the left for `degrees < 0` and stays fixed, so the path is x = cx + r·cos θ, y = cy + r·sin θ with the heading tangent. 1 m/s along the path, capped so the heading turns at most 90°/s; up to ±720° per call; same collision rules as `move`. Robot-relative: it reveals nothing about the world.
- Optional `"speed"` (0.1–1.0) on `/move`, `/turn`, `/arc` for "slowly" commands; never faster than the spec.
- `GET /scan` is `/depth` repeated across the camera's field of view: `n` (3–181) horizontal rays from the camera position, evenly spaced from 45° left to 45° right, distance to the first thing hit or `null` past 10 m. Distances only, no names or positions; used by the robot's own map.

Every reply passes a key whitelist (`api.py`). A second action while one is running returns 409. Non-numeric or non-finite arguments return 400.

## Operator API — 127.0.0.1:8081 (harness + dashboard only, never given to the agent)

Harness: `POST /admin/reset {level, seed}` · `POST /admin/command {text, run_id}` (Idle → Locate, starts clip recording) · `POST /admin/giveup` · `POST /admin/verdict {pass}` · `GET /admin/result` → `{outcome, sim_seconds, collisions, path_length, delivered, ...}` · `GET /admin/view/<look|chase|top|window>`

Console: `GET /` (title screen) · `GET /dashboard/console.html` (live console) · `GET /dashboard/engineering.html` (map and route numbers) · `POST /admin/instruct {text}` · `POST /admin/pause` · `POST /admin/resume` · `POST /admin/stop` · `GET /admin/status` · `GET /admin/runs` · `GET /admin/runs/<run_id>/<file>` · `POST /admin/open_folder {run_id?}` · `POST /admin/mission {mission, seed}` · `GET /admin/perf` (frame pacing; `POST {"reset": true}` starts a new window)

## Console and live task control

Type instructions in the console's command bar. Each one goes through the command interpreter (`agent/interpret.py`: typos, phrasings, intent -> one standard command, shown as "understood as") and is sorted by `controller.classify`:

| Kind | Examples | Effect |
|---|---|---|
| task | "Go and pick up the blue sphere instead." | replaces the current task at once; no "stop" needed |
| refine | "slower", "make it three times" | same goal plus the update; the old task is marked Refined |
| status | "What are you doing?", "How far have you travelled?" | answered from live state; the task keeps running |
| stop / pause / resume / reset | "stop", "wait", "continue", "reset the environment" | Stop ends the current action and waits; Pause freezes the clock without cancelling; Reset (button asks to confirm) restores the level |

How a replacement works, so an old task can never act again:
1. The controller makes the new task id active in the engine. A running `move`/`turn`/`arc` of the old task stops where it is at the next 1/60 s step. A `pickup` that has not grasped yet lifts clear and folds back empty; a `drop` that has not released keeps the object. Queued actions of the old task are refused.
2. Every agent request carries `X-Task-Id`; anything from the old task gets `409 stale task`.
3. The old agent process is terminated and anything it still prints is ignored.
4. The robot is not reset or moved: pose, heading and a held object stay as they are. The new agent starts from a fresh image and state and is told when it is holding an object from the previous instruction.

Console mode differs from a graded run in one way: a drop grades the object but does not end the task, so one instruction can include several steps. The operator, not the drop, ends a task.

Agents (`agent/`): `claude_brain.py` is the Claude vision agent (one tool per step, image inside the tool result; needs `ANTHROPIC_API_KEY`; model `claude-sonnet-5-5`). `local_brain.py` is a free offline planner for a fixed set of commands: fetch to the green square or next to another object, pick up, approach (slowly), stop in front of, move behind, circle once / twice / N times, go around an obstacle, move to the green zone, rotate N / turn around, move back N m, move closer, put down. It finds things from colour blobs in the camera image and the depth reading only (`perception.py`), never ground truth, and routes around objects it has seen.

## The robot's map (`agent/mapping.py`, `agent/explore.py`)

Built only from `/state`, `/scan` and the camera geometry; nothing is read from the level files. Each observation (every `/look` the planner takes) adds one scan. Two layers are kept separate:

| Layer | Source | Cell states (0.1 m cells) |
|---|---|---|
| occupancy | range scan | unknown · free · occupied |
| coverage | camera footprint: 0.47 m (lower image edge) to 4 m, inside the 90° field of view, cut at the first hit | times seen, last seen ("recently observed" = last 3 views), occluded (in the field of view but behind something) |

Next-best-view: candidate poses on a 0.3 m lattice of reachable free cells × 12 headings, each scored by predicted unseen floor (rays stop at known obstacles and at walls seen so far, unknown space counts as open) minus `time` × travel-and-turn seconds, minus overlap above 25% of the view, minus a clearance penalty. Weights are in `mapping.WEIGHTS`. Routes come from Dijkstra on the occupancy grid (0.48 m clearance, 0.62 m when carrying) with any-angle shortcuts. This is a one-step heuristic, not a proven optimum.

Used in tasks: `find` looks first where it saw the object before (the map persists between tasks in `outputs/runs/map_state.npz`, cleared on reset), then moves to the best unseen view instead of turning 45° at a time. Routes avoid what has been seen. New commands: "Explore the entire room", "Find the X", "Inspect the X" (four sides, facing it).

Measured (`tools/eval_exploration.py`, 3 shuffled level1 layouts + level2 with obstacles, coverage scored against the real floor on the operator side): see `outputs/eval/exploration.md`.

## Outputs

Each dashboard task is one run in `outputs/runs/<run_id>/`:
- `frames/`: 1440×1080 JPEG, 30 fps, recorded only while actions run
- `<run_id>.mp4`: command caption for the first 2 s, held to at least 10 s
- `<run_id>_clip20s.mp4`: a labelled time-lapse when the task ran longer than 20 s
- `actions.jsonl`: per action, the start/end time, task id, command, tool, args, result, pose and heading before/after, collided, interrupted, frame range, state and outcome
- `steps.jsonl`: the agent's log, one line per step (`step`, `timestamp`, `tool`, `args`, `result`, `frame_path`), with every image the agent saw as PNG in `look_frames/`
- `agent.jsonl` (what the agent said), `command.txt`, `result.json`, `meta.json`

The 640×480 robot camera image is what the agent sees; it is not the video.

## Values (tuned for this engine)

| Value | Ours |
|---|---|
| Units / heading | metres, degrees; 0 = +x, positive = clockwise from above |
| Camera | JPEG 640×480, 90° horizontal / 73.74° vertical FOV, robot front, 0.5 m high, 10° down |
| Clock | fixed 1/60 s steps, advanced only during actions; the window runs as many steps per frame as wall time needs (real time); measured about 58 fps with recording and the console open |
| Move / turn | 1 m/s (clamped ±5 m) / 90°/s (clamped ±180°) |
| Robot | circle, radius 0.2 m |
| Collision | stop, hold 0.1 s, back off 0.1 m, `collided: true`, Blocked, retries +1 |
| Retries | a 4th Blocked ends the run in `fail_collision` |
| No movement | < 1 cm progress in 2 s of sim time → Blocked |
| Pickup | nearest object with surface ≤ 0.5 m from the robot front and centre within ±30° of heading |
| Hand | folded beside the body (0.27 m right, 0.10 m ahead) when empty; counts in collisions (0.08 m circle) while moving and turning |
| Pickup motion | hand rises above the object, travels over it, lowers with claws open, closes on its sides, lifts 4 cm and brings it to the carry spot (about 1.0 s); grasped from above, so no part of the hand passes through it |
| Carry | object rides in the hand 0.62 m ahead and 0.36 m right of the robot centre (bottom-right of the view, centre kept clear); it collides with walls and objects |
| Drop | lowered onto the floor where it was carried; claws open, the hand rises clear and folds back (about 1.0 s); `in_zone` = object centre inside the zone square |
| Object yaw | an object keeps its own rotation while carried and when set down; a rotated cube's footprint rotates with it |
| Depth | horizontal ray from the camera position along the heading; ignores the carried object; max 10 m, else `null` |
| Clip | 1440×1080 JPEG frames at 30 fps, saved only while an action runs → `clips/<run_id>/` (harness) or `outputs/runs/<run_id>/` (dashboard) |
| Arc | true circle, centre fixed beside the robot, heading tangent, 1 m/s capped at 90°/s, ±720° per call |
| Cancel | a newer task stops the running action at the next 1/60 s step; pose and held object kept |

## Files

`sim.py` main loop + command queue + task ownership + deferred capture · `recorder.py` 1440x1080 clip frames + pose log · `perf.py` frame-pacing profiler · `api.py` both HTTP servers + whitelist · `controller.py` console task control, agent processes, videos · `missions.py` Brain Puzzle, Colour Sorting, Maze (scenes + private evaluators) · `envfile.py` reads `.env` · `../dashboard/` title screen, console, engineering page · `../agent/` agent processes · `robot.py` actions + state machine (incl. ramps and platforms) · `world.py` level, footprints, collision, depth, zones · `grader.py` command → correct objects, outcomes · `views.py` scene, cameras, window, HUD · `meshes.py` primitives · `levels/` scene configs (`level2` two obstacles, `obstacles` walls and pillars, `narrow` a wall with one gap).

Ground truth (object positions, colours, shapes, grades) stays inside the engine and leaves only through the operator port.
