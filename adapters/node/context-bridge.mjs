// One process boundary shared by the OpenClaw and Pi adapters.
// Receipt interpretation remains in universal-docs-context, not in these hosts.
import { spawn } from 'node:child_process';
import { lstat, access } from 'node:fs/promises';
import { constants } from 'node:fs';
import { isAbsolute, dirname } from 'node:path';

export const CONTEXT_SCHEMA = 'universal-docs.context/v1';
export const MAX_FRAME_BYTES = 16 * 1024;
export const MAX_CONTEXT_BYTES = 8 * 1024;
export const unavailable = (error) => ({ schema: CONTEXT_SCHEMA, status: 'unavailable', context: '', error });

function uniqueFlatFrame(text) {
  // The frame has only string/null values. Scan its actual root tokens so
  // duplicate keys (including escaped spellings) cannot be overwritten.
  const token = /\s*("(?:[^"\\]|\\.)*")\s*:\s*("(?:[^"\\]|\\.)*"|null)\s*([,}])/y;
  const keys = new Set();
  let position = 1;
  while (position < text.length) {
    token.lastIndex = position;
    const match = token.exec(text);
    if (!match) return false;
    const key = JSON.parse(match[1]);
    if (keys.has(key)) return false;
    keys.add(key); position = token.lastIndex;
    if (match[3] === '}') return position === text.length;
  }
  return false;
}

function readFrame(bytes, code) {
  if (code !== 0) return unavailable('delivery_nonzero');
  try {
    const text = new TextDecoder('utf-8', { fatal: true }).decode(bytes).trim();
    const frame = JSON.parse(text);
    if (!uniqueFlatFrame(text)) return unavailable('invalid_response');
    if (!frame || Array.isArray(frame) || typeof frame !== 'object'
      || Object.keys(frame).sort().join(',') !== 'context,error,schema,status'
      || frame.schema !== CONTEXT_SCHEMA) return unavailable('invalid_response');
    if (frame.status === 'unavailable') return unavailable('delivery_unavailable');
    if (!['prepared', 'prepared_stale'].includes(frame.status) || frame.error !== null
      || typeof frame.context !== 'string' || !frame.context.trim()
      || Buffer.byteLength(frame.context, 'utf8') > MAX_CONTEXT_BYTES) return unavailable('invalid_response');
    return frame;
  } catch { return unavailable('invalid_response'); }
}

async function validate(options) {
  if (!options || typeof options !== 'object' || Array.isArray(options)) throw new Error('configuration');
  const { executable, requestFile, cacheDir, timeoutMs = 3000 } = options;
  for (const path of [executable, requestFile, cacheDir]) {
    if (typeof path !== 'string' || !isAbsolute(path) || Buffer.byteLength(path) > 4096) throw new Error('configuration');
  }
  if (!Number.isInteger(timeoutMs) || timeoutMs < 100 || timeoutMs > 45000) throw new Error('configuration');
  const [exeStat, requestStat] = await Promise.all([lstat(executable), lstat(requestFile)]);
  if (!exeStat.isFile() || !requestStat.isFile() || requestStat.size > 64 * 1024) throw new Error('configuration');
  await access(executable, constants.X_OK);
  return { executable, requestFile, cacheDir, timeoutMs };
}

function safeEnvironment(cacheDir) {
  const env = { PATH: '/usr/bin:/bin', LANG: 'C.UTF-8', LC_ALL: 'C.UTF-8', UNIVERSAL_DOCS_CACHE_DIR: cacheDir };
  for (const key of ['HOME', 'TMPDIR', 'TMP', 'TEMP']) {
    if (process.env[key]) env[key] = process.env[key];
  }
  return env;
}

export async function prepareContext(options) {
  if (process.platform === 'win32') return unavailable('unsupported_platform');
  let config;
  try { config = await validate(options); } catch { return unavailable('invalid_configuration'); }
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(config.executable, ['--request-file', config.requestFile], {
        shell: false, detached: true, cwd: dirname(config.executable),
        env: safeEnvironment(config.cacheDir), stdio: ['ignore', 'pipe', 'ignore'],
      });
    } catch { resolve(unavailable('spawn_failed')); return; }
    const chunks = [];
    let length = 0;
    let failure = null;
    let settled = false;
    let graceTimer;
    let hardTimer;
    const deadline = setTimeout(() => abort('timeout'), config.timeoutMs);

    function signalGroup(signal) {
      if (!child.pid) return true;
      try { process.kill(-child.pid, signal); return true; }
      catch (error) { return error.code === 'ESRCH'; }
    }
    function finish(code) {
      if (settled) return;
      settled = true;
      clearTimeout(deadline); clearTimeout(graceTimer); clearTimeout(hardTimer);
      // The leader may already have exited; still clean its owned group.
      if (!signalGroup('SIGKILL')) failure = 'cleanup_unverified';
      child.stdout?.destroy();
      resolve(failure ? unavailable(failure) : readFrame(Buffer.concat(chunks, length), code));
    }
    function abort(reason) {
      if (settled || failure) return;
      failure = reason;
      // Signal before closing the pipe; closing first can race writer exit.
      if (!signalGroup('SIGTERM')) failure = 'cleanup_unverified';
      graceTimer = setTimeout(() => {
        if (!signalGroup('SIGKILL')) failure = 'cleanup_unverified';
      }, 150);
      hardTimer = setTimeout(() => {
        if (child.exitCode === null && child.signalCode === null) failure = 'cleanup_unverified';
        finish(null);
      }, 300);
    }
    child.on('error', () => { failure = 'spawn_failed'; finish(null); });
    child.stdout.on('error', () => abort('read_failed'));
    child.stdout.on('data', (chunk) => {
      if (settled || failure) return;
      if (length + chunk.length > MAX_FRAME_BYTES) { abort('output_limit'); return; }
      chunks.push(chunk); length += chunk.length;
    });
    child.on('close', (code) => finish(code));
  });
}
