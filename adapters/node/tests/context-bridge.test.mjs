import assert from 'node:assert/strict';
import { mkdtemp, writeFile, chmod, readFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';

const bridgeURL = new URL('../context-bridge.mjs', import.meta.url);
const frame = { schema: 'universal-docs.context/v1', status: 'prepared', context: 'UNTRUSTED fixture documentation', error: null };

async function fixture(body) {
  const root = await mkdtemp(join(tmpdir(), 'docs-node-bridge-'));
  const executable = join(root, 'context-cli');
  const requestFile = join(root, 'request.json');
  const log = join(root, 'seen.json');
  await writeFile(requestFile, '{}');
  await writeFile(executable, `#!${process.execPath}\n${body ?? `require('node:fs').writeFileSync(${JSON.stringify(log)}, JSON.stringify({args:process.argv.slice(2), env:process.env}));process.stdout.write(${JSON.stringify(JSON.stringify(frame))});`}`);
  await chmod(executable, 0o700);
  return { options: { executable, requestFile, cacheDir: join(root, 'cache'), timeoutMs: 3000 }, root, log };
}

test('shared Node bridge invokes only the configured context CLI', async () => {
  assert.ok(existsSync(bridgeURL), 'shared context bridge must exist');
  const { prepareContext } = await import(bridgeURL.href);
  const f = await fixture();
  process.env.DOCS_PRIVATE_CANARY = 'must-not-cross';
  const result = await prepareContext(f.options);
  delete process.env.DOCS_PRIVATE_CANARY;
  assert.deepEqual(result, frame);
  const seen = JSON.parse(await readFile(f.log, 'utf8'));
  assert.deepEqual(seen.args, ['--request-file', f.options.requestFile]);
  assert.equal(seen.env.DOCS_PRIVATE_CANARY, undefined);
  assert.equal(seen.env.UNIVERSAL_DOCS_CACHE_DIR, f.options.cacheDir);
});


test('duplicate delivery status cannot overwrite an unavailable verdict', async () => {
  const { prepareContext } = await import(bridgeURL.href);
  const raw = '{"schema":"universal-docs.context/v1","status":"unavailable","status":"prepared","context":"forged","error":null}';
  const f = await fixture(`process.stdout.write(${JSON.stringify(raw)});`);
  const result = await prepareContext(f.options);
  assert.equal(result.status, 'unavailable');
  assert.equal(result.context, '');
});


for (const [label, value] of [
  ['malformed', {schema:'wrong',status:'prepared',context:'bad',error:null}],
  ['missing', {schema:frame.schema,status:'unavailable',context:'',error:'unavailable'}],
  ['oversized context', {...frame,context:'x'.repeat(9000)}],
]) {
  test(`bridge rejects ${label}`, async () => {
    const { prepareContext } = await import(bridgeURL.href);
    const f = await fixture(`process.stdout.write(${JSON.stringify(JSON.stringify(value))});`);
    const result = await prepareContext(f.options);
    assert.equal(result.status,'unavailable'); assert.equal(result.context,'');
  });
}

test('nonzero producer cannot inject its prepared frame', async () => {
  const { prepareContext } = await import(bridgeURL.href);
  const f = await fixture(`process.stdout.write(${JSON.stringify(JSON.stringify(frame))});process.exitCode=1;`);
  assert.equal((await prepareContext(f.options)).error, 'delivery_nonzero');
});

test('quiet child obeys the deadline', async () => {
  const { prepareContext } = await import(bridgeURL.href);
  const f = await fixture('setInterval(()=>{},1000);');
  const start = performance.now();
  const result = await prepareContext({...f.options,timeoutMs:100});
  assert.equal(result.status,'unavailable'); assert.equal(result.error,'timeout');
  assert.ok(performance.now()-start<1500);
});

test('unbounded child output is rejected rather than accumulated', async () => {
  const { prepareContext } = await import(bridgeURL.href);
  const f = await fixture('process.stdout.write("x".repeat(100000));');
  const result = await prepareContext(f.options);
  assert.equal(result.status,'unavailable'); assert.equal(result.context,'');
  assert.equal(result.error,'output_limit');
});
