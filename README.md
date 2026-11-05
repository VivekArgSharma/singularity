# Fetch Robot (Membrane Robot track)

A simulated wheeled robot with one front camera fetches objects from plain-English commands. The engine is Panda3D (Python). An agent drives the robot over HTTP and sees only:
- the robot camera (640x480, 90° field of view, 0.5 m high, tilted 10° down)
- a depth reading
- the robot's own state

Three extra missions (Brain Puzzle, Colour Sorting, Maze Explorer) run on the same engine.

## Quick start (Windows)

```
uv venv --python 3.12 .venv
uv pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python tools\check_setup.py      # packages, FFmpeg, ports, render, .env
.venv\Scripts\python engine\sim.py             # window + console
```

Open http://127.0.0.1:8081/, press PLAY and type a command, e.g. "Bring the red cube to the green square."

## Claude or the free planner (`.env`)

| `.env` setting | Meaning |
|---|---|
| `USE_CLAUDE_API` | `false` = free local planner (no API calls), `true` = Claude vision agent (judged runs) |
| `ANTHROPIC_API_KEY` | the Claude key, used only when `USE_CLAUDE_API=true` |

Restart the engine after changing `.env`. The console shows which agent is active but cannot switch it. `.env` is git-ignored: never commit it, never show it in a clip.

Built-in defaults:
- model: Claude Haiku 5.5 (set `AGENT_MODEL=claude-sonnet-5-5` for Sonnet: fewer steps, about 10x the cost)
- spend guard: $35 total
- hard stop: 60 steps per run

They can be overridden as environment variables (`AGENT_MODEL`, `AGENT_BUDGET_USD`, `AGENT_MAX_STEPS`, and `AGENT_EFFORT`, default `medium`). First Claude test (a few cents on Haiku 5.5): `.venv\Scripts\python tools\claude_smoke.py`.

Ports: 8080 = agent API (camera, state, depth, scan, actions); 8081 = operator API and console (never given to the agent).

## Deploy (Render)

The repository includes a `Dockerfile` and a Render blueprint (`render.yaml`).
1. On Render: **New + → Blueprint**, then pick this repository. Render builds the image and starts the service.
2. It generates `CONSOLE_PASSWORD`; everyone opening the URL needs it (any user name).
3. Optional: add `ANTHROPIC_API_KEY` in the Render dashboard and set `USE_CLAUDE_API=true`.

The container renders on the CPU (no GPU needed), uses about 300 MB of memory, and publishes only the console. The agent API stays inside the container. Measured with Render's limits (512 MB):
- Starter plan (0.5 CPU): a 10 s fetch takes about 47 s
- free plan (0.1 CPU): it works but is very slow (about 4.5 min)

On a small server the clip is recorded at 960x720 (`FETCH_LOWMEM=1`, set in the image). `GET /healthz` is the health check.

To test the same image locally: `docker build -t fetch-robot .` then `docker run -p 7860:7860 -e CONSOLE_PASSWORD=pick-one fetch-robot`, and open http://127.0.0.1:7860/.

## What it does

- **Fetch:** the problem-statement commands, including "the round red thing", "the tallest object", "the cube that is farthest from the green square" and "something to drink". It also handles "put X next to Y". Objects that are not in the room are refused with "Not present", never substituted.
- **Command interpreter:** typos, synonyms and other phrasings ("I'm thirsty", "grab the red box and put it on the finish", "now drop it on the green tile") become one standard command. The console shows it as "understood as".
- **Live task control:** a new instruction replaces the running task at the next 1/60 s step. Status questions don't cancel it. Pause, resume, stop and reset are available.
- **Robot-owned map:**
  - layers for unknown, free, seen, occluded and occupied floor
  - next-best-view exploration
  - A* routes and Dijkstra travel times with the full footprint (body, hand, carried load), smoothed and replanned on bumps (`docs/NAVIGATION.md`)
- **Missions:** `POST /admin/mission {"mission": "brain_puzzle" | "colour_sort" | "maze", "seed": 0}`, or START in the console's Objectives.
- **Per-task outputs** in `outputs/runs/<run_id>/`:
  - the MP4 from the robot camera (1440x1080, 30 fps, command caption for the first 2 s)
  - `steps.jsonl`: one line per step (`step, timestamp, tool, args, result, frame_path`), with every image the agent saw saved as PNG in `look_frames/`
  - the action log, a pose for every frame, the result and the map

## Results

Measured with the free local planner. Details are in `docs/RESULTS.md`.

| What | Result |
|---|---|
| 6 PS commands x seeds 0-2 | 18/18 passed, 1 collision in total, all clips compliant |
| 70 phrasings | 70/70 read as intended |
| Window FPS, console + recording + mapping | 24 -> 54-59 fps; motion back to real time |
| Maze / Brain Puzzle / Colour Sorting (seeds 0-3) | 4/4, 4/4, 3/4 |

## Evaluations and tests

```
.venv\Scripts\python tools\eval_commands.py --label final                   # 6 commands x 3 seeds
.venv\Scripts\python tools\eval_commands.py --label obstacles --level obstacles
.venv\Scripts\python tools\eval_missions.py --seeds 0,1,2,3 --label final
.venv\Scripts\python tools\eval_phrasing.py                                 # 70 phrasings, no robot motion
.venv\Scripts\python tools\stretch_demo.py --repeat 2                       # explore, replace a task, orbit, tallest
.venv\Scripts\python tools\profile_engine.py --label now                    # frame pacing (opens a window)
```

Every file in `tests/` starts its own headless engine and can run while the window is open.

## Layout

| Folder | What |
|---|---|
| `engine/` | Panda3D engine: robot and physics (`robot.py`, `world.py`), rendering (`views.py`, `meshes.py`), main loop and recording (`sim.py`, `recorder.py`, `perf.py`), HTTP (`api.py`), task control (`controller.py`), grading (`grader.py`), missions (`missions.py`), levels. API and values: `engine/README.md` |
| `agent/` | Claude vision agent (`claude_brain.py`), local planner (`local_brain.py`, `perception.py`), command interpreter (`interpret.py`, `typos.py`), mission solvers (`mission_brain.py`), map and navigation (`mapping.py`, `explore.py`, `planner.py`, `navigate.py`), spend guard, HTTP client |
| `dashboard/` | title screen, live console, engineering page |
| `tools/` | evaluations, profiler, demos, clip maker, setup check |
| `tests/` | test suites |
| `docs/` | `ARCHITECTURE.md`, `NAVIGATION.md`, `RESULTS.md` |

Extensions beyond the problem statement: `/arc`, `/scan`, optional `speed`, the `X-Task-Id` header, `z` in `/state` (ramp missions only), and the missions. They are optional and need mentor approval before judged runs. The seven PS endpoints keep their exact shapes.
