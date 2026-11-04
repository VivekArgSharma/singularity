import * as THREE from 'three';
import { collides, rayDepth, inZone, footprintOf } from './world.js';

export class Robot {
  constructor(config, scene, world) {
    this.config = config;
    this.scene = scene;
    this.world = world;

    const start = config.robot.start || [-2.4, 0.0];
    this.pose = {
      x: start[0],
      y: start[1],
      heading: (config.robot.heading || 0.0) % 360
    };

    this.radius = config.robot.radius || 0.2;
    this.holding = null; // object reference when carrying
    this.retries = 0;
    this.stateName = 'Idle';
    this.prevState = 'Idle';

    this.stats = {
      steps: 0,
      simSeconds: 0,
      collisions: 0,
      pathLength: 0
    };

    this.commandText = '';
    this.heldOffset = 0.45; // m ahead of robot center

    // Build Robot 3D Mesh
    this.mesh = this.createRobotMesh();
    this.scene.add(this.mesh);
    this.updateMeshPose();
  }

  createRobotMesh() {
    const group = new THREE.Group();

    // Sleek cyber body: cylinder with rounded edges
    const bodyGeo = new THREE.CylinderGeometry(this.radius, this.radius, 0.22, 32);
    const bodyMat = new THREE.MeshStandardMaterial({
      color: 0x1e293b, // slate-800
      roughness: 0.3,
      metalness: 0.8
    });
    const body = new THREE.Mesh(bodyGeo, bodyMat);
    body.position.y = 0.11;
    body.castShadow = true;
    group.add(body);

    // Glowing cyan accent ring
    const ringGeo = new THREE.TorusGeometry(this.radius * 0.95, 0.015, 16, 32);
    const ringMat = new THREE.MeshBasicMaterial({ color: 0x06b6d4 }); // cyan-500
    const ring = new THREE.Mesh(ringGeo, ringMat);
    ring.rotation.x = Math.PI / 2;
    ring.position.y = 0.22;
    group.add(ring);

    // Camera turret module at front (+x in local forward)
    const camGeo = new THREE.BoxGeometry(0.08, 0.07, 0.1);
    const camMat = new THREE.MeshStandardMaterial({ color: 0x0f172a, roughness: 0.2, metalness: 0.9 });
    const camMesh = new THREE.Mesh(camGeo, camMat);
    camMesh.position.set(this.radius * 0.85, 0.25, 0);
    group.add(camMesh);

    // Glowing Camera Lens (visor)
    const lensGeo = new THREE.SphereGeometry(0.025, 16, 16);
    const lensMat = new THREE.MeshBasicMaterial({ color: 0x38bdf8 });
    const lens = new THREE.Mesh(lensGeo, lensMat);
    lens.position.set(this.radius * 0.85 + 0.04, 0.25, 0);
    group.add(lens);

    // Headlights
    const lightL = new THREE.PointLight(0xffffff, 0.5, 2.0);
    lightL.position.set(this.radius, 0.2, -0.08);
    group.add(lightL);

    const lightR = new THREE.PointLight(0xffffff, 0.5, 2.0);
    lightR.position.set(this.radius, 0.2, 0.08);
    group.add(lightR);

    return group;
  }

  updateMeshPose() {
    this.mesh.position.set(this.pose.x, 0, this.pose.y);
    // Heading 0 = +x, positive = clockwise
    // Three.js rotation: Y rotation clockwise is -angle in radians
    const rad = (this.pose.heading * Math.PI) / 180;
    this.mesh.rotation.y = -rad;

    // Update held object position if carrying
    this.updateHeldObjectPose();
  }

  updateHeldObjectPose() {
    if (!this.holding) return;
    const rad = (this.pose.heading * Math.PI) / 180;
    const hx = this.pose.x + Math.cos(rad) * this.heldOffset;
    const hy = this.pose.y + Math.sin(rad) * this.heldOffset;

    this.holding.currentPos = [hx, hy];
    this.holding.mesh.position.set(hx, 0.15 + (this.holding.height || this.holding.size) / 2, hy);
    this.holding.mesh.rotation.y = -rad;
  }

