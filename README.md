# Membrane Autonomous Vision Fetch Robot

An autonomous simulated robot that interprets plain-language user commands, perceives its 3D environment exclusively through raw forward-facing camera pixels and a single horizontal depth ray, navigates to the target, picks it up, and delivers it to the green drop zone.

Built with **Three.js** in-browser physics engine, **Node.js relay** with a zero-leak firewall, and an image-driven **Anthropic Claude** agent.

---

## 1. Tuned System Parameters

| Parameter | Specification / Tuned Value | Design Rationale |
| :--- | :--- | :--- |
| **Room Dimensions** | $6.0\text{ m} \times 6.0\text{ m}$, Wall Height $2.5\text{ m}$ | Centered at origin $(0, 0)$. Clamping boundary at $\pm 2.8\text{ m}$. |
| **Drop Zone** | $[2.0, 1.8]$, size $1.2\text{ m} \times 1.2\text{ m}$ | Vivid emerald green (`#10b981`), flat on floor, no collision. |
| **Robot Radius** | $0.20\text{ m}$ | Circular footprint with forward camera turret. |
| **Move Speed** | $1.0\text{ m/s}$ ($1.67\text{ cm}$ per $1/60\text{ s}$ tick) | Prevents tunneling through obstacles or walls. |
| **Collision Backoff**| $0.10\text{ m}$ reverse backoff | Immediately frees robot from collision contact; enters `Blocked`. |
| **Turn Speed** | $90^\circ/\text{s}$ ($1.5^\circ$ per tick) | Smooth rotation; heading $0^\circ = +x$, clockwise positive. |
| **Pickup Reach** | $\le 0.55\text{ m}$, $\pm 30^\circ$ of heading | Generous tolerance to prevent false misses on aligned targets. |
| **Carried Position**| $0.45\text{ m}$ forward, lifted $+0.15\text{ m}$ | Visible at the bottom of the POV camera without obstructing horizon. |
| **Depth Sensor** | Horizontal ray along heading, max $10.0\text{ m}$ | Cast from camera origin; prevents ground-plane false hits from tilt. |
| **Camera Geometry**| $0.2\text{ m}$ ahead, $0.5\text{ m}$ height, pitch $-10^\circ$ | Vertical FOV $\mathbf{73.74^\circ}$ (yields exact $90^\circ$ horizontal FOV at $4:3$). |
| **POV Frame Format**| $640 \times 480$ JPEG (quality 0.90) | High visual clarity for Claude vision while minimizing bandwidth. |
| **Clip Duration** | $10\text{ s} - 20\text{ s}$ MP4 | Auto-pauses during Claude thinking; 2.0s title card overlay. |

---

## 2. Zero-Leak Firewall & Sandbox Architecture

To strictly satisfy the hackathon **Zero-Score Prevention Condition**, no ground truth (names, IDs, colors, coordinates, counts, or shapes) ever reaches the Claude agent:

1. **Port Separation**:
   - **Agent API (`:8080`)**: Exclusively handles the 7 allowed agent actions. All responses pass through `scrub(op, result)` which drops any key not on the explicit data contract whitelist.
   - **Admin API (`:8081`)**: Isolated port used only by evaluation scripts (`run.py`, `dummy_agent.py`). Never imported by `loop.py`.
2. **Binary Frame Streaming**:
   - `GET /look` streams raw binary JPEG bytes directly from WebGL canvas buffers (`image/jpeg`). No metadata, bounding boxes, or captions are attached.
3. **Prompt & Schema Sanitization**:
   - System prompts and tool definitions contain only kinematic, coordinate, and tool operational descriptions. Zero scene words or object references appear.
   - Tested automatically via `tests/test_leaks.py`.
4. **Step-Only File Naming**:
   - Camera frames are saved strictly as `step_001.png`, `step_002.png`. Never named after objects or scenes.

---

## 3. How Claude Finds and Reasons About Objects

Claude interprets the camera image using zero-shot spatial and common-sense visual reasoning:
1. **Search Phase (360° Sweep)**: The robot performs incremental clockwise turns of $\approx 60^\circ - 70^\circ$, inspecting a new camera frame after each turn.
2. **Visual Identification**:
   - *Direct commands* ("round red thing"): Claude identifies the red spherical mesh, distinguishing it from the red cube distractor by analyzing contour curvature and spherical shading.
   - *Relative commands* ("tallest object"): Claude compares relative vertical bounding heights against the horizon and floor perspective, identifying the yellow cylinder.
   - *Indirect commands* ("something to drink"): Claude recognizes the cylinder with the metallic top ring representing a soda can.
3. **Closed-Loop Alignment & Approach**:
   - Once the candidate is detected, Claude centers the object in its field of view ($\approx 50\%$ horizontal coordinate).
   - Reads the center depth ray (`read_depth()`) to determine exact distance.
   - Drives forward in calibrated increments (e.g. half the measured distance), taking fresh looks to re-center and confirm target identity before initiating `pickup()`.
4. **Delivery to Zone**:
   - With the object held in view, the robot sweeps to locate the bright emerald green floor square, aligns towards it, and approaches until inside the perimeter to execute `drop()`.

---

## 4. Quickstart Guide

### Step 1: Install Dependencies
```bash
# Node dependencies
npm install

# Python dependencies
pip install anthropic requests pillow pytest
```

### Step 2: Start the Engine Relay
```bash
npm start
```
*Relay starts on `http://localhost:8080` (Agent API) and `http://localhost:8081` (Admin API).*

### Step 3: Open the Mission Control Dashboard
Open Google Chrome and navigate to:
```
http://localhost:8080/
```
Keep this window visible. You will see:
- Real-time 3D Viewport with POV, Follow, and Room Orbit cameras.
- 2D Cyber Tactical Radar with live FOV cone and breadcrumb trail.
- Robot Telemetry Panel with animated State Machine badges and manual judge override buttons.

### Step 4: Run Automated Tests
```bash
# Verify Zero-Leak Compliance
pytest tests/test_leaks.py -v

# Verify API Endpoints Contract
pytest tests/test_endpoints.py -v

# Run Full Scripted Deterministic Test (Pass & Fail paths)
python tests/dummy_agent.py
```

### Step 5: Execute an Episode with Claude
Ensure `ANTHROPIC_API_KEY` is set in your environment:
```bash
export ANTHROPIC_API_KEY="your-api-key"
python agent/run.py "Bring the round red thing"
```
