import * as THREE from 'three';

export class ViewsManager {
  constructor(scene, robot, world) {
    this.scene = scene;
    this.robot = robot;
    this.world = world;

    this.trail = [];
    this.showLabels = false;
    this.viewMode = 'pov'; // 'pov', 'orbit', 'follow'

    // Setup Hidden POV Camera & Renderer (Claude's Vision)
    this.povCamera = new THREE.PerspectiveCamera(73.74, 4 / 3, 0.05, 50);
    this.povCamera.rotation.order = 'YXZ';

    this.povCanvas = document.createElement('canvas');
    this.povCanvas.width = 640;
    this.povCanvas.height = 480;
    this.povRenderer = new THREE.WebGLRenderer({
      canvas: this.povCanvas,
      preserveDrawingBuffer: true,
      antialias: true
    });
    this.povRenderer.setSize(640, 480);
    this.povRenderer.shadowMap.enabled = true;

    // Display Camera (for human viewing)
    this.displayCamera = new THREE.PerspectiveCamera(73.74, 4 / 3, 0.05, 50);
    this.displayCamera.rotation.order = 'YXZ';

    this.displayCanvas = document.getElementById('displayCanvas');
    if (this.displayCanvas) {
      this.displayRenderer = new THREE.WebGLRenderer({
        canvas: this.displayCanvas,
        preserveDrawingBuffer: true,
        antialias: true
      });
      this.displayRenderer.setSize(this.displayCanvas.width, this.displayCanvas.height);
      this.displayRenderer.shadowMap.enabled = true;
    }

    // Top-down 2D Canvas
    this.topDownCanvas = document.getElementById('topDownCanvas');
    if (this.topDownCanvas) {
      this.topDownCtx = this.topDownCanvas.getContext('2d');
    }
  }

  placePovCamera() {
    const camPose = this.robot.getCameraWorldPose();
    this.povCamera.position.set(camPose.x, camPose.height, camPose.y);

    // Coordinate contract:
    // Heading 0 = +x, positive = clockwise.
    // Three.js: -Z is forward by default. Yaw = -(heading + 90) deg, pitch = -10 deg
    const yaw = -((camPose.heading + 90) * Math.PI) / 180;
    const pitch = (camPose.pitchDeg * Math.PI) / 180;
    this.povCamera.rotation.set(pitch, yaw, 0, 'YXZ');
  }

  renderPovJpeg() {
    this.placePovCamera();
    this.povRenderer.render(this.scene, this.povCamera);
    return this.povCanvas.toDataURL('image/jpeg', 0.90);
  }

  updateDisplayCamera() {
    if (this.viewMode === 'pov') {
      this.placePovCamera();
      this.displayCamera.copy(this.povCamera);
    } else if (this.viewMode === 'follow') {
      const rad = (this.robot.pose.heading * Math.PI) / 180;
      const behindDist = 1.0;
      const camX = this.robot.pose.x - Math.cos(rad) * behindDist;
      const camY = this.robot.pose.y - Math.sin(rad) * behindDist;
      this.displayCamera.position.set(camX, 1.2, camY);
      this.displayCamera.lookAt(this.robot.pose.x, 0.3, this.robot.pose.y);
    } else if (this.viewMode === 'orbit') {
      this.displayCamera.position.set(0, 5.5, 4.0);
      this.displayCamera.lookAt(0, 0, 0);
    }
  }

  renderDisplay() {
    if (!this.displayRenderer) return;
    this.updateDisplayCamera();
    this.displayRenderer.render(this.scene, this.displayCamera);
  }

