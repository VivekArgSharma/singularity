import * as THREE from 'three';
import { loadConfig, buildWorld, inZone } from './world.js';
import { Robot } from './robot.js';
import { ViewsManager } from './views.js';
import { Grader } from './grader.js';
import { ClipRecorder } from './recorder.js';

let scene, robot, world, views, grader, recorder;
let ws = null;
let lastDroppedObj = null;

async function init() {
  const logEl = document.getElementById('eventLog');
  const addLog = (msg) => {
    if (!logEl) return;
    const time = new Date().toLocaleTimeString();
    const line = document.createElement('div');
    line.className = 'log-item';
    line.innerHTML = `<span class="log-time">[${time}]</span> ${msg}`;
    logEl.prepend(line);
  };

  // 1. Setup Three.js Scene
  scene = new THREE.Scene();
  scene.background = new THREE.Color('#0f172a');

  // 2. Load Config & Build World
  const config = await loadConfig();
  world = buildWorld(config, scene);
  robot = new Robot(config, scene, world);
  views = new ViewsManager(scene, robot, world);
  grader = new Grader(world);
  recorder = new ClipRecorder(views);

  addLog('World initialized with 6 primitive shapes & green drop zone.');

  // 3. Setup WebSocket Client
  function connectWs() {
    const wsUrl = `ws://${window.location.host}/ws`;
    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      addLog('<span style="color:#34d399">WebSocket connected to Relay</span>');
      const wsStatus = document.getElementById('wsStatus');
      if (wsStatus) {
        wsStatus.textContent = 'ONLINE';
        wsStatus.className = 'pill-online';
      }
    };

    ws.onclose = () => {
      addLog('<span style="color:#f43f5e">WebSocket disconnected. Retrying in 2s...</span>');
      const wsStatus = document.getElementById('wsStatus');
      if (wsStatus) {
        wsStatus.textContent = 'OFFLINE';
        wsStatus.className = 'pill-offline';
      }
      setTimeout(connectWs, 2000);
    };

    ws.onerror = (err) => {
      console.error('WS Error:', err);
    };

    ws.onmessage = async (event) => {
      try {
        const msg = JSON.parse(event.data);

        // Handle Antigravity, Ollama & Pollinations autonomous agent live broadcast updates
        if (msg.type === 'antigravity_update' || msg.type === 'ollama_update' || msg.type === 'pollinations_update') {
          const agentName = msg.type === 'antigravity_update' ? 'Antigravity' : (msg.type === 'pollinations_update' ? 'Pollinations' : 'Ollama');
          handleAgentUpdate(msg, agentName);
          return;
        }

        const { id, op, args } = msg;

        let result = null;
        let ok = true;
        let error = null;

        views.updateHud(op, args);

        switch (op) {
          case 'state':
            result = robot.getState();
            break;

          case 'look':
            const jpegData = views.renderPovJpeg();
            result = { jpegBase64: jpegData };
            break;

          case 'depth':
            result = robot.depth();
            break;

          case 'move':
            recorder.resumeClip();
            result = await robot.move(args.distance);
            recorder.pauseClip();
            addLog(`Moved: ${result.moved}m, Collided: ${result.collided}`);
            break;

          case 'turn':
            recorder.resumeClip();
            result = await robot.turn(args.degrees);
            recorder.pauseClip();
            addLog(`Turned to heading: ${result.heading}°`);
            break;

          case 'pickup':
            result = robot.pickup();
            addLog(`Pickup: ${result.ok ? 'SUCCESS' : 'FAILED'}`);
            break;

          case 'drop':
            lastDroppedObj = robot.holding;
            result = robot.drop();
            addLog(`Drop: ok=${result.ok}, in_zone=${result.in_zone}`);
            // Check if recording is active and stop after brief hold
            if (recorder.isRecording) {
              setTimeout(async () => {
                const blob = await recorder.stopClip();
                if (blob) recorder.downloadClip(blob, `robot_run_${Date.now()}.webm`);
              }, 1500);
            }
            break;

          case 'setCommand':
            robot.setCommand(args.text);
            grader.manualVerdict = null;
            lastDroppedObj = null;
            addLog(`Command set: <b>"${args.text}"</b>`);
            recorder.startClip(args.text);
            result = { ok: true, command: args.text };
            break;

          case 'reset':
            robot.reset();
            grader.manualVerdict = null;
            lastDroppedObj = null;
            views.trail = [];
            addLog('Robot and scene reset to default');
            result = { ok: true };
            break;

          case 'giveUp':
            result = robot.giveUp();
            addLog('Harness called giveUp');
            break;

          case 'getResult':
            const outcome = grader.grade(
              lastDroppedObj || robot.holding,
              lastDroppedObj ? inZone(lastDroppedObj.currentPos, world) : false,
              robot.retries,
              robot.commandText
            );
            result = {
              outcome: outcome,
              steps: robot.stats.steps,
              simSeconds: Math.round(robot.stats.simSeconds * 10) / 10,
              collisions: robot.stats.collisions,
              pathLength: Math.round(robot.stats.pathLength * 100) / 100
            };
            addLog(`Grader evaluated run: <b style="color:${outcome === 'pass' ? '#34d399' : '#f43f5e'}">${outcome}</b>`);
            break;

          case 'manualVerdict':
            grader.applyManualVerdict(args.pass);
            addLog(`Manual judge verdict set to: ${args.pass ? 'PASS' : 'FAIL'}`);
            result = { ok: true, verdict: args.pass };
            break;

          default:
            ok = false;
            error = `Unknown operation: ${op}`;
        }

        ws.send(JSON.stringify({ id, ok, result, error }));
      } catch (err) {
        console.error('WS handler error:', err);
      }
    };
  }

  connectWs();

  // 4. Keyboard Controls for Debugging & Manual Driving
  window.addEventListener('keydown', async (e) => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;

    if (e.key === 'ArrowUp') {
      e.preventDefault();
      await robot.move(0.3);
      views.updateHud('move', { distance: 0.3 });
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      await robot.move(-0.3);
      views.updateHud('move', { distance: -0.3 });
    } else if (e.key === 'ArrowLeft') {
      e.preventDefault();
      await robot.turn(-20);
      views.updateHud('turn', { degrees: -20 });
    } else if (e.key === 'ArrowRight') {
      e.preventDefault();
      await robot.turn(20);
      views.updateHud('turn', { degrees: 20 });
    } else if (e.key === ' ') {
      e.preventDefault();
      if (robot.holding) {
        lastDroppedObj = robot.holding;
        robot.drop();
      } else {
        robot.pickup();
      }
      views.updateHud();
    } else if (e.key === 'l' || e.key === 'L') {
      views.showLabels = !views.showLabels;
    } else if (e.key === '1') {
      views.viewMode = 'pov';
      document.querySelectorAll('.view-btn').forEach(b => b.classList.remove('active'));
      document.getElementById('btnPov')?.classList.add('active');
    } else if (e.key === '2') {
      views.viewMode = 'follow';
      document.querySelectorAll('.view-btn').forEach(b => b.classList.remove('active'));
      document.getElementById('btnFollow')?.classList.add('active');
    } else if (e.key === '3') {
      views.viewMode = 'orbit';
      document.querySelectorAll('.view-btn').forEach(b => b.classList.remove('active'));
      document.getElementById('btnOrbit')?.classList.add('active');
    } else if (e.key === 'y' || e.key === 'Y') {
      grader.applyManualVerdict(true);
      addLog('Judge Key Pressed: <b>PASS (Y)</b>');
    } else if (e.key === 'n' || e.key === 'N') {
      grader.applyManualVerdict(false);
      addLog('Judge Key Pressed: <b>FAIL (N)</b>');
    }
  });

  // UI Button Bindings
  document.getElementById('btnReset')?.addEventListener('click', () => robot.reset());
  document.getElementById('btnLabels')?.addEventListener('click', () => {
    views.showLabels = !views.showLabels;
  });
  document.getElementById('btnJudgePass')?.addEventListener('click', () => {
    grader.applyManualVerdict(true);
    addLog('Judge: Manual PASS applied');
  });
  document.getElementById('btnJudgeFail')?.addEventListener('click', () => {
    grader.applyManualVerdict(false);
    addLog('Judge: Manual FAIL applied');
  });

  document.getElementById('btnPov')?.addEventListener('click', () => {
    views.viewMode = 'pov';
    document.querySelectorAll('.view-btn').forEach(b => b.classList.remove('active'));
    document.getElementById('btnPov').classList.add('active');
  });
  document.getElementById('btnFollow')?.addEventListener('click', () => {
    views.viewMode = 'follow';
    document.querySelectorAll('.view-btn').forEach(b => b.classList.remove('active'));
    document.getElementById('btnFollow').classList.add('active');
  });
  document.getElementById('btnOrbit')?.addEventListener('click', () => {
    views.viewMode = 'orbit';
    document.querySelectorAll('.view-btn').forEach(b => b.classList.remove('active'));
    document.getElementById('btnOrbit').classList.add('active');
  });

  document.getElementById('btnSetCmd')?.addEventListener('click', () => {
    const input = document.getElementById('inputCommand');
    if (input && input.value.trim()) {
      robot.setCommand(input.value.trim());
      recorder.startClip(input.value.trim());
      addLog(`Command set via UI: "${input.value.trim()}"`);
    }
  });

  // ==================== OLLAMA CONTROLLER UI LOGIC ====================
  const ollamaSelect = document.getElementById('ollamaModelSelect');
  const ollamaInput = document.getElementById('ollamaCommandInput');
  const btnOllamaStart = document.getElementById('btnOllamaStart');
  const btnOllamaStop = document.getElementById('btnOllamaStop');
  const ollamaStatusText = document.getElementById('ollamaStatusText');
  const ollamaThoughtBox = document.getElementById('ollamaThoughtBox');
  const ollamaStepBadge = document.getElementById('ollamaStepBadge');
  const ollamaConfBadge = document.getElementById('ollamaConfBadge');
  const ollamaObservations = document.getElementById('ollamaObservations');
  const ollamaActionPlanned = document.getElementById('ollamaActionPlanned');

  // Fetch local Ollama models on page load
  async function loadOllamaModels() {
    try {
      const res = await fetch('/api/ollama/models');
      if (res.ok) {
        const data = await res.json();
        if (data.models && data.models.length > 0) {
          ollamaSelect.innerHTML = '';
          data.models.forEach(m => {
            const opt = document.createElement('option');
            opt.value = m.name;
            opt.textContent = `${m.name} (${m.size}${m.vision ? ' - Vision' : ''})`;
            ollamaSelect.appendChild(opt);
          });
          // Prefer vision model if available
          const visModel = data.models.find(m => m.vision || m.name.includes('moondream') || m.name.includes('vision'));
          if (visModel) {
            ollamaSelect.value = visModel.name;
          }
        }
      }
    } catch (e) {
      console.warn('Could not load Ollama models:', e);
    }
  }

  loadOllamaModels();

  async function startOllamaRun() {
    const cmd = (ollamaInput ? ollamaInput.value : '').trim();
    if (!cmd) return;

    const model = ollamaSelect ? ollamaSelect.value : 'moondream';

    btnOllamaStart.style.display = 'none';
    btnOllamaStop.style.display = 'inline-block';
    ollamaThoughtBox.style.display = 'block';
    ollamaStatusText.textContent = `Running autonomously with ${model}...`;
    ollamaStatusText.style.color = '#38bdf8';
    ollamaObservations.textContent = 'Preparing vision and connecting to Ollama...';
    ollamaActionPlanned.textContent = 'Action: Starting episode';

    addLog(`<b>[Ollama]</b> Starting run with <b>${model}</b> for: "${cmd}"`);

    try {
      const resp = await fetch('/api/ollama/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command: cmd, model })
      });
      if (!resp.ok) {
        const err = await resp.json();
        addLog(`<span style="color:#f43f5e">[Ollama Error] ${err.error}</span>`);
        resetOllamaUi();
      }
    } catch (err) {
      addLog(`<span style="color:#f43f5e">[Ollama Network Error] ${err.message}</span>`);
      resetOllamaUi();
    }
  }

  function resetOllamaUi() {
    btnOllamaStart.style.display = 'inline-block';
    btnOllamaStop.style.display = 'none';
    ollamaStatusText.textContent = 'Select model & press Enter to start';
    ollamaStatusText.style.color = '#94a3b8';
  }

  btnOllamaStart?.addEventListener('click', startOllamaRun);
  btnOllamaStop?.addEventListener('click', async () => {
    addLog('[Ollama] User requested abort');
    await fetch('/api/ollama/stop', { method: 'POST' });
    resetOllamaUi();
  });

  // Pressing Enter in the input text box starts the Ollama run!
  ollamaInput?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      startOllamaRun();
    }
  });

  // ==================== QUICK PRESET CHIPS ====================
  document.querySelectorAll('.preset-chip').forEach(btn => {
    btn.addEventListener('click', () => {
      const cmd = btn.getAttribute('data-cmd');
      if (agCommandInput) agCommandInput.value = cmd;
      if (pollCommandInput) pollCommandInput.value = cmd;
      if (ollamaInput) ollamaInput.value = cmd;
      const cmdInput = document.getElementById('inputCommand');
      if (cmdInput) cmdInput.value = cmd;
      addLog(`Selected preset mission: "<b>${cmd}</b>"`);
    });
  });

  // ==================== AI TABS SWITCHER ====================
  const tabAg = document.getElementById('tabBtnAg');
  const tabManual = document.getElementById('tabBtnManual');
  const tabPoll = document.getElementById('tabBtnPoll');
  const tabOllama = document.getElementById('tabBtnOllama');
  const panelAg = document.getElementById('tabContentAg');
  const panelManual = document.getElementById('tabContentManual');
  const panelPoll = document.getElementById('tabContentPoll');
  const panelOllama = document.getElementById('tabContentOllama');

  function switchAiTab(activeBtn, activePanel) {
    [tabAg, tabManual, tabPoll, tabOllama].forEach(b => {
      if (b) {
        b.classList.remove('active');
        b.style.borderBottom = 'none';
        b.style.color = '#94a3b8';
      }
    });
    [panelAg, panelManual, panelPoll, panelOllama].forEach(p => { if (p) p.style.display = 'none'; });
    if (activeBtn) {
      activeBtn.classList.add('active');
      activeBtn.style.borderBottom = activeBtn === tabAg ? '2px solid #38bdf8' : '2px solid #f472b6';
      activeBtn.style.color = activeBtn === tabAg ? '#38bdf8' : '#fff';
    }
    if (activePanel) activePanel.style.display = 'block';
  }

  tabAg?.addEventListener('click', () => switchAiTab(tabAg, panelAg));
  tabManual?.addEventListener('click', () => switchAiTab(tabManual, panelManual));
  tabPoll?.addEventListener('click', () => switchAiTab(tabPoll, panelPoll));
  tabOllama?.addEventListener('click', () => switchAiTab(tabOllama, panelOllama));

  // ==================== ANTIGRAVITY CONTROLLER UI LOGIC ====================
  const agModelSelect = document.getElementById('agModelSelect');
  const agCommandInput = document.getElementById('agCommandInput');
  const btnAgStart = document.getElementById('btnAgStart');
  const btnAgStop = document.getElementById('btnAgStop');
  const agStatusText = document.getElementById('agStatusText');

  // Verify proxy connectivity on load
  async function checkAntigravityStatus() {
    try {
      const res = await fetch('/api/antigravity/status');
      if (res.ok) {
        const data = await res.json();
        if (data.connected) {
          if (agStatusText) {
            agStatusText.textContent = `🟢 Easy CLI Proxy Connected (${data.models.length} models)`;
            agStatusText.style.color = '#34d399';
          }
          if (agModelSelect && data.models && data.models.length > 0) {
            // Keep default selected if exists, otherwise populate
            const existingVals = Array.from(agModelSelect.options).map(o => o.value);
            data.models.forEach(m => {
              if (!existingVals.includes(m.id)) {
                const opt = document.createElement('option');
                opt.value = m.id;
                opt.textContent = `${m.id} (${m.owned_by || 'proxy'})`;
                agModelSelect.appendChild(opt);
              }
            });
          }
        } else {
          if (agStatusText) {
            agStatusText.textContent = '🟡 Proxy offline at 127.0.0.1:8317';
            agStatusText.style.color = '#fbbf24';
          }
        }
      }
    } catch (e) {
      if (agStatusText) {
        agStatusText.textContent = '🔴 Proxy connection error';
        agStatusText.style.color = '#f87171';
      }
    }
  }
  checkAntigravityStatus();

  async function startAntigravityRun() {
    const cmd = (agCommandInput ? agCommandInput.value : '').trim();
    if (!cmd) return;

    const model = agModelSelect ? agModelSelect.value : 'claude-sonnet-4-6';

    btnAgStart.style.display = 'none';
    btnAgStop.style.display = 'inline-block';
    agStatusText.textContent = `Running with ${model}...`;
    agStatusText.style.color = '#38bdf8';
    ollamaThoughtBox.style.display = 'block';

    addLog(`<b>[Antigravity]</b> Starting frontier vision run with <b>${model}</b> for: "${cmd}"`);

    try {
      const resp = await fetch('/api/antigravity/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          command: cmd,
          model: model,
          proxyUrl: 'http://127.0.0.1:8317/v1',
          apiKey: '12345'
        })
      });

      if (!resp.ok) {
        const err = await resp.json();
        alert('Could not start Antigravity run: ' + (err.error || resp.statusText));
        btnAgStart.style.display = 'inline-block';
        btnAgStop.style.display = 'none';
        agStatusText.textContent = 'Failed to start';
        agStatusText.style.color = '#f87171';
      }
    } catch (e) {
      console.error(e);
      alert('Error communicating with simulation server: ' + e.message);
      btnAgStart.style.display = 'inline-block';
      btnAgStop.style.display = 'none';
    }
  }

  btnAgStart?.addEventListener('click', startAntigravityRun);
  btnAgStop?.addEventListener('click', async () => {
    btnAgStop.disabled = true;
    agStatusText.textContent = 'Stopping...';
    await fetch('/api/antigravity/stop', { method: 'POST' });
    btnAgStart.style.display = 'inline-block';
    btnAgStop.style.display = 'none';
    btnAgStop.disabled = false;
    agStatusText.textContent = 'Stopped';
    agStatusText.style.color = '#94a3b8';
    addLog(`<b>[Antigravity]</b> Stopped by user.`);
  });

  agCommandInput?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      startAntigravityRun();
    }
  });

  // ==================== POLLINATIONS CONTROLLER UI LOGIC ====================
  const pollApiKeyInput = document.getElementById('pollApiKey');
  const pollModelSelect = document.getElementById('pollModelSelect');
  const pollCommandInput = document.getElementById('pollCommandInput');
  const btnPollStart = document.getElementById('btnPollStart');
  const btnPollStop = document.getElementById('btnPollStop');
  const pollStatusText = document.getElementById('pollStatusText');

  let envKeyAvailable = false;

  // Check if API key is configured in .env on server
  fetch('/api/pollinations/status')
    .then(r => r.json())
    .then(data => {
      if (data.hasEnvKey) {
        envKeyAvailable = true;
        if (!pollApiKeyInput.value) {
          pollApiKeyInput.placeholder = '✓ API Key loaded from .env (or override here)';
          pollStatusText.textContent = 'API Key active from .env';
          pollStatusText.style.color = '#34d399';
        }
      }
    })
    .catch(() => {});

  // Load saved Pollinations key from localStorage if available
  if (pollApiKeyInput) {
    const savedKey = localStorage.getItem('pollinations_api_key');
    if (savedKey) pollApiKeyInput.value = savedKey;
    pollApiKeyInput.addEventListener('input', () => {
      localStorage.setItem('pollinations_api_key', pollApiKeyInput.value.trim());
    });
  }

  async function startPollinationsRun() {
    const key = (pollApiKeyInput ? pollApiKeyInput.value : '').trim();
    if (!key && !envKeyAvailable) {
      alert('Please enter your Pollinations API key (sk_...) or set it in .env');
      pollApiKeyInput?.focus();
      return;
    }

    const cmd = (pollCommandInput ? pollCommandInput.value : '').trim();
    const model = pollModelSelect ? pollModelSelect.value : 'openai/gpt-4o';

    btnPollStart.style.display = 'none';
    btnPollStop.style.display = 'inline-block';
    pollStatusText.textContent = `Running with ${model}...`;
    pollStatusText.style.color = '#f472b6';
    ollamaThoughtBox.style.display = 'block';

    addLog(`<b>[Pollinations]</b> Starting run with <b>${model}</b> for: "${cmd}"`);

    try {
      const resp = await fetch('/api/pollinations/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command: cmd, apiKey: key, model })
      });
      if (!resp.ok) {
        const err = await resp.json();
        addLog(`<span style="color:#f43f5e">[Pollinations Error] ${err.error}</span>`);
        resetPollUi();
      }
    } catch (err) {
      addLog(`<span style="color:#f43f5e">[Pollinations Network Error] ${err.message}</span>`);
      resetPollUi();
    }
  }

  function resetPollUi() {
    btnPollStart.style.display = 'inline-block';
    btnPollStop.style.display = 'none';
    pollStatusText.textContent = 'Enter API key to unlock full vision';
    pollStatusText.style.color = '#94a3b8';
  }

  btnPollStart?.addEventListener('click', startPollinationsRun);
  btnPollStop?.addEventListener('click', async () => {
    addLog('[Pollinations] User requested abort');
    await fetch('/api/pollinations/stop', { method: 'POST' });
    resetPollUi();
  });

  pollCommandInput?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      startPollinationsRun();
    }
  });

  const ollamaRawStream = document.getElementById('ollamaRawStream');
  const debugLatency = document.getElementById('debugLatency');

  function handleAgentUpdate(msg, agentName = 'AI') {
    if (msg.state === 'THINKING') {
      ollamaStepBadge.textContent = `STEP ${msg.step}`;
      ollamaObservations.textContent = `Depth: ${msg.depth !== null ? msg.depth + 'm' : 'null'} | Analyzing camera frame with ${agentName}...`;
      ollamaActionPlanned.textContent = 'Action: Computing move...';
    } else if (msg.state === 'STEP_DONE') {
      ollamaStepBadge.textContent = `STEP ${msg.step}`;
      if (msg.confidence !== undefined) {
        ollamaConfBadge.textContent = `Confidence: ${(msg.confidence * 100).toFixed(0)}%`;
      }
      if (msg.observations) {
        const obsText = Array.isArray(msg.observations) ? msg.observations.join('; ') : String(msg.observations);
        ollamaObservations.textContent = `Vision: "${obsText}"`;
      }
      ollamaActionPlanned.textContent = `Action: ${msg.tool} ${JSON.stringify(msg.args || {})}`;
      addLog(`<b>[${agentName} Step ${msg.step}]</b>: ${msg.tool}(${JSON.stringify(msg.args || {})})`);

      if (msg.latencyMs && debugLatency) {
        debugLatency.textContent = `Latency: ${msg.latencyMs}ms`;
      }

      if (ollamaRawStream && msg.rawOutput) {
        const entry = document.createElement('div');
        entry.className = 'debug-entry';
        entry.innerHTML = `
          <div><span class="debug-step-tag">[${agentName} STEP ${msg.step}]</span> <span style="color:#34d399;">Action: ${msg.tool} ${JSON.stringify(msg.args || {})}</span></div>
          <div class="debug-raw-text">"${msg.rawOutput}"</div>
        `;
        ollamaRawStream.prepend(entry);
      }
    } else if (msg.state === 'COMPLETED' || msg.state === 'FINISHED') {
      resetOllamaUi();
      resetPollUi();
      if (msg.result) {
        const isPass = msg.result.outcome === 'pass';
        addLog(`<b>[${agentName} Complete]</b> Outcome: <span style="color:${isPass ? '#34d399' : '#f43f5e'}; font-weight:bold;">${msg.result.outcome}</span>`);
        ollamaObservations.textContent = `Mission complete with outcome: ${msg.result.outcome}`;
        ollamaActionPlanned.textContent = `Steps: ${msg.result.steps} | Sim Time: ${msg.result.simSeconds}s`;
      }
    } else if (msg.state === 'ABORTED' || msg.state === 'ERROR') {
      resetOllamaUi();
      resetPollUi();
      ollamaObservations.textContent = msg.error || 'Run stopped.';
    }
  }

  // 5. Continuous Render & HUD Animation Loop
  let lastTime = performance.now();
  let frameCount = 0;
  let fps = 60.0;
  const ribbonEngine = document.getElementById('ribbonEngine');

  function tick() {
    frameCount++;
    const now = performance.now();
    if (now - lastTime >= 1000) {
      fps = (frameCount * 1000) / (now - lastTime);
      frameCount = 0;
      lastTime = now;
      if (ribbonEngine) ribbonEngine.textContent = `${fps.toFixed(1)} FPS`;
    }

    views.renderDisplay();
    views.drawTopDown();
    views.updateHud();
    requestAnimationFrame(tick);
  }

  tick();
}

window.addEventListener('DOMContentLoaded', init);

