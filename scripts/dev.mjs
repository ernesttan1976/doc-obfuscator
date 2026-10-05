import { spawn } from 'node:child_process';
import { access } from 'node:fs/promises';
import path from 'node:path';
import process from 'node:process';
import { setTimeout as delay } from 'node:timers/promises';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const python = path.join(root, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
const vite = path.join(root, 'node_modules/vite/bin/vite.js');
const healthUrl = 'http://127.0.0.1:8765/api/health';
const minilmExtra = process.platform === 'darwin'
  ? 'minilm-macos'
  : process.platform === 'win32'
    ? 'minilm-windows'
    : null;
const children = [];
let stopping = false;

const childExit = (child, label) => new Promise((resolve) => {
  child.once('error', (error) => resolve({ error, label }));
  child.once('exit', (code, signal) => resolve({ code, signal, label }));
});

const isServiceReady = async () => {
  try {
    const response = await fetch(healthUrl, { signal: AbortSignal.timeout(1000) });
    if (!response.ok) return false;
    const health = await response.json();
    return health.service === 'blot-local';
  } catch {
    return false;
  }
};

const waitForService = async (child, exit) => {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    if (await isServiceReady()) return;
    const exited = await Promise.race([exit, delay(500).then(() => null)]);
    if (exited) {
      if (stopping) return;
      throw exited.error || new Error(`The local API exited before becoming ready (code ${exited.code ?? exited.signal}).`);
    }
    if (child.exitCode !== null) throw new Error('The local API exited before becoming ready.');
  }
  throw new Error('The local API did not become ready within 30 seconds.');
};

const stopChildren = () => {
  for (const child of children) {
    if (child.exitCode === null && child.signalCode === null) child.kill('SIGTERM');
  }
};

const syncPythonDependencies = () => new Promise((resolve, reject) => {
  const args = ['sync', '--inexact', '--extra', 'dev'];
  if (minilmExtra) args.push('--extra', minilmExtra);
  const sync = spawn('uv', args, { cwd: root, stdio: 'inherit' });
  sync.once('error', reject);
  sync.once('exit', (code) => {
    if (code === 0) resolve();
    else reject(new Error('Python dependency sync failed.'));
  });
});

const main = async () => {
  if (stopping) return;
  try {
    await syncPythonDependencies();
  } catch {
    throw new Error('Could not sync the Python dependencies. Ensure `uv` is installed and retry.');
  }
  try {
    await Promise.all([access(python), access(vite)]);
  } catch {
    throw new Error('The virtual environment or UI dependencies are missing. Run `npm ci` and retry.');
  }

  let apiExit;
  if (await isServiceReady()) {
    console.log('Using the local API already running at http://127.0.0.1:8765.');
  } else {
    const api = spawn(python, ['-m', 'backend.app'], { cwd: root, stdio: 'inherit' });
    children.push(api);
    apiExit = childExit(api, 'Local API');
    console.log('Starting the local API…');
    await waitForService(api, apiExit);
  }
  if (stopping) return;

  const frontend = spawn(process.execPath, [vite, '--host', '127.0.0.1', '--strictPort', '--open', '/obfuscation-workspace.html'], {
    cwd: root,
    stdio: 'inherit',
  });
  children.push(frontend);
  const frontendExit = childExit(frontend, 'Vite');
  console.log('Starting the web app…');

  const result = await Promise.race([
    frontendExit,
    ...(apiExit ? [apiExit] : []),
  ]);
  if (stopping) return;
  if (result.error) throw result.error;
  throw new Error(`${result.label} stopped (code ${result.code ?? result.signal}).`);
};

const handleSignal = () => {
  stopping = true;
  stopChildren();
};

process.once('SIGINT', handleSignal);
process.once('SIGTERM', handleSignal);

main()
  .catch((error) => {
    if (!stopping && error.name !== 'AbortError') console.error(error.message);
    process.exitCode = stopping ? 0 : 1;
  })
  .finally(stopChildren);
