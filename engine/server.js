import express from 'express';
import { WebSocketServer } from 'ws';
import http from 'http';
import path from 'path';
import fs from 'fs';
import { fileURLToPath } from 'url';
import crypto from 'crypto';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const rootDir = path.resolve(__dirname, '..');

// Load .env if present
const envPath = path.join(rootDir, '.env');
if (fs.existsSync(envPath)) {
  const content = fs.readFileSync(envPath, 'utf-8');
  for (const line of content.split('\n')) {
    const trimmed = line.trim();
    if (trimmed && !trimmed.startsWith('#') && trimmed.includes('=')) {
      const [k, ...v] = trimmed.split('=');
      const key = k.trim();
      const val = v.join('=').trim().replace(/^['"]|['"]$/g, '');
      if (!process.env[key] && val) {
        process.env[key] = val;
      }
    }
  }
}

// Express apps
const agentApp = express();
const adminApp = express();

agentApp.use(express.json());
adminApp.use(express.json());

// Serve static files (HTML, CSS, JS, config, and three.js from node_modules)
agentApp.use(express.static(path.join(rootDir, 'public')));
agentApp.use('/config', express.static(path.join(rootDir, 'config')));
agentApp.use('/node_modules', express.static(path.join(rootDir, 'node_modules')));

// Global state & WebSocket connection
let engineSocket = null;
const pendingRequests = new Map();
let isActionLocked = false;

// Whitelist definitions according to hackathon data contract
const WHITELISTS = {
  state: ['name', 'x', 'y', 'heading', 'holding', 'retries'],
  depth: ['depth'],
  move: ['moved', 'collided'],
  turn: ['heading'],
  pickup: ['ok'],
  drop: ['ok', 'in_zone']
};

function scrub(op, rawResult) {
  if (typeof rawResult !== 'object' || rawResult === null) {
    return rawResult;
  }
  const allowed = WHITELISTS[op];
  if (!allowed) {
    return rawResult;
  }
  const scrubbed = {};
  for (const key of allowed) {
    if (key in rawResult) {
      scrubbed[key] = rawResult[key];
    }
  }
  // Detect leak attempts
  for (const key of Object.keys(rawResult)) {
    if (!allowed.includes(key)) {
      console.warn(`[LEAK FIREWALL WARNING] Dropped unauthorized key '${key}' from op '${op}'`);
    }
  }
  return scrubbed;
}

// Single action lock wrapper
function withLock(handler) {
  return async (req, res) => {
    if (isActionLocked) {
      return res.status(409).json({ error: 'Action already in progress (409 Conflict)' });
    }
    isActionLocked = true;
    try {
      await handler(req, res);
    } catch (err) {
      console.error('Handler error:', err);
      if (!res.headersSent) {
        res.status(500).json({ error: err.message || 'Internal server error' });
      }
    } finally {
      isActionLocked = false;
    }
  };
}

// Call Engine over WebSocket
function callEngine(op, args = {}, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    if (!engineSocket || engineSocket.readyState !== 1) { // 1 = OPEN
      return reject(new Error('ENGINE_NOT_CONNECTED'));
    }

    const id = crypto.randomUUID();
    const timer = setTimeout(() => {
      pendingRequests.delete(id);
      reject(new Error(`Engine request '${op}' timed out after ${timeoutMs}ms`));
    }, timeoutMs);

    pendingRequests.set(id, { resolve, reject, timer, op });

    try {
      engineSocket.send(JSON.stringify({ id, op, args }));
    } catch (err) {
      clearTimeout(timer);
      pendingRequests.delete(id);
      reject(err);
    }
  });
}

// Helper to handle standard agent dispatch
function agentDispatch(op, getArgs = (req) => ({})) {
  return withLock(async (req, res) => {
    try {
      const args = getArgs(req);
      const rawResult = await callEngine(op, args);
      const cleanResult = scrub(op, rawResult);
      res.json(cleanResult);
    } catch (err) {
      if (err.message === 'ENGINE_NOT_CONNECTED') {
        return res.status(503).json({ error: 'Engine browser page not connected (503)' });
      }
      res.status(500).json({ error: err.message });
    }
  });
}

// ==================== AGENT ROUTES (:8080) ====================

// GET /state
agentApp.get('/state', agentDispatch('state'));

// GET /depth
agentApp.get('/depth', agentDispatch('depth'));

// POST /move { distance }
agentApp.post('/move', agentDispatch('move', req => ({ distance: Number(req.body.distance || 0) })));

// POST /turn { degrees }
agentApp.post('/turn', agentDispatch('turn', req => ({ degrees: Number(req.body.degrees || 0) })));

// POST /pickup
agentApp.post('/pickup', agentDispatch('pickup'));

// POST /drop
agentApp.post('/drop', agentDispatch('drop'));

// GET /look -> Returns pure JPEG bytes
agentApp.get('/look', withLock(async (req, res) => {
  try {
    const rawResult = await callEngine('look');
    // rawResult can be { jpegBase64: '...' } or a base64 data URL
    let base64Data = rawResult.jpegBase64 || rawResult;
    if (typeof base64Data === 'string' && base64Data.startsWith('data:image')) {
      base64Data = base64Data.split(',')[1];
    }
    const buffer = Buffer.from(base64Data, 'base64');
    res.setHeader('Content-Type', 'image/jpeg');
    res.setHeader('Content-Length', buffer.length);
    res.end(buffer);
  } catch (err) {
    if (err.message === 'ENGINE_NOT_CONNECTED') {
      return res.status(503).json({ error: 'Engine browser page not connected (503)' });
    }
    res.status(500).json({ error: err.message });
  }
}));

