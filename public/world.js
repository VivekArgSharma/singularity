import * as THREE from 'three';

export async function loadConfig(url = '/config/level1.json') {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Failed to load config from ${url}: ${res.statusText}`);
  return await res.json();
}

// Helper: Create soft radial contact shadow decal for ground
function createContactShadow(radius = 0.25, opacity = 0.55) {
  const shadowCanvas = document.createElement('canvas');
  shadowCanvas.width = 128;
  shadowCanvas.height = 128;
  const sctx = shadowCanvas.getContext('2d');

  const grad = sctx.createRadialGradient(64, 64, 4, 64, 64, 60);
  grad.addColorStop(0, `rgba(0, 0, 0, ${opacity})`);
  grad.addColorStop(0.5, `rgba(0, 0, 0, ${opacity * 0.45})`);
  grad.addColorStop(1, 'rgba(0, 0, 0, 0)');

  sctx.fillStyle = grad;
  sctx.fillRect(0, 0, 128, 128);

  const shadowTex = new THREE.CanvasTexture(shadowCanvas);
  const shadowGeo = new THREE.PlaneGeometry(radius * 2, radius * 2);
  const shadowMat = new THREE.MeshBasicMaterial({
    map: shadowTex,
    transparent: true,
    depthWrite: false
  });
  const shadowMesh = new THREE.Mesh(shadowGeo, shadowMat);
  shadowMesh.rotation.x = -Math.PI / 2;
  shadowMesh.position.y = 0.001; // Just above floor
  return shadowMesh;
}

export function buildWorld(config, scene) {
  const halfRoom = config.room.size / 2; // 3.0m
  const wallHeight = config.room.wall_height || 2.5;

  // 1. High-Tech Studio Floor with 0.5m grid tiles & coordinate axes
  const floorCanvas = document.createElement('canvas');
  floorCanvas.width = 1024;
  floorCanvas.height = 1024;
  const fctx = floorCanvas.getContext('2d');

  // Base floor: sleek dark tech slate
  fctx.fillStyle = '#111827'; // slate-900
  fctx.fillRect(0, 0, 1024, 1024);

  // Sub-grid lines (0.1m subtle ticks)
  fctx.strokeStyle = 'rgba(55, 65, 81, 0.4)';
  fctx.lineWidth = 1;
  const subDiv = 1024 / 60; // 60 subdivisions = 0.1m
  for (let i = 0; i <= 1024; i += subDiv) {
    fctx.beginPath();
    fctx.moveTo(i, 0); fctx.lineTo(i, 1024);
    fctx.stroke();
    fctx.beginPath();
    fctx.moveTo(0, i); fctx.lineTo(1024, i);
    fctx.stroke();
  }

  // Major 0.5m grid lines (matches physical blocks)
  fctx.strokeStyle = '#374151'; // cool gray
  fctx.lineWidth = 2.5;
  const majorDiv = 1024 / 12; // 12 subdivisions = 0.5m blocks
  for (let i = 0; i <= 1024; i += majorDiv) {
    fctx.beginPath();
    fctx.moveTo(i, 0); fctx.lineTo(i, 1024);
    fctx.stroke();
    fctx.beginPath();
    fctx.moveTo(0, i); fctx.lineTo(1024, i);
    fctx.stroke();
  }

  // Coordinate origin crosshair at center (512, 512)
  fctx.strokeStyle = '#06b6d4'; // cyan
  fctx.lineWidth = 3;
  fctx.beginPath();
  fctx.moveTo(512 - 20, 512); fctx.lineTo(512 + 20, 512);
  fctx.moveTo(512, 512 - 20); fctx.lineTo(512, 512 + 20);
  fctx.stroke();

  // Subtle perimeter boundary caution line
  fctx.strokeStyle = 'rgba(234, 179, 8, 0.5)'; // amber
  fctx.lineWidth = 4;
  fctx.strokeRect(6, 6, 1012, 1012);

  const floorTexture = new THREE.CanvasTexture(floorCanvas);
  const floorGeo = new THREE.PlaneGeometry(config.room.size, config.room.size);
  const floorMat = new THREE.MeshStandardMaterial({
    map: floorTexture,
    roughness: 0.75,
    metalness: 0.15
  });
  const floor = new THREE.Mesh(floorGeo, floorMat);
  floor.rotation.x = -Math.PI / 2;
  floor.position.set(0, 0, 0);
  floor.receiveShadow = true;
  scene.add(floor);

  // 2. Ceiling with subtle recessed lighting look
  const ceilingMat = new THREE.MeshStandardMaterial({ color: '#1e293b', roughness: 0.95 });
  const ceiling = new THREE.Mesh(floorGeo, ceilingMat);
  ceiling.rotation.x = Math.PI / 2;
  ceiling.position.set(0, wallHeight, 0);
  scene.add(ceiling);

  // 3. Four Modern Architectural Studio Walls
  const wallMat = new THREE.MeshStandardMaterial({
    color: '#334155', // slate-700
    roughness: 0.85,
    metalness: 0.1
  });
  const wallGeoZ = new THREE.BoxGeometry(0.12, wallHeight, config.room.size);
  const wallGeoX = new THREE.BoxGeometry(config.room.size, wallHeight, 0.12);

  // Baseboard trim material
  const baseboardMat = new THREE.MeshStandardMaterial({ color: '#0f172a', roughness: 0.5, metalness: 0.8 });
  const baseboardGeoZ = new THREE.BoxGeometry(0.14, 0.1, config.room.size);
  const baseboardGeoX = new THREE.BoxGeometry(config.room.size, 0.1, 0.14);

  // Left wall (-x)
  const wallLeft = new THREE.Mesh(wallGeoZ, wallMat);
  wallLeft.position.set(-halfRoom, wallHeight / 2, 0);
  scene.add(wallLeft);
  const baseLeft = new THREE.Mesh(baseboardGeoZ, baseboardMat);
  baseLeft.position.set(-halfRoom, 0.05, 0);
  scene.add(baseLeft);

  // Right wall (+x)
  const wallRight = new THREE.Mesh(wallGeoZ, wallMat);
  wallRight.position.set(halfRoom, wallHeight / 2, 0);
  scene.add(wallRight);
  const baseRight = new THREE.Mesh(baseboardGeoZ, baseboardMat);
  baseRight.position.set(halfRoom, 0.05, 0);
  scene.add(baseRight);

  // Front wall (-z)
  const wallFront = new THREE.Mesh(wallGeoX, wallMat);
  wallFront.position.set(0, wallHeight / 2, -halfRoom);
  scene.add(wallFront);
  const baseFront = new THREE.Mesh(baseboardGeoX, baseboardMat);
  baseFront.position.set(0, 0.05, -halfRoom);
  scene.add(baseFront);

  // Back wall (+z)
  const wallBack = new THREE.Mesh(wallGeoX, wallMat);
  wallBack.position.set(0, wallHeight / 2, halfRoom);
  scene.add(wallBack);
  const baseBack = new THREE.Mesh(baseboardGeoX, baseboardMat);
  baseBack.position.set(0, 0.05, halfRoom);
  scene.add(baseBack);

  // 4. Drop Zone (Holographic Green Target with Tech Corner Chevrons)
  const [zx, zy] = config.zone.center;
  const zSize = config.zone.size;

  const zoneCanvas = document.createElement('canvas');
  zoneCanvas.width = 512;
  zoneCanvas.height = 512;
  const zctx = zoneCanvas.getContext('2d');

  // Emerald translucent fill
  zctx.fillStyle = 'rgba(16, 185, 129, 0.35)';
  zctx.fillRect(0, 0, 512, 512);

  // Subtle interior grid
  zctx.strokeStyle = 'rgba(52, 211, 153, 0.5)';
  zctx.lineWidth = 3;
  for (let i = 64; i < 512; i += 64) {
    zctx.beginPath();
    zctx.moveTo(i, 16); zctx.lineTo(i, 496);
    zctx.stroke();
    zctx.beginPath();
    zctx.moveTo(16, i); zctx.lineTo(496, i);
    zctx.stroke();
  }

  // Neon perimeter border
  zctx.strokeStyle = '#10b981';
  zctx.lineWidth = 12;
  zctx.strokeRect(8, 8, 496, 496);

  // Tech Corner Brackets
  zctx.strokeStyle = '#34d399';
  zctx.lineWidth = 16;
  const cLen = 60;
  // Top-left
  zctx.beginPath(); zctx.moveTo(8, 8 + cLen); zctx.lineTo(8, 8); zctx.lineTo(8 + cLen, 8); zctx.stroke();
  // Top-right
  zctx.beginPath(); zctx.moveTo(504 - cLen, 8); zctx.lineTo(504, 8); zctx.lineTo(504, 8 + cLen); zctx.stroke();
  // Bottom-left
  zctx.beginPath(); zctx.moveTo(8, 504 - cLen); zctx.lineTo(8, 504); zctx.lineTo(8 + cLen, 504); zctx.stroke();
  // Bottom-right
  zctx.beginPath(); zctx.moveTo(504 - cLen, 504); zctx.lineTo(504, 504); zctx.lineTo(504, 504 - cLen); zctx.stroke();

  // Central Target Icon
  zctx.fillStyle = '#10b981';
  zctx.font = 'bold 28px sans-serif';
  zctx.textAlign = 'center';
  zctx.textBaseline = 'middle';
  zctx.fillText('TARGET ZONE', 256, 256);

  const zoneTex = new THREE.CanvasTexture(zoneCanvas);
  const zoneGeo = new THREE.PlaneGeometry(zSize, zSize);
  const zoneMat = new THREE.MeshStandardMaterial({
    map: zoneTex,
    transparent: true,
    roughness: 0.4,
    metalness: 0.2
  });
  const zoneMesh = new THREE.Mesh(zoneGeo, zoneMat);
  zoneMesh.rotation.x = -Math.PI / 2;
  zoneMesh.position.set(zx, 0.003, zy);
  scene.add(zoneMesh);

  // 5. Studio Lighting (Key light, soft fill light, ambient ground bounce)
  const ambient = new THREE.AmbientLight(0xf8fafc, 0.85);
  scene.add(ambient);

  const dirLight = new THREE.DirectionalLight(0xfff7ed, 0.95);
  dirLight.position.set(3, 5, 2);
  dirLight.castShadow = true;
  dirLight.shadow.mapSize.width = 2048;
  dirLight.shadow.mapSize.height = 2048;
  dirLight.shadow.bias = -0.0005;
  scene.add(dirLight);

  const coolFillLight = new THREE.DirectionalLight(0x38bdf8, 0.4);
  coolFillLight.position.set(-3, 4, -2);
  scene.add(coolFillLight);

  // 6. Spawn the 6 Distinct High-Quality Objects with Contact Shadows
  const objects = [];
  for (const objDef of config.objects) {
    let mesh;
    const color = objDef.hex;
    const mat = new THREE.MeshStandardMaterial({
      color: color,
      roughness: 0.45,
      metalness: 0.15
    });

    // Add soft contact shadow decal on floor under object
    const shadowDecal = createContactShadow(objDef.size * 0.75, 0.6);
    shadowDecal.position.set(objDef.pos[0], 0.002, objDef.pos[1]);
    scene.add(shadowDecal);

    if (objDef.shape === 'cube') {
      const geo = new THREE.BoxGeometry(objDef.size, objDef.height, objDef.size);
      mesh = new THREE.Mesh(geo, mat);
      mesh.position.set(objDef.pos[0], objDef.height / 2, objDef.pos[1]);

      // Subtle edge bevel outline
      const wireGeo = new THREE.EdgesGeometry(geo);
      const wireMat = new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.25 });
      const wire = new THREE.LineSegments(wireGeo, wireMat);
      mesh.add(wire);

    } else if (objDef.shape === 'sphere') {
      const geo = new THREE.SphereGeometry(objDef.size / 2, 64, 48);
      mesh = new THREE.Mesh(geo, mat);
      mesh.position.set(objDef.pos[0], objDef.size / 2, objDef.pos[1]);

    } else if (objDef.shape === 'cylinder') {
      // Drink Can design
      const geo = new THREE.CylinderGeometry(objDef.size / 2, objDef.size / 2, objDef.height, 48);
      mesh = new THREE.Mesh(geo, mat);
      mesh.position.set(objDef.pos[0], objDef.height / 2, objDef.pos[1]);

      // Silver metallic top and bottom rims
      const rimMat = new THREE.MeshStandardMaterial({ color: 0xd1d5db, roughness: 0.25, metalness: 0.85 });
      const topRimGeo = new THREE.TorusGeometry(objDef.size / 2 - 0.015, 0.012, 16, 32);
      const topRim = new THREE.Mesh(topRimGeo, rimMat);
      topRim.rotation.x = Math.PI / 2;
      topRim.position.y = objDef.height / 2;
      mesh.add(topRim);

      const botRim = new THREE.Mesh(topRimGeo, rimMat);
      botRim.rotation.x = Math.PI / 2;
      botRim.position.y = -objDef.height / 2;
      mesh.add(botRim);

      // Aluminum pull-tab on top
      const tabGeo = new THREE.BoxGeometry(0.04, 0.004, 0.06);
      const tab = new THREE.Mesh(tabGeo, rimMat);
      tab.position.set(0, objDef.height / 2 + 0.004, 0.02);
      mesh.add(tab);

    } else if (objDef.shape === 'cone') {
      // Traffic safety pylon design
      const coneGroup = new THREE.Group();

      // Cone body
      const coneGeo = new THREE.ConeGeometry(objDef.size / 2, objDef.height, 48);
      const coneMesh = new THREE.Mesh(coneGeo, mat);
      coneMesh.position.y = objDef.height / 2;
      coneGroup.add(coneMesh);

      // Square weighted base plate
      const baseMat = new THREE.MeshStandardMaterial({ color: 0x1e293b, roughness: 0.6, metalness: 0.3 });
      const basePlateGeo = new THREE.BoxGeometry(objDef.size * 1.15, 0.02, objDef.size * 1.15);
      const basePlate = new THREE.Mesh(basePlateGeo, baseMat);
      basePlate.position.y = 0.01;
      coneGroup.add(basePlate);

      // Reflective white safety band around the cone
      const bandMat = new THREE.MeshStandardMaterial({ color: 0xf8fafc, roughness: 0.3, metalness: 0.4 });
      const bandGeo = new THREE.CylinderGeometry(objDef.size * 0.28, objDef.size * 0.38, 0.09, 32, 1, true);
      const band = new THREE.Mesh(bandGeo, bandMat);
      band.position.y = objDef.height * 0.45;
      coneGroup.add(band);

      coneGroup.position.set(objDef.pos[0], 0, objDef.pos[1]);
      mesh = coneGroup;
    }

    mesh.castShadow = true;
    mesh.receiveShadow = true;
    scene.add(mesh);

    objects.push({
      ...objDef,
      mesh,
      shadowDecal,
      currentPos: [...objDef.pos],
      held: false
    });
  }

  return {
    config,
    scene,
    objects,
    zone: { center: [zx, zy], size: zSize, mesh: zoneMesh }
  };
}

// Footprint calculation for analytical 2D collision & ray casting
export function footprintOf(obj) {
  const [x, y] = obj.currentPos;
  if (obj.shape === 'cube') {
    const half = obj.size / 2;
    return {
      type: 'box',
      minX: x - half,
      maxX: x + half,
      minY: y - half,
      maxY: y + half,
      centerX: x,
      centerY: y,
      size: obj.size
    };
  } else {
    // sphere, cylinder, cone are circles from above
    return {
      type: 'circle',
      centerX: x,
      centerY: y,
      radius: obj.size / 2
    };
  }
}

// 2D Distance
function distSq(x1, y1, x2, y2) {
  const dx = x1 - x2;
  const dy = y1 - y2;
  return dx * dx + dy * dy;
}

// Circle-to-Circle Collision
function circleCircleCollide(c1x, c1y, r1, c2x, c2y, r2) {
  const rSum = r1 + r2;
  return distSq(c1x, c1y, c2x, c2y) < rSum * rSum;
}

// Circle-to-AABB Collision
function circleBoxCollide(cx, cy, cr, minX, maxX, minY, maxY) {
  const closestX = Math.max(minX, Math.min(cx, maxX));
  const closestY = Math.max(minY, Math.min(cy, maxY));
  return distSq(cx, cy, closestX, closestY) < cr * cr;
}

// Check if position or held object collides with walls or any floor object
export function collides(pos, radius, heldObj, world) {
  const [rx, ry] = pos;
  const halfRoom = world.config.room.size / 2; // 3.0

  // 1. Robot circle vs walls (clamping boundary +/- 2.8)
  const wallLimit = halfRoom - radius; // 3.0 - 0.2 = 2.8
  if (rx < -wallLimit || rx > wallLimit || ry < -wallLimit || ry > wallLimit) {
    return true;
  }

  // 2. Robot circle vs floor objects
  for (const obj of world.objects) {
    if (obj.held) continue;
    const fp = footprintOf(obj);
    if (fp.type === 'circle') {
      if (circleCircleCollide(rx, ry, radius, fp.centerX, fp.centerY, fp.radius)) {
        return true;
      }
    } else if (fp.type === 'box') {
      if (circleBoxCollide(rx, ry, radius, fp.minX, fp.maxX, fp.minY, fp.maxY)) {
        return true;
      }
    }
  }

  // 3. Held object footprint collision vs walls and other objects
  if (heldObj) {
    const hfp = footprintOf(heldObj);
    // Held object vs walls
    if (hfp.type === 'circle') {
      const hLimit = halfRoom - hfp.radius;
      if (hfp.centerX < -hLimit || hfp.centerX > hLimit || hfp.centerY < -hLimit || hfp.centerY > hLimit) {
        return true;
      }
    } else if (hfp.type === 'box') {
      if (hfp.minX < -halfRoom || hfp.maxX > halfRoom || hfp.minY < -halfRoom || hfp.maxY > halfRoom) {
        return true;
      }
    }

    // Held object vs other unheld objects
    for (const obj of world.objects) {
      if (obj.held || obj.id === heldObj.id) continue;
      const ofp = footprintOf(obj);
      if (hfp.type === 'circle' && ofp.type === 'circle') {
        if (circleCircleCollide(hfp.centerX, hfp.centerY, hfp.radius, ofp.centerX, ofp.centerY, ofp.radius)) return true;
      } else if (hfp.type === 'circle' && ofp.type === 'box') {
        if (circleBoxCollide(hfp.centerX, hfp.centerY, hfp.radius, ofp.minX, ofp.maxX, ofp.minY, ofp.maxY)) return true;
      } else if (hfp.type === 'box' && ofp.type === 'circle') {
        if (circleBoxCollide(ofp.centerX, ofp.centerY, ofp.radius, hfp.minX, hfp.maxX, hfp.minY, hfp.maxY)) return true;
      } else if (hfp.type === 'box' && ofp.type === 'box') {
        if (!(hfp.maxX < ofp.minX || hfp.minX > ofp.maxX || hfp.maxY < ofp.minY || hfp.minY > ofp.maxY)) return true;
      }
    }
  }

  return false;
}

// Horizontal 2D Raycast along heading
export function rayDepth(origin, headingDeg, ignoreObjId, world) {
  const [ox, oy] = origin;
  const rad = (headingDeg * Math.PI) / 180;
  const dx = Math.cos(rad);
  const dy = Math.sin(rad);

  let minT = 10.0; // Max ray distance = 10m
  let hit = false;

  const halfRoom = world.config.room.size / 2; // 3.0

  // 1. Ray vs 4 walls
  // x = +halfRoom
  if (dx > 1e-6) {
    const t = (halfRoom - ox) / dx;
    if (t > 0 && t < minT) {
      const yAt = oy + t * dy;
      if (yAt >= -halfRoom && yAt <= halfRoom) { minT = t; hit = true; }
    }
  }
  // x = -halfRoom
  if (dx < -1e-6) {
    const t = (-halfRoom - ox) / dx;
    if (t > 0 && t < minT) {
      const yAt = oy + t * dy;
      if (yAt >= -halfRoom && yAt <= halfRoom) { minT = t; hit = true; }
    }
  }
  // y = +halfRoom
  if (dy > 1e-6) {
    const t = (halfRoom - oy) / dy;
    if (t > 0 && t < minT) {
      const xAt = ox + t * dx;
      if (xAt >= -halfRoom && xAt <= halfRoom) { minT = t; hit = true; }
    }
  }
  // y = -halfRoom
  if (dy < -1e-6) {
    const t = (-halfRoom - oy) / dy;
    if (t > 0 && t < minT) {
      const xAt = ox + t * dx;
      if (xAt >= -halfRoom && xAt <= halfRoom) { minT = t; hit = true; }
    }
  }

  // 2. Ray vs Objects
  for (const obj of world.objects) {
    if (obj.held || obj.id === ignoreObjId) continue;
    const fp = footprintOf(obj);

    if (fp.type === 'circle') {
      // Ray-circle analytical intersection
      const ocx = ox - fp.centerX;
      const ocy = oy - fp.centerY;
      const b = 2 * (ocx * dx + ocy * dy);
      const c = ocx * ocx + ocy * ocy - fp.radius * fp.radius;
      const disc = b * b - 4 * c;
      if (disc >= 0) {
        const sqrtDisc = Math.sqrt(disc);
        const t1 = (-b - sqrtDisc) / 2;
        const t2 = (-b + sqrtDisc) / 2;
        if (t1 > 0 && t1 < minT) { minT = t1; hit = true; }
        else if (t2 > 0 && t2 < minT) { minT = t2; hit = true; }
      }
    } else if (fp.type === 'box') {
      // Ray-AABB intersection (Slab method)
      let tNear = -Infinity;
      let tFar = Infinity;

      if (Math.abs(dx) < 1e-6) {
        if (ox < fp.minX || ox > fp.maxX) continue;
      } else {
        let t1 = (fp.minX - ox) / dx;
        let t2 = (fp.maxX - ox) / dx;
        if (t1 > t2) [t1, t2] = [t2, t1];
        if (t1 > tNear) tNear = t1;
        if (t2 < tFar) tFar = t2;
        if (tNear > tFar || tFar < 0) continue;
      }

      if (Math.abs(dy) < 1e-6) {
        if (oy < fp.minY || oy > fp.maxY) continue;
      } else {
        let t1 = (fp.minY - oy) / dy;
        let t2 = (fp.maxY - oy) / dy;
        if (t1 > t2) [t1, t2] = [t2, t1];
        if (t1 > tNear) tNear = t1;
        if (t2 < tFar) tFar = t2;
        if (tNear > tFar || tFar < 0) continue;
      }

      if (tNear > 0 && tNear < minT) {
        minT = tNear;
        hit = true;
      }
    }
  }

  return hit && minT <= 10.0 ? Math.round(minT * 100) / 100 : null;
}

// Check if point is inside drop zone square
export function inZone(pos, world) {
  const [px, py] = pos;
  const [zx, zy] = world.zone.center;
  const half = world.zone.size / 2;
  return px >= zx - half && px <= zx + half && py >= zy - half && py <= zy + half;
}

