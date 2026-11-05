# Path planning: A* and Dijkstra on the robot's own map

Code: `agent/planner.py` (search, footprint, smoothing), `agent/navigate.py` (execution and replanning), `agent/mapping.py` (`_nbv_lattice`: Dijkstra for viewpoint costs). Tests: `tests/test_planner.py`. Benchmark: `tools/eval_navigation.py` -> `outputs/eval/navigation.md`.

## Graph

- State: (cell, heading bin). Cells are 0.1 m on the robot's own occupancy map (built from `/scan`); 8 headings of 45 degrees.
- Edges: drive one cell forward or backward in the current heading; turn 45 degrees in place.
- Cost of an edge, in seconds:
  - drive: `length / 1 m/s x (1 + risk x r(cell))`, backward x `reverse` (1.5). `r` is 0 at `comfort` (0.25 m) of spare clearance and 1 at the hard limit.
  - turn: `0.5 s` (45 degrees at 90 deg/s) `+ turn_fixed` (0.25 s stop-start overhead).
- All costs are non-negative.

Weights are in `planner.WEIGHTS`. They were chosen from the sweep in `outputs/eval/navigation.md`: `risk = 2` let the body pass 4 mm from a box; `risk = 6` keeps at least 0.11 m (body) and 0.106 m (hand) for about 5% more time. `turn_fixed` between 0 and 1 changed little on these levels.

## Which algorithm where

| Question | Algorithm | Why |
|---|---|---|
| Route to one known goal (fetch, deliver, orbit start, chosen viewpoint) | A* | goal-directed; expanded 2.2x fewer states and planned 1.8x faster than Dijkstra in the benchmark, same cost |
| Time to reach every candidate viewpoint | Dijkstra (one run, all states) | next-best-view scores about 700 viewpoint x heading candidates; one search gives all their costs, turns included |
| Distance map for the dashboard | Dijkstra | the same run, shown as "time-to-reach" |

## What is guaranteed, and under which assumptions

- Dijkstra returns the minimum-cost path on this graph (non-negative costs). Checked against Bellman-Ford on 200 states.
- A* uses `h = max(0, straight-line distance - goal radius) / 1 m/s`. Every metre of progress costs at least 1 s and turns cost at least 0, so `h` never overestimates; with re-expansion on improvement A* returns the same optimal cost as Dijkstra. Checked on 25 random start/goal pairs (equal cost, A* never expands more).
- Diagonal steps cost 0.1414 s per cell, straight steps 0.1 s (checked).
- "Optimal" means optimal for this cost on this lattice (0.1 m, 45 degrees) and this map. The executed route is smoothed and curved afterwards: it is a feasible, validated improvement of the lattice optimum, not a proven continuous-space optimum. Unknown space is not driven through, so a shorter route through unseen space is never claimed.

## Footprint

- Body: circle of the robot radius. Hard limit `ROBOT_R + 0.08` m from obstacle cells (cells mark surfaces to about +-7 cm, so this keeps at least 1 cm of real gap); comfort band above it priced by `risk`.
- Hand or load: the folded hand (empty) or the carried object (holding, radius from its measured size) is checked at its real offset (front-right) for each heading, and at the half-way angle of every in-place turn.
- Diagonal moves may not cut between two blocked cells.
- Smoothing (any-angle shortcuts and arc corners) is re-validated with the same footprint along the whole segment or arc before it is used.

Result in the benchmark: zero collisions for A* and Dijkstra over all scenarios; the narrow gap (0.9 m) admits the empty robot and is correctly refused when it carries a load that cannot fit (no route claimed instead of a crash). The earlier grid planner collided 8 times and touched surfaces (0.001 m).

## From path to motion

1. Lattice path -> corner points where a straight run ends.
2. Any-angle shortcuts between corner points where the full footprint stays clear (removes 45-degree staircases; also replaces back-up-and-turn manoeuvres when a straight leg is safe).
3. Corners become `/arc` curves (radius 0.6 to 0.2 m) when the arc fits, is clear, and is not slower than stop-turn-go; otherwise an in-place turn.
4. Executed with the existing primitives (`move`, `turn`, `arc`; 1 m/s, 90 deg/s). Circling an object still uses the dedicated orbit (`/arc` round its centre); A* only brings the robot to the orbit start.

## Replanning (`navigate.py`)

Replans only when there is a reason: a move or arc collides; the robot ends a segment more than 0.15 m or 10 degrees from the predicted pose; an observation after a long segment shows the rest of the route blocked; up to 4 replans. A new instruction ends the agent process, and the engine refuses requests from the old task, so a stale route cannot continue. Each new task plans from the robot's real pose on the latest map, with the current load.

## Measured results (`outputs/eval/navigation.md`, 8 scenarios, same maps and physics)

| | grid (before) | A* | Dijkstra | A* unsmoothed |
|---|---|---|---|---|
| success | 6/8 | 7/8 | 7/8 | 7/8 |
| collisions | 8 | 0 | 0 | 0 |
| sim time (s, mean of successes) | 7.04 | 6.04 | 6.20 | 7.31 |
| turns + curves | 4.5 + 0 | 1.7 + 1.9 | 2.1 + 1.9 | 4.4 + 0 |
| planning ms / states expanded | 29 / 2966 | 23 / 4040 | 41 / 8974 | 23 / 4040 |
| closest approach body / hand (m) | 0.01 / 0.001 | 0.11 / 0.106 | 0.11 / 0.106 | 0.15 / 0.106 |

The 8th scenario (carrying the cube through the 0.9 m gap) has no valid route; A* and Dijkstra report that, grid drove into the gap and failed on collisions. Path length is slightly longer for A* (4.66 m vs 4.57 m) because the cost buys clearance and fewer turns.

Exploration with Dijkstra-priced viewpoints (`outputs/eval/exploration_lattice.md` vs `exploration.md`, same 4 scenarios): time to 90% coverage 16.9 -> 15.0 s, total 21.7 -> 17.4 s, distance 8.25 -> 6.71 m, viewpoints 12.75 -> 8.75, redundant observation 47% -> 28%, turns 18.5 -> 11.3, collisions 0 -> 0. The plain 45-degree sweep is still faster to 90% in the open room (8.9 s) but fails the obstacle level.

## Limits

- 45-degree heading bins: paths between bins come from smoothing, not from the search.
- Unknown space is treated as not drivable; in a barely explored map the planner may report no route where one exists (exploration fixes that).
- The load footprint uses a circle from the measured size (worst case for a box seen corner-on).
- Dynamic obstacles are not modelled (the scene has none); new static information triggers replanning.