// ==================== ADMIN ROUTES (:8081) ====================

// POST /admin/command { text }
adminApp.post('/admin/command', async (req, res) => {
  try {
    const result = await callEngine('setCommand', { text: req.body.text || '' });
    res.json(result);
  } catch (err) {
    if (err.message === 'ENGINE_NOT_CONNECTED') {
      return res.status(503).json({ error: 'Engine browser page not connected (503)' });
    }
    res.status(500).json({ error: err.message });
  }
});

// POST /admin/reset
adminApp.post('/admin/reset', async (req, res) => {
  try {
    const result = await callEngine('reset');
    res.json(result);
  } catch (err) {
    if (err.message === 'ENGINE_NOT_CONNECTED') {
      return res.status(503).json({ error: 'Engine browser page not connected (503)' });
    }
    res.status(500).json({ error: err.message });
  }
});

// POST /admin/giveup
adminApp.post('/admin/giveup', async (req, res) => {
  try {
    const result = await callEngine('giveUp');
    res.json(result);
  } catch (err) {
    if (err.message === 'ENGINE_NOT_CONNECTED') {
      return res.status(503).json({ error: 'Engine browser page not connected (503)' });
    }
    res.status(500).json({ error: err.message });
  }
});

// POST /admin/verdict { pass: boolean }
adminApp.post('/admin/verdict', async (req, res) => {
  try {
    const result = await callEngine('manualVerdict', { pass: Boolean(req.body.pass) });
    res.json(result);
  } catch (err) {
    if (err.message === 'ENGINE_NOT_CONNECTED') {
      return res.status(503).json({ error: 'Engine browser page not connected (503)' });
    }
    res.status(500).json({ error: err.message });
  }
});

// GET /admin/result -> { outcome, steps, simSeconds, collisions, pathLength }
adminApp.get('/admin/result', async (req, res) => {
  try {
    const result = await callEngine('getResult');
    res.json(result);
  } catch (err) {
    if (err.message === 'ENGINE_NOT_CONNECTED') {
      return res.status(503).json({ error: 'Engine browser page not connected (503)' });
    }
    res.status(500).json({ error: err.message });
  }
});

// ==================== ANTIGRAVITY / EASY CLI RUNNER ====================
let isAntigravityRunning = false;
let abortAntigravity = false;

// GET /api/antigravity/status
agentApp.get('/api/antigravity/status', async (req, res) => {
  const proxyUrl = req.query.proxyUrl || process.env.ANTIGRAVITY_BASE_URL || 'http://127.0.0.1:8317/v1';
  const apiKey = req.query.apiKey || process.env.ANTIGRAVITY_API_KEY || '12345';
  try {
    const r = await fetch(`${proxyUrl.replace(/\/+$/, '')}/models`, {
      headers: { 'Authorization': `Bearer ${apiKey}` },
      signal: AbortSignal.timeout(3000)
    });
    if (r.ok) {
      const data = await r.json();
      return res.json({
        connected: true,
        proxyUrl,
        models: data.data || [],
        isRunning: isAntigravityRunning
      });
    }
  } catch (e) {}
  res.json({
    connected: false,
    proxyUrl,
    models: [],
    isRunning: isAntigravityRunning
  });
});

// POST /api/antigravity/stop
agentApp.post('/api/antigravity/stop', (req, res) => {
  abortAntigravity = true;
  isAntigravityRunning = false;
  res.json({ ok: true, status: 'Stopped' });
});