  getCameraWorldPose() {
    const rad = (this.pose.heading * Math.PI) / 180;
    const fwdOffset = 0.2; // camera 0.2m ahead
    const camX = this.pose.x + Math.cos(rad) * fwdOffset;
    const camY = this.pose.y + Math.sin(rad) * fwdOffset;
    return {
      x: camX,
      y: camY,
      height: 0.5,
      pitchDeg: -10.0,
      heading: this.pose.heading
    };
  }

  setState(newState) {
    if (this.stateName !== newState) {
      this.prevState = this.stateName;
      this.stateName = newState;
    }
  }

  setCommand(text) {
    this.commandText = text || '';
    if (this.stateName === 'Idle') {
      this.setState('Locate');
    }
  }

  reset() {
    const start = this.config.robot.start || [-2.4, 0.0];
    this.pose.x = start[0];
    this.pose.y = start[1];
    this.pose.heading = (this.config.robot.heading || 0.0) % 360;
    this.holding = null;
    this.retries = 0;
    this.stateName = 'Idle';
    this.prevState = 'Idle';
    this.stats = { steps: 0, simSeconds: 0, collisions: 0, pathLength: 0 };

    // Reset world objects to initial config
    for (const obj of this.world.objects) {
      obj.currentPos = [...obj.pos];
      obj.held = false;
      obj.mesh.position.set(obj.pos[0], (obj.height || obj.size) / 2, obj.pos[1]);
      obj.mesh.rotation.set(0, 0, 0);
      obj.mesh.visible = true;
    }

    this.updateMeshPose();
  }

  giveUp() {
    this.setState('Done');
    return { ok: true, state: 'Done' };
  }

  getState() {
    // Whitelisted keys only: name, x, y, heading, holding, retries
    return {
      name: this.stateName,
      x: Math.round(this.pose.x * 1000) / 1000,
      y: Math.round(this.pose.y * 1000) / 1000,
      heading: Math.round(this.pose.heading * 10) / 10,
      holding: Boolean(this.holding),
      retries: this.retries
    };
  }

  // Active Motion: Move
  async move(distance) {
    this.stats.steps++;
    if (this.stateName === 'Blocked') {
      this.setState(this.prevState === 'Blocked' ? 'Go to object' : this.prevState);
    } else if (this.stateName === 'Locate' && distance > 0) {
      this.setState('Go to object');
    }

    const speed = 1.0; // 1 m/s
    const dt = 1 / 60; // 60 Hz fixed sim step
    const stepDist = speed * dt; // ~0.0167 m per tick

    const totalDist = Math.abs(distance);
    const dir = Math.sign(distance) || 1;
    const rad = (this.pose.heading * Math.PI) / 180;
    const cosH = Math.cos(rad);
    const sinH = Math.sin(rad);

    let movedDist = 0;
    let collided = false;

    while (movedDist + 1e-5 < totalDist) {
      const curStep = Math.min(stepDist, totalDist - movedDist);
      const nextX = this.pose.x + cosH * curStep * dir;
      const nextY = this.pose.y + sinH * curStep * dir;

      this.stats.simSeconds += dt;

      // Test collision at proposed position
      if (collides([nextX, nextY], this.radius, this.holding, this.world)) {
        collided = true;
        this.stats.collisions++;
        this.retries++;

        // Back off 0.1m in opposite direction of travel
        const backoffDist = 0.1;
        const backX = this.pose.x - cosH * backoffDist * dir;
        const backY = this.pose.y - sinH * backoffDist * dir;

        if (!collides([backX, backY], this.radius, this.holding, this.world)) {
          this.pose.x = backX;
          this.pose.y = backY;
          this.stats.pathLength += backoffDist;
        }

        this.setState('Blocked');

        if (this.retries >= 4) {
          this.setState('Done');
        }
        break;
      }

      this.pose.x = nextX;
      this.pose.y = nextY;
      movedDist += curStep;
      this.stats.pathLength += curStep;
      this.updateMeshPose();

      // Yield frame slightly for smooth visualization if in browser animation
      await new Promise(r => setTimeout(r, 8));
    }

    this.updateMeshPose();
    return {
      moved: Math.round(movedDist * dir * 100) / 100,
      collided: collided
    };
  }

