# Measured results

All numbers come from the tools named in each section; they write their tables to `outputs/eval/` (not committed). Agent: the free local planner unless stated otherwise. No Claude runs are included yet.

## Fetch: the six problem-statement commands x seeds 0, 1, 2 (`tools/eval_commands.py`)

| Runs | Passed | Mean actions | Mean sim time | Collisions (all runs) | Clips compliant |
|---|---|---|---|---|---|
| 18 | 18 | 27.6 | 20.3 s | 1 | 18 |

"Bring me something to drink" counts as passed when the can was delivered; the run itself ends as `needs_manual` and is confirmed with the console's Mark pass / Mark fail.

## Phrasings (`tools/eval_phrasing.py`, part of `tests/test_grader.py`)

70 ways of giving commands (synonyms, politeness, typos, follow-ups such as "now drop it on the green tile", "I'm thirsty", other names for the green square). **70/70 are read as the intended action, object and destination.** Each standard command the interpreter produces reads back as the same intent.

## Frame pacing (`tools/profile_engine.py`, real window, 8 s of driving per workload)

| Workload | FPS before | FPS after | Sim steps/s before -> after |
|---|---|---|---|
| moving, no recording | 57.7 | 57.4-57.7 | 56.9 -> 58.2-58.9 |
| + robot camera pulled at 2.5 Hz | 53.5 | 58.2-58.3 | 52.6 -> 58.5-58.9 |
| + mapping traffic (look + scan every 0.5 s) | 54.4 | 58.3 | 53.4 -> 58.4-58.8 |
| recording | 39.3 | 58.0-59.8 | 38.5 -> 57.9-59.1 |
| everything (console views + recording + mapping) | 24.2 | 54.2-58.8 | 23.9 -> 56.0-58.3 |

Before, each requested view was rendered twice and JPEG-encoded on the main thread (34-38 ms), and each clip frame forced an extra full render (22 ms). Because the simulation advanced one step per rendered frame, the robot moved in slow motion (0.4x real time with the console open). Now views are captured in the normal render pass and encoded on the HTTP thread, and the simulation runs as many fixed 1/60 s steps as wall time requires.

## Navigation (`tools/eval_navigation.py`, 8 scenarios)

A* on the heading lattice:
- 7/8 scenarios reached; the 8th has no valid route for the carried load, and the planner reports that
- 0 collisions, against 8 for the earlier grid planner
- 6.04 s mean sim time, against 7.04 s
- closest approach: body 0.11 m, hand 0.106 m

Dijkstra matches A* in route quality with 2.2x more states expanded. Details: `docs/NAVIGATION.md`.

## Exploration (`tools/eval_exploration.py`, 4 scenarios)

Next-best-view with Dijkstra-priced viewpoints, compared with the previous exploration:

| Measure | Before | After |
|---|---|---|
| Time to 90% coverage | 16.9 s | 15.0 s |
| Total time | 21.7 s | 17.4 s |
| Distance | 8.25 m | 6.71 m |
| Viewpoints | 12.75 | 8.75 |
| Redundant observation | 47% | 28% |
| Collisions | 0 | 0 |

The plain 45-degree sweep is faster to 90% in the open room but fails the obstacle level.

## Missions (`tools/eval_missions.py`, seeds 0-3)

| Mission | Passed | Notes |
|---|---|---|
| Maze Explorer | 4/4 | 0 collisions; re-enters a cell only when backing out of a dead end |
| Brain Puzzle | 4/4 | all six evaluator checks verified (ramp carried, placed, platform reached, cube carried, back on the floor, delivered) |
| Colour Sorting | 3/4 | seed 1 reaches the collision limit |

## Perception (`tools/eval_shapes.py`, 3 layouts, 12 directions x 6 distances)

Shape calls, excluding views where the object is cut by the frame:

| Object | Right |
|---|---|
| cube | 96% |
| sphere | 92% |
| cylinder | 100% |
| cone | 99% |

Most remaining errors are spheres read as cubes beyond 2 m.

## Camera height experiment (`tools/eval_camera.py`)

The PS fixes the camera at 0.5 m. A 0.28 m (chest) camera was measured as an experiment only:
- position error is slightly worse lower down (median 6.9 cm at 0.5 m, 7.6 cm at 0.28 m)
- close-range visibility is much better (objects within 0.5 m located 17% of the time at 0.5 m, 78% at 0.28 m)

The default stays 0.5 m.

## Known limits

- **Proposed obstacle level** (`levels/obstacles.json`): about 2/18 with the local planner. Its final approach still bumps walls, and half-hidden objects are misread.
- **"Put X next to Y"** passes in the open room; it fails when Y is beside a wall or seen at a steep angle.
- **Claude runs not yet measured:** the agent is wired (a fake key reaches the API and fails with 401), but no runs have used a real key.