// POST /api/antigravity/start
agentApp.post('/api/antigravity/start', async (req, res) => {
  if (isAntigravityRunning) {
    return res.status(409).json({ error: 'An Antigravity run is already active' });
  }

  const proxyUrl = req.body.proxyUrl || process.env.ANTIGRAVITY_BASE_URL || 'http://127.0.0.1:8317/v1';
  const apiKey = req.body.apiKey || process.env.ANTIGRAVITY_API_KEY || '12345';
  const command = req.body.command || 'Bring the round red thing';
  const model = req.body.model || 'claude-sonnet-4-6';

  isAntigravityRunning = true;
  abortAntigravity = false;

  res.json({ ok: true, status: 'Started', command, model });

  (async () => {
    const broadcast = (data) => {
      if (engineSocket && engineSocket.readyState === 1) {
        engineSocket.send(JSON.stringify({ type: 'antigravity_update', ...data }));
      }
    };

    try {
      console.log(`[Antigravity Agent] Starting run with '${model}' via ${proxyUrl} for: "${command}"`);
      await callEngine('setCommand', { text: command });

      const MAX_STEPS = 25;
      let frame = await callEngine('look');
      let base64Img = frame.jpegBase64 || frame;
      if (typeof base64Img === 'string' && base64Img.startsWith('data:image')) {
        base64Img = base64Img.split(',')[1];
      }

      const toolsSchema = [
        {
          type: "function",
          function: {
            name: "move",
            description: "Drive robot forward (positive distance) or backward (negative) by exact metres (e.g. 0.5, 1.0, 1.5). Scale: 1 floor block tile = 0.5m.",
            parameters: {
              type: "object",
              properties: {
                distance: { type: "number", description: "Exact distance to travel in metres" },
                observations: { type: "string", description: "Visual analysis of the scene" },
                target: { type: "string", description: "Target being approached or bypassed" },
                confidence: { type: "number", description: "0.0 to 1.0" }
              },
              required: ["distance", "observations", "target", "confidence"]
            }
          }
        },
        {
          type: "function",
          function: {
            name: "turn",
            description: "Rotate robot in place by exact degrees. Positive = clockwise (right), negative = counter-clockwise (left). 90° FOV camera means ~7 pixels per degree.",
            parameters: {
              type: "object",
              properties: {
                degrees: { type: "number", description: "Exact degrees to turn (e.g. -25, 30, 90, 180)" },
                observations: { type: "string", description: "Visual scene analysis" },
                target: { type: "string", description: "Target heading" },
                confidence: { type: "number", description: "0.0 to 1.0" }
              },
              required: ["degrees", "observations", "target", "confidence"]
            }
          }
        },
        {
          type: "function",
          function: {
            name: "read_depth",
            description: "Measures center horizontal laser ray distance straight ahead up to 10 metres.",
            parameters: {
              type: "object",
              properties: {
                observations: { type: "string", description: "Depth check reason" },
                target: { type: "string", description: "Target" },
                confidence: { type: "number", description: "0.0 to 1.0" }
              },
              required: ["observations", "target", "confidence"]
            }
          }
        },
        {
          type: "function",
          function: {
            name: "pickup",
            description: "Grasp object directly in front. Only succeeds when target distance is <= 0.55m.",
            parameters: {
              type: "object",
              properties: {
                observations: { type: "string", description: "Close target confirmation" },
                target: { type: "string", description: "Object being picked up" },
                confidence: { type: "number", description: "0.0 to 1.0" }
              },
              required: ["observations", "target", "confidence"]
            }
          }
        },
        {
          type: "function",
          function: {
            name: "drop",
            description: "Release held object into the green drop zone on the floor.",
            parameters: {
              type: "object",
              properties: {
                observations: { type: "string", description: "Drop zone confirmation" },
                target: { type: "string", description: "Green drop zone" },
                confidence: { type: "number", description: "0.0 to 1.0" }
              },
              required: ["observations", "target", "confidence"]
            }
          }
        },
        {
          type: "function",
          function: {
            name: "done",
            description: "End mission successfully once object is deposited in the green zone.",
            parameters: {
              type: "object",
              properties: {
                observations: { type: "string", description: "Mission completion summary" },
                target: { type: "string", description: "Completed target" },
                confidence: { type: "number", description: "1.0" }
              },
              required: ["observations", "target", "confidence"]
            }
          }
        }
      ];

      function pruneOlderImages(msgs) {
        let seen = false;
        for (let i = msgs.length - 1; i >= 0; i--) {
          const m = msgs[i];
          if (Array.isArray(m.content)) {
            for (let j = 0; j < m.content.length; j++) {
              if (m.content[j].type === 'image_url') {
                if (seen) {
                  m.content[j] = { type: 'text', text: '[earlier frame omitted]' };
                }
                seen = true;
              }
            }
          }
        }
      }

      const messages = [
        {
          role: "system",
          content: `You are the autonomous navigation controller of a mobile AMR robot in a 6.0m x 6.0m arena.
Your physical specifications:
- Forward Camera: 90° horizontal FOV, 640x480 resolution. Center reticle is at x=320, y=240.
  Rotation scale: ~7 pixels horizontally per 1° degree of rotation.
  Clockwise (right) is positive degrees (+), counter-clockwise (left) is negative degrees (-).
- Laser Rangefinder: Measures exact line-of-sight distance straight ahead at center reticle.
- Floor Grid: Floor tiles are 0.5m x 0.5m squares ("blocks"). 2 blocks = 1.0m, 4 blocks = 2.0m.
- Gripper: Effective grasp reach is 0.55m. Attempting pickup when distance > 0.55m will FAIL.

Navigation & Obstacle Avoidance Protocol:
1. Scan & Locate: Look at the 3D first-person image. Note where the target object is.
2. Occlusion Bypass: If an obstacle (e.g. orange traffic cone) sits directly in front of your path to the target, DO NOT ram it. Steer sideways (e.g. turn -30° or +30°), drive forward 0.6m, then turn back toward the target to bypass it with clear line of sight.
3. Approach: Use laser depth to gauge distance. Advance smoothly until depth reading is between 0.40m and 0.50m.
4. Grasp: Call pickup() when depth is <= 0.55m and target is directly centered.
5. Deposit: Once holding the object, rotate until the flat glowing green square drop zone on the floor is centered, advance onto it, call drop(), and then done().`
        },
        {
          role: "user",
          content: [
            { type: "text", text: `Command: "${command}". Here is your starting onboard camera view:` },
            { type: "image_url", image_url: { url: `data:image/jpeg;base64,${base64Img}`, detail: "low" } }
          ]
        }
      ];

      for (let step = 1; step <= MAX_STEPS; step++) {
        if (abortAntigravity) {
          console.log('[Antigravity Agent] Aborted.');
          broadcast({ state: 'ABORTED', step });
          break;
        }

        pruneOlderImages(messages);

        const depthData = await callEngine('depth');
        const stateData = await callEngine('state');
        const currentDepth = depthData.depth;
        const isHolding = stateData.holding;

        broadcast({ state: 'THINKING', step, depth: currentDepth, holding: isHolding });
        const startTime = Date.now();

        const chatEndpoint = `${proxyUrl.replace(/\/+$/, '')}/chat/completions`;
        const resp = await fetch(chatEndpoint, {
          method: 'POST',
          headers: {
            'Authorization': `Bearer ${apiKey}`,
            'Content-Type': 'application/json'
          },
          body: JSON.stringify({
            model: model,
            messages: messages,
            tools: toolsSchema,
            tool_choice: "auto",
            temperature: 0.1,
            max_tokens: 1000
          })
        });

        if (!resp.ok) {
          const errText = await resp.text();
          console.error('[Antigravity Error]:', resp.status, errText);
          broadcast({ state: 'ERROR', error: `HTTP ${resp.status}: ${errText}` });
          break;
        }

        const resData = await resp.json();
        const choice = resData.choices?.[0];
        const resMsg = choice?.message || {};
        messages.push(resMsg);
        const elapsedMs = Date.now() - startTime;

        let toolName = null;
        let toolArgs = {};
        let observations = '';
        let confidence = 0.9;
        let callId = `call_${step}`;

        if (resMsg.tool_calls && resMsg.tool_calls.length > 0) {
          const call = resMsg.tool_calls[0];
          callId = call.id || callId;
          toolName = call.function?.name;
          try {
            toolArgs = typeof call.function?.arguments === 'string'
              ? JSON.parse(call.function.arguments)
              : (call.function?.arguments || {});
          } catch (e) {
            toolArgs = {};
          }
          observations = toolArgs.observations || resMsg.content || '';
          confidence = toolArgs.confidence !== undefined ? toolArgs.confidence : 0.9;
        } else {
          // Fallback parsing from text
          const rawText = resMsg.content || '';
          observations = rawText;
          const jsonMatch = rawText.match(/\{[\s\S]*\}/);
          if (jsonMatch) {
            try {
              const p = JSON.parse(jsonMatch[0]);
              toolName = p.tool || p.name;
              toolArgs = p.args || p.parameters || {};
              observations = p.observations || rawText;
              confidence = p.confidence || 0.8;
            } catch (e) {}
          }
          if (!toolName) {
            toolName = 'read_depth';
            toolArgs = {};
          }
        }

        console.log(`[Antigravity Step ${step}] ${toolName}`, toolArgs, `(Latency: ${elapsedMs}ms)`);

        let actionResult = null;
        if (toolName === 'turn') {
          const deg = toolArgs.degrees !== undefined ? Number(toolArgs.degrees) : (toolArgs.angle !== undefined ? Number(toolArgs.angle) : 45);
          actionResult = await callEngine('turn', { degrees: isNaN(deg) ? 45 : deg });
        } else if (toolName === 'move') {
          const dist = toolArgs.distance !== undefined ? Number(toolArgs.distance) : (toolArgs.amount !== undefined ? Number(toolArgs.amount) : 0.5);
          actionResult = await callEngine('move', { distance: isNaN(dist) ? 0.5 : dist });
        } else if (toolName === 'read_depth' || toolName === 'depth') {
          actionResult = await callEngine('depth');
        } else if (toolName === 'pickup') {
          actionResult = await callEngine('pickup');
        } else if (toolName === 'drop') {
          actionResult = await callEngine('drop');
        } else if (toolName === 'done') {
          actionResult = { ok: true };
          broadcast({ state: 'COMPLETED', step, tool: 'done', observations, latencyMs: elapsedMs });
          break;
        }

        // Fetch post-action telemetry
        const postState = await callEngine('state');
        const postDepth = await callEngine('depth');
        const latestDepth = postDepth.depth;
        const latestHolding = postState.holding;

        broadcast({
          state: 'STEP_DONE',
          step,
          tool: toolName,
          args: toolArgs,
          observations,
          confidence,
          latencyMs: elapsedMs,
          depth: latestDepth,
          holding: latestHolding,
          actionResult
        });

        // Fresh look frame if moved or turned
        if (toolName === 'move' || toolName === 'turn') {
          const freshLook = await callEngine('look');
          base64Img = freshLook.jpegBase64 || freshLook;
          if (typeof base64Img === 'string' && base64Img.startsWith('data:image')) {
            base64Img = base64Img.split(',')[1];
          }
        }

        const toolReplyPayload = {
          result: actionResult,
          current_laser_depth: latestDepth,
          holding_object: latestHolding
        };
        if (toolName === 'pickup' && !actionResult.ok) {
          toolReplyPayload.hint = `Pickup failed. Current laser depth is ${latestDepth}m. You must be <= 0.55m from the target object to grasp it. Drive closer first.`;
        }

        messages.push({
          role: "tool",
          tool_call_id: callId,
          name: toolName,
          content: JSON.stringify(toolReplyPayload)
        });

        if (toolName === 'move' || toolName === 'turn') {
          messages.push({
            role: "user",
            content: [
              { type: "text", text: `Onboard camera view after ${toolName}: (Center depth: ${latestDepth !== null ? latestDepth.toFixed(2) + 'm' : 'null'}, holding: ${latestHolding})` },
              { type: "image_url", image_url: { url: `data:image/jpeg;base64,${base64Img}`, detail: "low" } }
            ]
          });
        }
      }
    } catch (err) {
      console.error('[Antigravity Agent Fatal Error]:', err);
      broadcast({ state: 'ERROR', error: err.message });
    } finally {
      isAntigravityRunning = false;
    }
  })();
});

