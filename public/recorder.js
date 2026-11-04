export class ClipRecorder {
  constructor(viewsManager) {
    this.views = viewsManager;
    this.recorder = null;
    this.recordedChunks = [];
    this.isRecording = false;
    this.isPaused = false;
    this.commandText = '';
    this.clipStartTime = 0;
    this.recordedDuration = 0;

    // Offscreen 1440x1080 canvas for high-quality video clip compositing
    this.clipCanvas = document.createElement('canvas');
    this.clipCanvas.width = 1440;
    this.clipCanvas.height = 1080;
    this.clipCtx = this.clipCanvas.getContext('2d');

    this.rafId = null;
  }

  startClip(commandText) {
    this.commandText = commandText || '';
    this.recordedChunks = [];
    this.clipStartTime = performance.now();
    this.recordedDuration = 0;
    this.isRecording = true;
    this.isPaused = false;

    const stream = this.clipCanvas.captureStream(30); // 30 FPS
    let mimeType = 'video/webm;codecs=vp9';
    if (!MediaRecorder.isTypeSupported(mimeType)) {
      mimeType = 'video/webm';
    }

    try {
      this.recorder = new MediaRecorder(stream, { mimeType, videoBitsPerSecond: 6000000 });
    } catch (e) {
      console.warn('Fallback to standard MediaRecorder without mime options', e);
      this.recorder = new MediaRecorder(stream);
    }

    this.recorder.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) {
        this.recordedChunks.push(e.data);
      }
    };

    this.recorder.start(100);
    this.loopDraw();
    console.log('[ClipRecorder] Started recording clip for command:', this.commandText);
  }

  pauseClip() {
    if (this.recorder && this.recorder.state === 'recording') {
      this.recorder.pause();
      this.isPaused = true;
    }
  }

  resumeClip() {
    if (this.recorder && this.recorder.state === 'paused') {
      this.recorder.resume();
      this.isPaused = false;
    }
  }

  async stopClip() {
    if (!this.isRecording) return null;
    this.isRecording = false;

    // Hold final frame for 1.5 seconds per spec
    await new Promise(r => setTimeout(r, 1500));

    if (this.rafId) cancelAnimationFrame(this.rafId);

    return new Promise((resolve) => {
      if (!this.recorder || this.recorder.state === 'inactive') {
        return resolve(null);
      }
      this.recorder.onstop = () => {
        const blob = new Blob(this.recordedChunks, { type: 'video/webm' });
        console.log('[ClipRecorder] Clip finished, blob size:', blob.size);
        resolve(blob);
      };
      this.recorder.stop();
    });
  }

  loopDraw() {
    if (!this.isRecording) return;

    // Composite POV canvas onto 1440x1080 canvas
    const povCanvas = this.views.povCanvas;
    const ctx = this.clipCtx;

    // Draw POV scaled to full 1440x1080 (same 4:3 aspect ratio)
    ctx.drawImage(povCanvas, 0, 0, 1440, 1080);

    // Show command text overlay during first 2 seconds only (no HUD, team name, or code)
    const elapsedSec = (performance.now() - this.clipStartTime) / 1000;
    if (elapsedSec <= 2.0 && this.commandText) {
      ctx.save();
      // Elegant semi-transparent bottom banner
      ctx.fillStyle = 'rgba(0, 0, 0, 0.75)';
      ctx.fillRect(80, 920, 1280, 90);
      ctx.strokeStyle = '#38bdf8';
      ctx.lineWidth = 3;
      ctx.strokeRect(80, 920, 1280, 90);

      ctx.fillStyle = '#ffffff';
      ctx.font = 'bold 36px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText(`COMMAND: "${this.commandText}"`, 720, 978);
      ctx.restore();
    }

    this.rafId = requestAnimationFrame(() => this.loopDraw());
  }

  downloadClip(blob, filename = 'robot_fetch_pov.webm') {
    if (!blob) return;
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.style.display = 'none';
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => {
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    }, 1000);
  }
}

