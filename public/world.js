import * as THREE from 'three';

export async function loadConfig(url = '/config/level1.json') {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Failed to load config from ${url}: ${res.statusText}`);
  return await res.json();
}

export function buildWorld(config, scene) {
  const halfRoom = config.room.size / 2; // 3.0
  const wallHeight = config.room.wall_height || 2.5;

  // 1. Tan Floor with procedural faint grid/tile lines
  const floorCanvas = document.createElement('canvas');
  floorCanvas.width = 512;
  floorCanvas.height = 512;
  const fctx = floorCanvas.getContext('2d');
  fctx.fillStyle = '#d2b48c'; // Tan floor
  fctx.fillRect(0, 0, 512, 512);
  fctx.strokeStyle = '#c4a480';
  fctx.lineWidth = 4;
  const tileSize = 64;
  for (let x = 0; x <= 512; x += tileSize) {
    fctx.beginPath();
    fctx.moveTo(x, 0);
    fctx.lineTo(x, 512);
    fctx.stroke();
  }
  for (let y = 0; y <= 512; y += tileSize) {
    fctx.beginPath();
    fctx.moveTo(0, y);
    fctx.lineTo(512, y);
    fctx.stroke();
  }

  const floorTexture = new THREE.CanvasTexture(floorCanvas);
  floorTexture.wrapS = THREE.RepeatWrapping;
  floorTexture.wrapT = THREE.RepeatWrapping;
  floorTexture.repeat.set(6, 6);

  const floorGeo = new THREE.PlaneGeometry(config.room.size, config.room.size);
  const floorMat = new THREE.MeshStandardMaterial({
    map: floorTexture,
    roughness: 0.9,
    metalness: 0.05
  });
  const floor = new THREE.Mesh(floorGeo, floorMat);
  floor.rotation.x = -Math.PI / 2;
  floor.position.set(0, 0, 0);
  floor.receiveShadow = true;
  scene.add(floor);

  // 2. Ceiling (so camera looking slightly up or at horizon sees a warm ceiling, no void)
  const ceilingMat = new THREE.MeshStandardMaterial({ color: '#f3e8d2', roughness: 1.0 });
  const ceiling = new THREE.Mesh(floorGeo, ceilingMat);
  ceiling.rotation.x = Math.PI / 2;
  ceiling.position.set(0, wallHeight, 0);
  scene.add(ceiling);

  // 3. Four Wooden Walls
  const wallMat = new THREE.MeshStandardMaterial({ color: '#8b5a2b', roughness: 0.85, metalness: 0.0 });
  const wallGeoZ = new THREE.BoxGeometry(0.1, wallHeight, config.room.size);
  const wallGeoX = new THREE.BoxGeometry(config.room.size, wallHeight, 0.1);

  // Left wall (-x)
  const wallLeft = new THREE.Mesh(wallGeoZ, wallMat);
  wallLeft.position.set(-halfRoom, wallHeight / 2, 0);
  scene.add(wallLeft);

  // Right wall (+x)
  const wallRight = new THREE.Mesh(wallGeoZ, wallMat);
  wallRight.position.set(halfRoom, wallHeight / 2, 0);
  scene.add(wallRight);

  // Front wall (-z / -y in 2D)
  const wallFront = new THREE.Mesh(wallGeoX, wallMat);
  wallFront.position.set(0, wallHeight / 2, -halfRoom);
  scene.add(wallFront);

  // Back wall (+z / +y in 2D)
  const wallBack = new THREE.Mesh(wallGeoX, wallMat);
  wallBack.position.set(0, wallHeight / 2, halfRoom);
  scene.add(wallBack);

  // 4. Drop Zone (Flat Green Square on Floor)
  const [zx, zy] = config.zone.center;
  const zSize = config.zone.size;
  const zoneGeo = new THREE.PlaneGeometry(zSize, zSize);
  const zoneMat = new THREE.MeshStandardMaterial({
    color: '#10b981', // vibrant emerald green
    roughness: 0.6,
    metalness: 0.1
  });
  const zoneMesh = new THREE.Mesh(zoneGeo, zoneMat);
  zoneMesh.rotation.x = -Math.PI / 2;
  zoneMesh.position.set(zx, 0.002, zy); // slight lift to prevent z-fighting
  zoneMesh.receiveShadow = true;
  scene.add(zoneMesh);

  // 5. Lighting (soft ambient + directional sun + subtle fill light)
  const ambient = new THREE.AmbientLight(0xffffff, 0.95);
  scene.add(ambient);

  const dirLight = new THREE.DirectionalLight(0xfff5eb, 0.75);
  dirLight.position.set(2, 4, 1);
  dirLight.castShadow = true;
  dirLight.shadow.mapSize.width = 1024;
  dirLight.shadow.mapSize.height = 1024;
  scene.add(dirLight);

  // 6. Spawn the 6 Objects
  const objects = [];
  for (const objDef of config.objects) {
    let mesh;
    const color = objDef.hex;
    const mat = new THREE.MeshStandardMaterial({
      color: color,
      roughness: 0.8,
      metalness: 0.1
    });

    if (objDef.shape === 'cube') {
      const geo = new THREE.BoxGeometry(objDef.size, objDef.height, objDef.size);
      mesh = new THREE.Mesh(geo, mat);
      mesh.position.set(objDef.pos[0], objDef.height / 2, objDef.pos[1]);
    } else if (objDef.shape === 'sphere') {
      const geo = new THREE.SphereGeometry(objDef.size / 2, 32, 24);
      mesh = new THREE.Mesh(geo, mat);
      mesh.position.set(objDef.pos[0], objDef.size / 2, objDef.pos[1]);
    } else if (objDef.shape === 'cylinder') {
      const geo = new THREE.CylinderGeometry(objDef.size / 2, objDef.size / 2, objDef.height, 32);
      mesh = new THREE.Mesh(geo, mat);
      mesh.position.set(objDef.pos[0], objDef.height / 2, objDef.pos[1]);

      if (objDef.lid_ring) {
        // Can-like metallic lid ring on top
        const ringGeo = new THREE.TorusGeometry(objDef.size / 2 - 0.02, 0.012, 16, 32);
        const ringMat = new THREE.MeshStandardMaterial({ color: '#ffffff', roughness: 0.3, metalness: 0.8 });
        const ringMesh = new THREE.Mesh(ringGeo, ringMat);
        ringMesh.rotation.x = Math.PI / 2;
        ringMesh.position.y = objDef.height / 2 + 0.005;
        mesh.add(ringMesh);
      }
    } else if (objDef.shape === 'cone') {
      const geo = new THREE.ConeGeometry(objDef.size / 2, objDef.height, 32);
      mesh = new THREE.Mesh(geo, mat);
      mesh.position.set(objDef.pos[0], objDef.height / 2, objDef.pos[1]);
    }

    mesh.castShadow = true;
    mesh.receiveShadow = true;
    scene.add(mesh);

    objects.push({
      ...objDef,
      mesh,
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