// ==================== POLLINATIONS AI AGENT RUNNER ====================
let isPollinationsRunning = false;
let abortPollinations = false;

// GET /api/pollinations/status
agentApp.get('/api/pollinations/status', (req, res) => {
  res.json({
    hasEnvKey: Boolean(process.env.POLLINATIONS_API_KEY),
    isRunning: isPollinationsRunning
  });
});

// POST /api/pollinations/stop
agentApp.post('/api/pollinations/stop', (req, res) => {
  abortPollinations = true;
  isPollinationsRunning = false;
  res.json({ ok: true, status: 'Stopped' });
});

// POST /api/pollinations/start
agentApp.post('/api/pollinations/start', async (req, res) => {
  if (isPollinationsRunning) {
    return res.status(409).json({ error: 'A Pollinations run is already active' });
  }

  const apiKey = req.body.apiKey || process.env.POLLINATIONS_API_KEY;
  if (!apiKey) {
    return res.status(400).json({ error: 'Pollinations API key is required' });
  }

  const command = req.body.command || 'Bring the round red thing';
  const model = req.body.model || 'openai/gpt-4o-mini';

  isPollinationsRunning = true;
  abortPollinations = false;

  res.json({ ok: true, status: 'Started', command, model });

  (async () => {
    try {
      console.log(`[Pollinations Agent] Starting run with '${model}' for: "${command}"`);
      await callEngine('setCommand', { text: command });

      const broadcast = (data) => {
        if (engineSocket && engineSocket.readyState === 1) {
          engineSocket.send(JSON.stringify({ type: 'pollinations_update', ...data }));
        }
      };

      const MAX_STEPS = 25;
      let frame = await callEngine('look');
      let base64Img = frame.jpegBase64 || frame;
      if (typeof base64Img === 'string' && base64Img.startsWith('data:image')) {
        base64Img = base64Img.split(',')[1];
      }

      const messages = [
        {
          role: 'system',
          content: `You are the autonomous navigation controller of a mobile robot.
Your physical specifications:
- Room size: 6.0m x 6.0m arena. Floor grid lines form 0.5m squares ("blocks"). 
  Scale: 1 block = 0.5m, 2 blocks = 1.0m, 4 blocks = 2.0m.
- Forward camera: 90° horizontal field of view (640x480 resolution). 
  Center pixel is x=320. 1° of rotation spans ~7 pixels horizontally.
  Clockwise (right) is positive degrees, counter-clockwise (left) is negative degrees.
- Depth sensor: center laser ray measures distance straight ahead up to 10.0m.

Mission command: "${command}"

Available Actions & Dynamic Parameters:
1. "turn": {"degrees": <number>}
   - Turn by ANY exact degree value. E.g. {"degrees": -15} for slight left, {"degrees": 30} for right, {"degrees": 180} for turnaround.
2. "move": {"distance": <number>}
   - Drive forward by ANY exact distance in metres. E.g. {"distance": 0.5} (1 block), {"distance": 1.2} (2.4 blocks).
   - If you hit an obstacle, it safely stops and returns {"moved": ..., "collided": true}.
3. "read_depth": {}
   - Measures exact distance in metres to whatever is centered in view.
4. "pickup": {}
   - Grasp object directly ahead when distance <= 0.55m.
5. "drop": {}
   - Release held object into the green floor zone.
6. "done": {}
   - End mission once successfully delivered.

Optimal Strategy:
- Center the target horizontally in camera view using fine-tuned turns.
- Check depth! If center depth is > 0.55m (e.g. 1.0m, 2.0m, 3.0m), DO NOT call "pickup"! Drive forward with "move" (e.g. {"distance": 1.0} or {"distance": 1.5}) to approach the target.
- ONLY call "pickup" once you are right next to the object and center depth is <= 0.55m!
- Once holding object, rotate to spot the flat green square drop zone, navigate onto it, call "drop", then "done".

CRITICAL: Respond ONLY with a valid JSON object in this exact format:
{
  "tool": "turn" | "move" | "read_depth" | "pickup" | "drop" | "done",
  "args": { "degrees": 25 } or { "distance": 1.0 } or {},
  "observations": "Brief visual description of what you see in the frame",
  "target": "Target object or green zone",
  "confidence": 0.95
}`
        },
        {
          role: 'user',
          content: [
            { type: 'text', text: `Command: "${command}". What is your next action?` },
            { type: 'image_url', image_url: { url: `data:image/jpeg;base64,${base64Img}`, detail: 'high' } }
          ]
        }
      ];

      for (let step = 1; step <= MAX_STEPS; step++) {
        if (abortPollinations) {
          console.log('[Pollinations Agent] Aborted.');
          broadcast({ state: 'ABORTED', step });
          break;
        }

        const depthData = await callEngine('depth');
        const stateData = await callEngine('state');
        const currentDepth = depthData.depth;
        const isHolding = stateData.holding;

        broadcast({ state: 'THINKING', step, depth: currentDepth, holding: isHolding });
        const startTime = Date.now();

        // Call Pollinations OpenAI-compatible endpoint
        const pollResp = await fetch('https://gen.pollinations.ai/v1/chat/completions', {
          method: 'POST',
          headers: {
            'Authorization': `Bearer ${apiKey}`,
            'Content-Type': 'application/json'
          },
          body: JSON.stringify({
            model: model,
            messages: messages,
            temperature: 0.1
          })
        });

        if (!pollResp.ok) {
          const errText = await pollResp.text();
          console.error('[Pollinations Error]:', pollResp.status, errText);
          broadcast({ state: 'ERROR', error: `HTTP ${pollResp.status}: ${errText}` });
          break;
        }

        const pollData = await pollResp.json();
        const rawContent = pollData.choices?.[0]?.message?.content || '';
        const elapsedMs = Date.now() - startTime;

        let parsed = null;
        const jsonMatch = rawContent.match(/\{[\s\S]*\}/);
        if (jsonMatch) {
          try { parsed = JSON.parse(jsonMatch[0]); } catch (e) {}
        }

        if (!parsed || !parsed.tool) {
          parsed = { tool: 'turn', args: { degrees: 45 }, observations: rawContent, confidence: 0.5 };
        }

        const toolName = parsed.tool;
        const toolArgs = parsed.args || {};

        console.log(`[Pollinations Step ${step}] ${toolName}`, toolArgs, `(Latency: ${elapsedMs}ms)`);

        let actionResult = null;
        if (toolName === 'turn') {
          const deg = toolArgs.degrees !== undefined ? Number(toolArgs.degrees) : (toolArgs.angle !== undefined ? Number(toolArgs.angle) : 45);
          actionResult = await callEngine('turn', { degrees: isNaN(deg) ? 45 : deg });
        } else if (toolName === 'move') {
          const dist = toolArgs.distance !== undefined ? Number(toolArgs.distance) : (toolArgs.amount !== undefined ? Number(toolArgs.amount) : 0.5);
          actionResult = await callEngine('move', { distance: isNaN(dist) ? 0.5 : dist });
        } else if (toolName === 'read_depth' || toolName === 'depth') {
          actionResult = await callEngine('depth');
        } else if (toolName === 'pickup') {
          actionResult = await callEngine('pickup');
        } else if (toolName === 'drop') {
          actionResult = await callEngine('drop');
        } else if (toolName === 'done') {
          actionResult = { ok: true };
          broadcast({ state: 'COMPLETED', step, tool: 'done', rawOutput: rawContent });
          break;
        }

        // Fetch fresh state & depth after action execution
        const postState = await callEngine('state');
        const postDepth = await callEngine('depth');
        const latestDepth = postDepth.depth;
        const latestHolding = postState.holding;

        // Fetch fresh look frame if moved or turned
        if (toolName === 'move' || toolName === 'turn') {
          const freshLook = await callEngine('look');
          base64Img = freshLook.jpegBase64 || freshLook;
          if (typeof base64Img === 'string' && base64Img.startsWith('data:image')) {
            base64Img = base64Img.split(',')[1];
          }
        }

        // Construct clear, feedback-rich prompt for vision model
        let feedbackText = `Action result: ${JSON.stringify(actionResult)}. Current center depth: ${latestDepth !== null ? latestDepth.toFixed(2) + 'm' : 'null (open space)'}, holding: ${latestHolding}.`;
        if (toolName === 'pickup' && !actionResult.ok) {
          feedbackText += ` [ATTENTION: Pickup failed! The robot can only grasp objects when center depth <= 0.55m. You are currently ${latestDepth !== null ? latestDepth.toFixed(2) + 'm' : 'too far'} away. You MUST call "move" to drive closer before calling pickup!]`;
        }

        // Update conversation message
        messages.push({ role: 'assistant', content: rawContent });
        messages.push({
          role: 'user',
          content: [
            { type: 'text', text: `${feedbackText} What is your next action JSON?` },
            { type: 'image_url', image_url: { url: `data:image/jpeg;base64,${base64Img}`, detail: 'high' } }
          ]
        });

        // Prune older images to save tokens
        if (messages.length > 5) {
          for (let i = 1; i < messages.length - 2; i++) {
            if (Array.isArray(messages[i].content)) {
              messages[i].content = messages[i].content.filter(p => p.type !== 'image_url');
            }
          }
        }

        broadcast({
          state: 'STEP_DONE',
          step,
          tool: toolName,
          args: toolArgs,
          result: actionResult,
          rawOutput: rawContent,
          latencyMs: elapsedMs,
          thumbnail: base64Img ? `data:image/jpeg;base64,${base64Img}` : null,
          observations: parsed.observations,
          confidence: parsed.confidence
        });

        await new Promise(r => setTimeout(r, 600));
      }

      const finalResult = await callEngine('getResult');
      broadcast({ state: 'FINISHED', result: finalResult });

    } catch (err) {
      console.error('[Pollinations Loop Error]:', err);
    } finally {
      isPollinationsRunning = false;
    }
  })();
});

