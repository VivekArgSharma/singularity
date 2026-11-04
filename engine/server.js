import express from 'express';
import { WebSocketServer } from 'ws';
import http from 'http';
import path from 'path';
import { fileURLToPath } from 'url';
import crypto from 'crypto';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const rootDir = path.resolve(__dirname, '..');

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
        const promptText = `You are the eyes of a mobile robot.
User mission: "${command}"
Currently holding object: ${isHolding ? 'YES (carrying object to green zone)' : 'NO (looking for target)'}

Describe what objects, colors, and shapes you see in this camera view.
Explicitly state:
1. Do you see the target object matching "${command}"? If yes, is it on the LEFT, CENTER, or RIGHT of your view?
2. Do you see the green square drop zone on the floor? If yes, where is it (left, center, right)?`;

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

              // Check if target keywords are described in the scene
              if (cmdLower.includes('red') && lower.includes('red')) targetDetected = true;
              else if (cmdLower.includes('blue') && lower.includes('blue')) targetDetected = true;
              else if (cmdLower.includes('yellow') && (lower.includes('yellow') || lower.includes('can') || lower.includes('drink'))) targetDetected = true;
              else if (cmdLower.includes('orange') && (lower.includes('orange') || lower.includes('cone'))) targetDetected = true;
              else if (cmdLower.includes('cone') && lower.includes('cone')) targetDetected = true;
              else if (cmdLower.includes('sphere') && (lower.includes('sphere') || lower.includes('ball') || lower.includes('circle'))) targetDetected = true;
              else if (cmdLower.includes('cube') && (lower.includes('cube') || lower.includes('box'))) targetDetected = true;

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

