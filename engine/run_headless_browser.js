import { spawn } from 'child_process';
import os from 'os';
import path from 'path';

const chromePath = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const tmpDir = path.join(os.tmpdir(), 'chrome_engine_' + Date.now());

const args = [
  '--headless=new',
  '--disable-gpu',
  '--no-sandbox',
  `--user-data-dir=${tmpDir}`,
  'http://localhost:8080/'
];

const child = spawn(chromePath, args, { stdio: 'inherit' });

child.on('exit', (code) => {
  console.log(`[Headless Chrome] Exited with code ${code}`);
  process.exit(code || 0);
});

process.on('SIGINT', () => child.kill());
process.on('SIGTERM', () => child.kill());