  // Draw 2D Pixel-art / Cyber Radar Top-Down View
  drawTopDown() {
    if (!this.topDownCtx) return;
    const ctx = this.topDownCtx;
    const w = this.topDownCanvas.width;
    const h = this.topDownCanvas.height;

    // Background Room
    ctx.fillStyle = '#0f172a'; // dark slate
    ctx.fillRect(0, 0, w, h);

    const roomSize = this.world.config.room.size; // 6.0m
    const margin = 24;
    const mapArea = Math.min(w, h) - margin * 2;
    const scale = mapArea / roomSize; // px per meter

    const worldToCanvas = (wx, wy) => {
      // wx in [-3, 3] -> [0, mapArea]
      const cx = margin + (wx + roomSize / 2) * scale;
      const cy = margin + (wy + roomSize / 2) * scale;
      return [cx, cy];
    };

    // 1. Tan Floor Grid
    const [minCx, minCy] = worldToCanvas(-roomSize / 2, -roomSize / 2);
    ctx.fillStyle = '#1e293b';
    ctx.fillRect(minCx, minCy, mapArea, mapArea);

    // Subtle Grid lines
    ctx.strokeStyle = '#334155';
    ctx.lineWidth = 1;
    for (let i = -3; i <= 3; i++) {
      const [gx] = worldToCanvas(i, 0);
      ctx.beginPath();
      ctx.moveTo(gx, minCy);
      ctx.lineTo(gx, minCy + mapArea);
      ctx.stroke();

      const [, gy] = worldToCanvas(0, i);
      ctx.beginPath();
      ctx.moveTo(minCx, gy);
      ctx.lineTo(minCx + mapArea, gy);
      ctx.stroke();
    }

    // Wooden Border Walls
    ctx.strokeStyle = '#92400e';
    ctx.lineWidth = 4;
    ctx.strokeRect(minCx, minCy, mapArea, mapArea);

    // 2. Green Drop Zone
    const [zx, zy] = this.world.zone.center;
    const zs = this.world.zone.size;
    const [zoneCx, zoneCy] = worldToCanvas(zx - zs / 2, zy - zs / 2);
    ctx.fillStyle = 'rgba(16, 185, 129, 0.4)';
    ctx.fillRect(zoneCx, zoneCy, zs * scale, zs * scale);
    ctx.strokeStyle = '#10b981';
    ctx.lineWidth = 2;
    ctx.strokeRect(zoneCx, zoneCy, zs * scale, zs * scale);

    // Drop zone label
    ctx.fillStyle = '#34d399';
    ctx.font = '10px monospace';
    ctx.fillText('DROP ZONE', zoneCx + 4, zoneCy + 14);

    // 3. Path Trail
    this.trail.push([this.robot.pose.x, this.robot.pose.y]);
    if (this.trail.length > 500) this.trail.shift();

    if (this.trail.length > 1) {
      ctx.strokeStyle = 'rgba(56, 189, 248, 0.5)';
      ctx.lineWidth = 2;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      const [sx, sy] = worldToCanvas(this.trail[0][0], this.trail[0][1]);
      ctx.moveTo(sx, sy);
      for (let i = 1; i < this.trail.length; i++) {
        const [tx, ty] = worldToCanvas(this.trail[i][0], this.trail[i][1]);
        ctx.lineTo(tx, ty);
      }
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // 4. Draw Objects
    for (const obj of this.world.objects) {
      const [ox, oy] = obj.currentPos;
      const [ocx, ocy] = worldToCanvas(ox, oy);

      ctx.fillStyle = obj.hex;
      ctx.strokeStyle = '#ffffff';
      ctx.lineWidth = 1.5;

      if (obj.shape === 'cube') {
        const halfPx = (obj.size / 2) * scale;
        ctx.fillRect(ocx - halfPx, ocy - halfPx, obj.size * scale, obj.size * scale);
        ctx.strokeRect(ocx - halfPx, ocy - halfPx, obj.size * scale, obj.size * scale);
      } else {
        // circle footprint for sphere, cylinder, cone
        const rPx = (obj.size / 2) * scale;
        ctx.beginPath();
        ctx.arc(ocx, ocy, rPx, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
      }

      if (this.showLabels) {
        ctx.fillStyle = '#f8fafc';
        ctx.font = 'bold 9px sans-serif';
        ctx.fillText(`${obj.color} ${obj.shape}`, ocx - 18, ocy - 10);
      }
    }

    // 5. Robot & FOV Wedge
    const [rcx, rcy] = worldToCanvas(this.robot.pose.x, this.robot.pose.y);
    const rRadiusPx = this.robot.radius * scale;
    const rad = (this.robot.pose.heading * Math.PI) / 180;

    // Concentric Range Rings (1m, 2m, 3m) from Robot Center
    ctx.strokeStyle = 'rgba(56, 189, 248, 0.18)';
    ctx.lineWidth = 1;
    [1.0, 2.0, 3.0].forEach(r => {
      ctx.beginPath();
      ctx.arc(rcx, rcy, r * scale, 0, Math.PI * 2);
      ctx.stroke();
      ctx.fillStyle = 'rgba(56, 189, 248, 0.45)';
      ctx.font = '8px monospace';
      ctx.fillText(`${r}m`, rcx + r * scale + 2, rcy + 3);
    });

    // Full Arena FOV Vision Cone (90 degrees = +/- 45 deg, sees all the way to the far walls)
    const fovAngle = (45 * Math.PI) / 180;
    const fovDistPx = roomSize * scale * 1.2;

    const fovGrad = ctx.createRadialGradient(rcx, rcy, 10, rcx, rcy, fovDistPx);
    fovGrad.addColorStop(0, 'rgba(6, 182, 212, 0.25)');
    fovGrad.addColorStop(0.7, 'rgba(6, 182, 212, 0.08)');
    fovGrad.addColorStop(1, 'rgba(6, 182, 212, 0.01)');

    ctx.fillStyle = fovGrad;
    ctx.strokeStyle = 'rgba(6, 182, 212, 0.35)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(rcx, rcy);
    ctx.arc(rcx, rcy, fovDistPx, rad - fovAngle, rad + fovAngle);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();

    // Robot Body
    ctx.fillStyle = '#0284c7';
    ctx.strokeStyle = '#38bdf8';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(rcx, rcy, rRadiusPx, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();

    // Heading Arrow
    const headX = rcx + Math.cos(rad) * (rRadiusPx + 8);
    const headY = rcy + Math.sin(rad) * (rRadiusPx + 8);
    ctx.strokeStyle = '#f43f5e';
    ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.moveTo(rcx, rcy);
    ctx.lineTo(headX, headY);
    ctx.stroke();

    // Robot center dot
    ctx.fillStyle = '#ffffff';
    ctx.beginPath();
    ctx.arc(rcx, rcy, 2.5, 0, Math.PI * 2);
    ctx.fill();
  }

  updateHud(lastTool = null, lastArgs = null) {
    const el = (id) => document.getElementById(id);
    if (!el('hudState')) return;

    el('hudState').textContent = this.robot.stateName;
    el('hudState').className = `badge state-${this.robot.stateName.toLowerCase().replace(/\s+/g, '-')}`;

    el('hudPose').textContent = `X: ${this.robot.pose.x.toFixed(2)}m  Y: ${this.robot.pose.y.toFixed(2)}m  H: ${this.robot.pose.heading.toFixed(1)}°`;
    el('hudHolding').textContent = this.robot.holding ? 'YES (Carrying Object)' : 'NO (Empty)';
    el('hudHolding').style.color = this.robot.holding ? '#34d399' : '#94a3b8';

    el('hudRetries').textContent = `${this.robot.retries} / 3`;
    el('hudSteps').textContent = this.robot.stats.steps;
    el('hudCollisions').textContent = this.robot.stats.collisions;
    el('hudPath').textContent = `${this.robot.stats.pathLength.toFixed(2)} m`;
    el('hudSimSec').textContent = `${this.robot.stats.simSeconds.toFixed(1)} s`;

    if (lastTool) {
      el('hudLastAction').textContent = `${lastTool}(${lastArgs ? JSON.stringify(lastArgs) : ''})`;
    }

    // Top Telemetry Ribbon Updates
    if (el('ribbonHeading')) el('ribbonHeading').textContent = `${this.robot.pose.heading.toFixed(0)}°`;
    if (el('ribbonHolding')) {
      el('ribbonHolding').textContent = this.robot.holding ? 'CARRYING' : 'EMPTY';
      el('ribbonHolding').style.color = this.robot.holding ? '#34d399' : '#f8fafc';
    }
    if (el('ribbonCollisions')) el('ribbonCollisions').textContent = this.robot.stats.collisions;

    // Laser Center Depth
    let depthVal = this.robot.getCenterDepth();
    const depthStr = depthVal !== null ? `${depthVal.toFixed(2)}m` : 'OPEN (>10m)';
    if (el('ribbonDepth')) el('ribbonDepth').textContent = depthStr;

    // Cyberpunk POV HUD Overlay Updates
    const hudOverlay = el('povHudOverlay');
    if (hudOverlay) {
      if (this.viewMode === 'pov') {
        hudOverlay.style.display = 'flex';
        if (el('hudCompassDeg')) el('hudCompassDeg').textContent = `${this.robot.pose.heading.toFixed(0)}° ${this.getCardinal(this.robot.pose.heading)}`;
        if (el('hudReticleDist')) {
          el('hudReticleDist').textContent = `RANGE: ${depthStr}`;
          el('hudReticleDist').style.color = (depthVal !== null && depthVal <= 0.55) ? '#34d399' : '#38bdf8';
        }
        if (el('hudStatusTag')) el('hudStatusTag').textContent = `STATUS: ${this.robot.stateName.toUpperCase()}`;
      } else {
        hudOverlay.style.display = 'none';
      }
    }

    if (el('camModeTitle')) {
      const titles = {
        'pov': 'CAMERA: ROBOT POV (90° FOV)',
        'follow': 'CAMERA: THIRD-PERSON CHASE',
        'orbit': 'CAMERA: TACTICAL ORBIT DOME'
      };
      el('camModeTitle').textContent = titles[this.viewMode] || 'CAMERA: ACTIVE';
    }
  }

  getCardinal(deg) {
    const d = (deg + 360) % 360;
    if (d >= 337.5 || d < 22.5) return 'E';
    if (d >= 22.5 && d < 67.5) return 'SE';
    if (d >= 67.5 && d < 112.5) return 'S';
    if (d >= 112.5 && d < 157.5) return 'SW';
    if (d >= 157.5 && d < 202.5) return 'W';
    if (d >= 202.5 && d < 247.5) return 'NW';
    if (d >= 247.5 && d < 292.5) return 'N';
    return 'NE';
  }
}