// ==================== OLLAMA LOCAL AGENT RUNNER ====================
let isOllamaRunning = false;
let abortOllama = false;

// GET /api/ollama/models - Fetch and list local models
agentApp.get('/api/ollama/models', async (req, res) => {
  try {
    const fetchResp = await fetch('http://127.0.0.1:11434/api/tags');
    if (!fetchResp.ok) {
      return res.status(502).json({ error: 'Ollama is not responding at 127.0.0.1:11434' });
    }
    const data = await fetchResp.json();
    const chatModels = (data.models || [])
      .filter(m => !m.name.includes('reranker') && !m.name.includes('embed') && !m.name.includes('MiniLM'))
      .map(m => ({
        name: m.name,
        size: m.size ? `${(m.size / (1024 * 1024 * 1024)).toFixed(1)} GB` : 'Local',
        vision: (m.details?.families || []).some(f => f && (f.includes('vl') || f.includes('vision') || f.includes('moondream'))) || m.name.includes('vision') || m.name.includes('moondream') || m.name.includes('vl')
      }));
    res.json({ models: chatModels });
  } catch (err) {
    res.status(500).json({ error: 'Failed to contact Ollama: ' + err.message });
  }
});

// POST /api/ollama/stop
agentApp.post('/api/ollama/stop', (req, res) => {
  abortOllama = true;
  isOllamaRunning = false;
  res.json({ ok: true, status: 'Stopped' });
});

