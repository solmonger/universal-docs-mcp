import assert from 'node:assert/strict';
import { chmod, mkdtemp, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { test } from 'node:test';

const FRAME = {
  schema: 'universal-docs.context/v1',
  status: 'prepared',
  context: 'DOCS_PACKET current-user only',
  error: null,
};

async function fixture() {
  const root = await mkdtemp(join(tmpdir(), 'openclaw-docs-adapter-'));
  const executable = join(root, 'context-cli');
  const requestFile = join(root, 'request.json');
  await writeFile(requestFile, '{}');
  await writeFile(
    executable,
    `#!${process.execPath}\nprocess.stdout.write(${JSON.stringify(JSON.stringify(FRAME))});`,
  );
  await chmod(executable, 0o700);
  return {
    executable,
    requestFile,
    cacheDir: join(root, 'cache'),
  };
}

function host(pluginConfig) {
  const registrations = [];
  return {
    api: {
      pluginConfig,
      logger: { warn() {} },
      on(name, handler, options) {
        registrations.push({ name, handler, options });
      },
    },
    registrations,
  };
}

test('native manifest enables the hook capability and validates trusted paths', async () => {
  const manifest = JSON.parse(
    await (await import('node:fs/promises')).readFile(
      new URL('../openclaw.plugin.json', import.meta.url),
      'utf8',
    ),
  );
  assert.equal(manifest.id, 'universal-docs-openclaw');
  assert.deepEqual(manifest.activation, { onCapabilities: ['hook'] });
  assert.deepEqual(manifest.configSchema.required, [
    'executable',
    'requestFile',
    'cacheDir',
  ]);
  assert.equal(manifest.configSchema.additionalProperties, false);
  assert.equal(manifest.configSchema.properties.executable.pattern, '^/');
});

test('before_prompt_build delivers the bridge packet through prependContext', async () => {
  const { register } = await import('../openclaw.mjs');
  const config = await fixture();
  const h = host(config);

  register(h.api);

  assert.equal(h.registrations.length, 1);
  assert.equal(h.registrations[0].name, 'before_prompt_build');
  const result = await h.registrations[0].handler(
    { prompt: 'current user prompt' },
    { sessionKey: 'isolated-test', runId: 'run-1' },
  );
  assert.deepEqual(result, { prependContext: FRAME.context });
});