  // Active Motion: Turn
  async turn(degrees) {
    this.stats.steps++;
    if (this.stateName === 'Blocked') {
      this.setState(this.prevState === 'Blocked' ? 'Locate' : this.prevState);
    }

    const turnSpeed = 90.0; // 90 deg/s
    const dt = 1 / 60;
    const stepDeg = turnSpeed * dt; // 1.5 deg per tick

    const totalDeg = Math.abs(degrees);
    const dir = Math.sign(degrees) || 1;

    let turned = 0;

    while (turned + 1e-4 < totalDeg) {
      const curStep = Math.min(stepDeg, totalDeg - turned);
      const nextHeading = (this.pose.heading + curStep * dir + 360) % 360;

      this.stats.simSeconds += dt;

      // If holding an object, check if rotating sweeps into an obstacle
      if (this.holding) {
        const nextRad = (nextHeading * Math.PI) / 180;
        const testHx = this.pose.x + Math.cos(nextRad) * this.heldOffset;
        const testHy = this.pose.y + Math.sin(nextRad) * this.heldOffset;
        const tempPos = this.holding.currentPos;
        this.holding.currentPos = [testHx, testHy];
        const hit = collides([this.pose.x, this.pose.y], this.radius, this.holding, this.world);
        this.holding.currentPos = tempPos;

        if (hit) {
          this.stats.collisions++;
          this.retries++;
          this.setState('Blocked');
          break;
        }
      }

      this.pose.heading = nextHeading;
      turned += curStep;
      this.updateMeshPose();
      await new Promise(r => setTimeout(r, 8));
    }

    this.updateMeshPose();
    return {
      heading: Math.round(this.pose.heading * 10) / 10
    };
  }

  // Pickup
  pickup() {
    this.stats.steps++;
    if (this.holding) {
      return { ok: false };
    }

    const rad = (this.pose.heading * Math.PI) / 180;
    const fwdX = Math.cos(rad);
    const fwdY = Math.sin(rad);

    let bestObj = null;
    let bestDist = Infinity;

    for (const obj of this.world.objects) {
      if (obj.held) continue;
      const dx = obj.currentPos[0] - this.pose.x;
      const dy = obj.currentPos[1] - this.pose.y;
      const dist = Math.sqrt(dx * dx + dy * dy);

      // Spec: Reach 0.5m from robot centre
      if (dist <= 0.55) {
        // Angle check: within +/- 30 degrees of heading
        const angleToObj = Math.atan2(dy, dx);
        let diff = (angleToObj - rad) % (Math.PI * 2);
        if (diff > Math.PI) diff -= Math.PI * 2;
        if (diff < -Math.PI) diff += Math.PI * 2;
        const diffDeg = Math.abs((diff * 180) / Math.PI);

        if (diffDeg <= 30.0 && dist < bestDist) {
          bestDist = dist;
          bestObj = obj;
        }
      }
    }

    if (bestObj) {
      this.holding = bestObj;
      bestObj.held = true;
      this.setState('Carry');
      this.updateMeshPose();
      return { ok: true };
    }

    return { ok: false };
  }

  // Drop
  drop() {
    this.stats.steps++;
    if (!this.holding) {
      return { ok: false, in_zone: false };
    }

    const rad = (this.pose.heading * Math.PI) / 180;
    const dropX = this.pose.x + Math.cos(rad) * this.heldOffset;
    const dropY = this.pose.y + Math.sin(rad) * this.heldOffset;

    const droppedObj = this.holding;
    droppedObj.held = false;
    droppedObj.currentPos = [dropX, dropY];
    droppedObj.mesh.position.set(dropX, (droppedObj.height || droppedObj.size) / 2, dropY);
    this.holding = null;

    const insideZone = inZone([dropX, dropY], this.world);
    this.setState('Done');

    return {
      ok: true,
      in_zone: insideZone
    };
  }

  // Depth ray
  depth() {
    const camPose = this.getCameraWorldPose();
    const ignoreId = this.holding ? this.holding.id : null;
    const d = rayDepth([camPose.x, camPose.y], this.pose.heading, ignoreId, this.world);
    return { depth: d };
  }
}