// POST /api/ollama/start
agentApp.post('/api/ollama/start', async (req, res) => {
  if (isOllamaRunning) {
    return res.status(409).json({ error: 'An Ollama autonomous run is already in progress' });
  }

  const command = req.body.command || 'Bring the round red thing';
  const model = req.body.model || 'moondream';

  isOllamaRunning = true;
  abortOllama = false;

  res.json({ ok: true, status: 'Started', command, model });

  // Run async loop in background
  (async () => {
    try {
      console.log(`[Ollama Agent] Starting run with model '${model}' for command: "${command}"`);
      await callEngine('setCommand', { text: command });

      const broadcast = (data) => {
        if (engineSocket && engineSocket.readyState === 1) {
          engineSocket.send(JSON.stringify({ type: 'ollama_update', ...data }));
        }
      };

      const MAX_STEPS = 30;
      let lastCollided = false;
      let consecutiveTurns = 0;

      for (let step = 1; step <= MAX_STEPS; step++) {
        if (abortOllama) {
          console.log('[Ollama Agent] Run aborted.');
          broadcast({ state: 'ABORTED', step });
          break;
        }

        // 1. Gather Telemetry & Vision
        const lookData = await callEngine('look');
        let base64Img = lookData.jpegBase64 || lookData;
        if (typeof base64Img === 'string' && base64Img.startsWith('data:image')) {
          base64Img = base64Img.split(',')[1];
        }

        const depthData = await callEngine('depth');
        const stateData = await callEngine('state');
        const currentDepth = depthData.depth;
        const isHolding = stateData.holding;

        // 2. Build Vision Prompt suited for Lightweight VLMs
        let promptText = '';
        if (!isHolding) {
          promptText = `Describe the position of the object matching "${command}" in this camera view. Is it visible on the left, center, right, or not visible?`;
        } else {
          promptText = `Describe the position of the green square drop zone on the floor. Is it visible on the left, center, right, or not visible?`;
        }

        broadcast({
          state: 'THINKING',
          step,
          depth: currentDepth,
          holding: isHolding
        });

        // 3. Call Ollama API
        let parsed = null;
        let rawResponseText = '';
        const startTime = Date.now();

        try {
          const reqPayload = {
            model: model,
            prompt: promptText,
            stream: false,
            options: {
              temperature: 0.1,
              num_predict: 250
            }
          };

          if (base64Img) {
            reqPayload.images = [base64Img];
          }

          const ollamaResp = await fetch('http://127.0.0.1:11434/api/generate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(reqPayload)
          });

          if (ollamaResp.ok) {
            const respData = await ollamaResp.json();
            rawResponseText = respData.response || '';

            // Try to extract JSON
            const jsonMatch = rawResponseText.match(/\{[\s\S]*\}/);
            if (jsonMatch) {
              try {
                parsed = JSON.parse(jsonMatch[0]);
              } catch (e) {
                console.warn('[Ollama Agent] JSON parse failed on match:', jsonMatch[0]);
              }
            }

            // Natural Language Understanding Fallback for lightweight VLMs (e.g. Moondream)
            if (!parsed || !parsed.tool) {
              const lower = rawResponseText.toLowerCase();
              console.log(`[Ollama Agent NLU Fallback] Analyzing raw text for command "${command}":`, lower);

              const cmdLower = command.toLowerCase();
              let targetDetected = false;

              // Check affirmative replies
              if (lower.includes('yes') || lower.includes('center') || lower.includes('middle') || lower.includes('ahead')) {
                targetDetected = true;
              }

              // Check if target keywords or colors are described in the scene
              if (cmdLower.includes('red') && lower.includes('red')) targetDetected = true;
              else if (cmdLower.includes('blue') && lower.includes('blue')) targetDetected = true;
              else if (cmdLower.includes('yellow') && (lower.includes('yellow') || lower.includes('can') || lower.includes('drink'))) targetDetected = true;
              else if (cmdLower.includes('orange') && (lower.includes('orange') || lower.includes('cone'))) targetDetected = true;
              else if (cmdLower.includes('cone') && lower.includes('cone')) targetDetected = true;
              else if (cmdLower.includes('sphere') && (lower.includes('sphere') || lower.includes('ball') || lower.includes('circle'))) targetDetected = true;
              else if (cmdLower.includes('cube') && (lower.includes('cube') || lower.includes('box'))) targetDetected = true;

              // Explicit negative overrides
              if (lower.includes('not visible') || lower.includes('none') || lower === 'no') {
                targetDetected = false;
              }

              if (!isHolding) {
                if (targetDetected) {
                  // Determine horizontal placement
                  if (lower.includes('left')) {
                    parsed = {
                      observations: [rawResponseText.trim()],
                      target: command,
                      confidence: 0.85,
                      tool: 'turn',
                      args: { degrees: -25 }
                    };
                  } else if (lower.includes('right')) {
                    parsed = {
                      observations: [rawResponseText.trim()],
                      target: command,
                      confidence: 0.85,
                      tool: 'turn',
                      args: { degrees: 25 }
                    };
                  } else {
                    // Center or visible ahead
                    if (currentDepth !== null && currentDepth <= 0.55) {
                      parsed = {
                        observations: [rawResponseText.trim()],
                        target: command,
                        confidence: 0.95,
                        tool: 'pickup',
                        args: {}
                      };
                    } else {
                      const dist = currentDepth !== null ? Math.min(Math.max(0.3, currentDepth * 0.5), 1.2) : 0.5;
                      parsed = {
                        observations: [rawResponseText.trim()],
                        target: command,
                        confidence: 0.85,
                        tool: 'move',
                        args: { distance: dist }
                      };
                    }
                  }
                } else {
                  // Target not yet in frame, turn 45 degrees to sweep room
                  parsed = {
                    observations: [rawResponseText.trim() || 'Target not visible in this angle'],
                    target: command,
                    confidence: 0.4,
                    tool: 'turn',
                    args: { degrees: 45 }
                  };
                }
              } else {
                // Robot is holding the object -> find green drop zone
                if (lower.includes('green') || lower.includes('zone') || lower.includes('square')) {
                  if (lower.includes('left')) {
                    parsed = { observations: [rawResponseText.trim()], target: 'green zone', confidence: 0.9, tool: 'turn', args: { degrees: -25 } };
                  } else if (lower.includes('right')) {
                    parsed = { observations: [rawResponseText.trim()], target: 'green zone', confidence: 0.9, tool: 'turn', args: { degrees: 25 } };
                  } else {
                    // Near or at zone
                    parsed = { observations: [rawResponseText.trim()], target: 'green zone', confidence: 0.95, tool: 'drop', args: {} };
                  }
                } else {
                  parsed = {
                    observations: [rawResponseText.trim() || 'Searching for green drop zone'],
                    target: 'green zone',
                    confidence: 0.5,
                    tool: 'turn',
                    args: { degrees: 50 }
                  };
                }
              }
            }
          } else {
            rawResponseText = `HTTP Error from Ollama: ${ollamaResp.status} ${ollamaResp.statusText}`;
          }
        } catch (err) {
          console.error('[Ollama Agent] API call error:', err);
          rawResponseText = `Error calling Ollama API: ${err.message}`;
        }

        const elapsedMs = Date.now() - startTime;

        // Collision recovery: if last move collided with an obstacle (e.g. orange cone), detour around it
        if (lastCollided) {
          console.log('[Ollama Agent] Collision detected! Executing obstacle detour maneuver.');
          parsed = {
            observations: ['Obstacle detected ahead, steering to clear line of sight'],
            target: command,
            confidence: 0.7,
            tool: 'turn',
            args: { degrees: 45 }
          };
          lastCollided = false;
        }

        if (!parsed) {
          parsed = {
            observations: ['Fallback scan'],
            target: command,
            confidence: 0.3,
            tool: 'turn',
            args: { degrees: 45 }
          };
        }

        const toolName = parsed.tool;
        const toolArgs = parsed.args || {};

        console.log(`[Ollama Agent] Step ${step}: ${toolName}`, toolArgs, `(Latency: ${elapsedMs}ms)`);

        let actionResult = null;
        lastCollided = false;

        // Execute action on engine
        if (toolName === 'turn') {
          actionResult = await callEngine('turn', { degrees: Number(toolArgs.degrees || 45) });
        } else if (toolName === 'move') {
          actionResult = await callEngine('move', { distance: Number(toolArgs.distance || 0.4) });
          lastCollided = actionResult.collided;
        } else if (toolName === 'pickup') {
          actionResult = await callEngine('pickup');
        } else if (toolName === 'drop') {
          actionResult = await callEngine('drop');
        } else if (toolName === 'done') {
          actionResult = { ok: true };
          broadcast({
            state: 'COMPLETED',
            step,
            tool: 'done',
            rawOutput: rawResponseText,
            latencyMs: elapsedMs,
            observations: parsed.observations,
            target: parsed.target
          });
          break;
        }

        broadcast({
          state: 'STEP_DONE',
          step,
          tool: toolName,
          args: toolArgs,
          result: actionResult,
          rawOutput: rawResponseText,
          latencyMs: elapsedMs,
          thumbnail: base64Img ? `data:image/jpeg;base64,${base64Img}` : null,
          observations: parsed.observations,
          target: parsed.target,
          confidence: parsed.confidence
        });

        // Delay between steps for smooth visualization
        await new Promise(r => setTimeout(r, 600));
      }

      const finalResult = await callEngine('getResult');
      console.log('[Ollama Agent] Run complete, outcome:', finalResult);
      broadcast({ state: 'FINISHED', result: finalResult });

    } catch (loopErr) {
      console.error('[Ollama Agent Loop Error]:', loopErr);
    } finally {
      isOllamaRunning = false;
    }
  })();
});

