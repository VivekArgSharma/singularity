# Architecture

Three processes on one machine, as the problem statement prescribes, plus an operator console that is never visible to the agent.

```
 operator ──► console (8081) ──► controller ──► agent process ──HTTP 8080──► engine (Panda3D)
                                     │              │                           │ physics, camera,
                                     │              └── Claude API (optional)   │ state machine, clip
                                     └──────────── operator API (8081) ─────────┘
```

| Process | Code | Knows |
|---|---|---|
| Engine | `engine/sim.py`, `robot.py`, `world.py`, `views.py` | the whole scene (ground truth). Serves the agent API on 8080 and the operator API on 8081. |
| Agent | `agent/run.py` + `claude_brain.py` or `local_brain.py` | only what 8080 returns: camera JPEG, depth, range scan, its own pose and holding flag, action results |
| Claude | Anthropic API | only what the agent sends: the command, the latest camera image (inside the tool result), the depth value, one text line per earlier step |

Object data stops at the engine boundary. Every agent reply passes a key whitelist (`engine/api.py`), and the agent port has no view or admin routes. `tests/test_dashboard.py` and `tests/test_claude_loop.py` check both, including that the system prompt and tool descriptions name no colour, shape, object or room.

## Engine

- Fixed 1/60 s physics steps, advanced only while an action runs (the clock is frozen while the agent thinks). In the window, as many steps run per rendered frame as wall time needs, so motion is real time at any frame rate; headless runs one step per frame.
- Robot: a 0.2 m circle with a gripper hand. Collisions stop the move, back off 0.1 m and enter Blocked; a 4th Blocked ends the run with `fail_collision`; 2 s without progress also counts as Blocked.
- Cameras render to offscreen buffers. Views requested over HTTP are captured in the normal render pass and encoded off the main thread, which keeps the window at about 58 fps with recording and the console open.
- Recording: 1440x1080 frames at 30 fps from the robot camera while actions run; `tools/make_clip.py` adds the 2 s command caption and writes the MP4.
- Grading (`grader.py`): which objects satisfy the command, then `pass`, `fail_wrong_object`, `fail_not_reached` or `fail_collision`. "Put X next to Y" passes when X ends within 0.35 m of Y.

## Agents

**Claude vision agent** (`agent/claude_brain.py`):
- one Claude call and one tool call per step; the tools map one-to-one to the endpoints
- the `/look` image goes inside the tool result; only the latest image is kept, and earlier steps are one line of text each
- the system prompt is cached
- 60-step hard stop and a spend guard (`agent/budget.py`)

**Free local planner** (`agent/local_brain.py`): the same API, no model calls. It reads commands with fixed rules and finds things from colour blobs and silhouettes in the camera image (`perception.py`), with heights estimated from the image. It is the development and test agent, and the fallback when no key is set.

**Command interpreter** (`agent/interpret.py`): typo correction and a phrasing normaliser (`agent/typos.py`) turn the operator's sentence into one standard command, which the console shows as "understood as". The local planner runs the standard command; Claude receives the operator's own words.

Which agent runs is set in `.env` (`USE_CLAUDE_API`).

## Robot map and navigation (agent side)

Built only from `/state`, `/scan` and the camera geometry (`agent/mapping.py`):
- an occupancy layer from the range scan
- a camera coverage layer: seen, recently seen, occluded

Next-best-view exploration scores candidate viewpoints by predicted unseen floor against travel time. Travel time comes from one Dijkstra pass over a heading lattice. Routes come from A* on the same lattice, with the full footprint (body, hand, carried load), smoothing, and replanning on bumps (`agent/planner.py`, `agent/navigate.py`). Details: `docs/NAVIGATION.md`.

## Missions

`engine/missions.py` builds three extra seeded scenes with private evaluators that read only simulator state:
- **Brain Puzzle:** carry a ramp to a platform, climb it, fetch the cube.
- **Colour Sorting:** five boxes to the corner squares of their own colour.
- **Maze Explorer:** find the exit tile.

The robot physics supports ramps and platforms: the wheels cannot climb a riser, but a shallow ramp can be driven. Solvers live in `agent/mission_brain.py`. Start a mission with `POST /admin/mission`. These are additions beyond the fetch task.

## Console

`dashboard/index.html` (title screen) and `dashboard/console.html` (live console):
- views: robot camera, chase view, top-down, or the robot's own map
- the command bar, logs, pause / resume / stop
- objectives (fetch commands and missions), run videos, settings

`dashboard/engineering.html` shows the map and route numbers.

## Extensions beyond the problem statement

These are optional and need mentor approval before judged runs. The seven PS endpoints keep their exact shapes.
- `POST /arc`: true circular motion
- `GET /scan`: multi-ray range scan, lidar-style
- optional `speed` on moves
- the `X-Task-Id` request header
- `z` in `/state` (ramp missions only)
- the missions
