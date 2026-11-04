export class Grader {
  constructor(world) {
    this.world = world;
    this.manualVerdict = null;
  }

  resolveTargets(commandText, objects) {
    if (!commandText || typeof commandText !== 'string') return null;
    const lower = commandText.toLowerCase().trim();

    // 1. Indirect commands
    if (lower.includes('drink') || lower.includes('can') || lower.includes('soda') || lower.includes('beverage')) {
      const drinkObj = objects.find(o => o.lid_ring || (o.shape === 'cylinder' && o.color === 'yellow'));
      return drinkObj ? [drinkObj.id] : null;
    }

    // 2. Relative reasoning: "tallest"
    if (lower.includes('tallest')) {
      let maxH = -1;
      let tallest = null;
      for (const obj of objects) {
        const h = obj.height || obj.size;
        if (h > maxH) {
          maxH = h;
          tallest = obj;
        }
      }
      return tallest ? [tallest.id] : null;
    }

    // 3. Relative reasoning: "smallest"
    if (lower.includes('smallest')) {
      let minH = Infinity;
      let smallest = null;
      for (const obj of objects) {
        const sz = obj.size;
        if (sz < minH) {
          minH = sz;
          smallest = obj;
        }
      }
      return smallest ? [smallest.id] : null;
    }

    // 4. "farthest from" drop zone
    if (lower.includes('farthest') && (lower.includes('zone') || lower.includes('square') || lower.includes('green'))) {
      const [zx, zy] = this.world.zone.center;
      let maxDist = -1;
      let best = null;
      const targetShape = lower.includes('cube') ? 'cube' : lower.includes('sphere') ? 'sphere' : null;

      for (const obj of objects) {
        if (targetShape && obj.shape !== targetShape) continue;
        const [ox, oy] = obj.pos;
        const d = Math.hypot(ox - zx, oy - zy);
        if (d > maxDist) {
          maxDist = d;
          best = obj;
        }
      }
      return best ? [best.id] : null;
    }

    // 5. Shape and Color filters
    let targetShape = null;
    if (lower.includes('sphere') || lower.includes('round') || lower.includes('ball')) {
      targetShape = 'sphere'; // 'round' strictly means sphere
    } else if (lower.includes('cube') || lower.includes('box')) {
      targetShape = 'cube';
    } else if (lower.includes('cylinder') || lower.includes('can')) {
      targetShape = 'cylinder';
    } else if (lower.includes('cone')) {
      targetShape = 'cone';
    }

    let targetColor = null;
    if (lower.includes('red')) targetColor = 'red';
    else if (lower.includes('blue')) targetColor = 'blue';
    else if (lower.includes('yellow')) targetColor = 'yellow';
    else if (lower.includes('orange')) targetColor = 'orange';

    const matches = objects.filter(o => {
      let ok = true;
      if (targetShape && o.shape !== targetShape) ok = false;
      if (targetColor && o.color !== targetColor) ok = false;
      return ok;
    });

    if (matches.length > 0) {
      return matches.map(m => m.id);
    }

    return null; // Could not reliably parse, requires manual judge verdict
  }

  grade(heldObj, inZone, retries, commandText) {
    if (retries >= 4) {
      return 'fail_collision';
    }

    if (!inZone) {
      return 'fail_not_reached';
    }

    if (!heldObj) {
      return 'fail_not_reached';
    }

    if (this.manualVerdict !== null) {
      return this.manualVerdict ? 'pass' : 'fail_wrong_object';
    }

    const targets = this.resolveTargets(commandText, this.world.objects);
    if (!targets) {
      return 'needs_manual';
    }

    if (targets.includes(heldObj.id)) {
      return 'pass';
    }

    return 'fail_wrong_object';
  }

  applyManualVerdict(yes) {
    this.manualVerdict = Boolean(yes);
  }
}