// ==================== HTTP & WEBSOCKET SERVERS ====================

const agentServer = http.createServer(agentApp);
const wss = new WebSocketServer({ server: agentServer, path: '/ws' });

wss.on('connection', (ws) => {
  console.log('[WebSocket] Engine browser connected.');
  engineSocket = ws;

  ws.on('message', (message) => {
    try {
      const data = JSON.parse(message.toString());
      const { id, ok, result, error } = data;
      if (!id || !pendingRequests.has(id)) return;

      const { resolve, reject, timer } = pendingRequests.get(id);
      clearTimeout(timer);
      pendingRequests.delete(id);

      if (ok) {
        resolve(result);
      } else {
        reject(new Error(error || 'Engine returned error'));
      }
    } catch (err) {
      console.error('[WebSocket] Failed to parse message from engine:', err);
    }
  });

  ws.on('close', () => {
    console.log('[WebSocket] Engine browser disconnected.');
    if (engineSocket === ws) {
      engineSocket = null;
    }
  });
});

const AGENT_PORT = 8080;
const ADMIN_PORT = 8081;

agentServer.listen(AGENT_PORT, () => {
  console.log(`====================================================`);
  console.log(`🤖 Engine Relay & Agent API running at http://localhost:${AGENT_PORT}`);
  console.log(`   Browser Engine UI: http://localhost:${AGENT_PORT}/`);
  console.log(`====================================================`);
});

adminApp.listen(ADMIN_PORT, () => {
  console.log(`🔒 Admin API running at http://localhost:${ADMIN_PORT}`);
});

