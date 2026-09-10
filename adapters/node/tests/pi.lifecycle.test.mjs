import assert from 'node:assert/strict';
import { chmod, mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { test } from 'node:test';

const sdkRoot = process.env.PI_SDK_DIR;
const extensionURL = new URL('../pi.mjs', import.meta.url);

async function sdkImport(relativePath) {
  assert.ok(sdkRoot, 'PI_SDK_DIR must name the isolated SDK evidence directory');
  const packageRoot = relativePath.startsWith('@earendil-works/pi-coding-agent/')
    ? join(sdkRoot, 'node_modules')
    : join(sdkRoot, 'node_modules', '@earendil-works', 'pi-coding-agent', 'node_modules');
  return import(pathToFileURL(join(packageRoot, relativePath)).href);
}

async function createContextFixture(frame, realExecutable) {
  const root = await mkdtemp(join(tmpdir(), 'pi-docs-fixture-'));
  const executable = realExecutable || join(root, 'universal-docs-context');
  const requestFile = join(root, 'request.json');
  const outputFile = join(root, 'frame.json');
  await writeFile(requestFile, JSON.stringify({
    package: 'requests',
    ecosystem: 'python',
    selection: 'requested',
    requested_version: '2.32.3',
    query: 'install',
    freshness_mode: 'require_check',
    context_max_bytes: 12000,
    deadline_ms: 8000,
  }));
  if (!realExecutable) {
    await writeFile(outputFile, JSON.stringify(frame));
    await writeFile(executable, `#!${process.execPath}
const fs = require('node:fs');
process.stdout.write(fs.readFileSync(${JSON.stringify(outputFile)}, 'utf8'));
`);
    await chmod(executable, 0o700);
  }
  return {
    root,
    options: {
      executable,
      requestFile,
      cacheDir: join(root, 'cache'),
      timeoutMs: realExecutable ? 10000 : 3000,
    },
  };
}

function restoreEnvironment(previous) {
  for (const key of ['UNIVERSAL_DOCS_PI_EXECUTABLE', 'UNIVERSAL_DOCS_PI_REQUEST_FILE', 'UNIVERSAL_DOCS_PI_CACHE_DIR', 'UNIVERSAL_DOCS_PI_TIMEOUT_MS']) {
    if (previous[key] === undefined) delete process.env[key];
    else process.env[key] = previous[key];
  }
}

function configureEnvironment(options) {
  const keys = {
    UNIVERSAL_DOCS_PI_EXECUTABLE: options.executable,
    UNIVERSAL_DOCS_PI_REQUEST_FILE: options.requestFile,
    UNIVERSAL_DOCS_PI_CACHE_DIR: options.cacheDir,
    UNIVERSAL_DOCS_PI_TIMEOUT_MS: String(options.timeoutMs),
  };
  const previous = Object.fromEntries(Object.keys(keys).map((key) => [key, process.env[key]]));
  Object.assign(process.env, keys);
  return previous;
}

function fixtureProvider(capture, piAI) {
  return (pi) => {
    pi.registerProvider('pi-fixture', {
      name: 'Pi local fixture',
      api: 'pi-fixture-api',
      baseUrl: 'http://127.0.0.1:1/fixture-never-called',
      apiKey: 'fixture-only-not-a-secret',
      models: [{
        id: 'fixture-model',
        name: 'Fixture model',
        reasoning: false,
        input: ['text'],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: 32_000,
        maxTokens: 128,
      }],
      streamSimple(model, context, options) {
        capture.push(structuredClone({ model, context }));
        const stream = piAI.createAssistantMessageEventStream();
        const message = {
          role: 'assistant',
          content: [{ type: 'text', text: 'fixture-ok' }],
          api: model.api,
          provider: model.provider,
          model: model.id,
          usage: {
            input: 0,
            output: 1,
            cacheRead: 0,
            cacheWrite: 0,
            totalTokens: 1,
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
          },
          stopReason: 'stop',
          timestamp: Date.now(),
        };
        queueMicrotask(() => {
          stream.push({ type: 'start', partial: message });
          stream.push({ type: 'text_start', contentIndex: 0, partial: message });
          stream.push({ type: 'text_delta', contentIndex: 0, delta: 'fixture-ok', partial: message });
          stream.push({ type: 'text_end', contentIndex: 0, content: 'fixture-ok', partial: message });
          stream.push({ type: 'done', reason: 'stop', message });
          stream.end();
        });
        return stream;
      },
    });
  };
}

async function runSession({ frame, promptCount = 2, realContextExecutable }) {
  const [codingAgent, piAI] = await Promise.all([
    sdkImport('@earendil-works/pi-coding-agent/dist/index.js'),
    sdkImport('@earendil-works/pi-ai/dist/index.js'),
  ]);
  const docsExtension = (await import(extensionURL.href)).default;
  const fixtureRoot = await mkdtemp(join(tmpdir(), 'pi-sdk-isolated-'));
  const fixture = await createContextFixture(frame, realContextExecutable);
  const previous = configureEnvironment(fixture.options);
  const capture = [];
  try {
    const loader = new codingAgent.DefaultResourceLoader({
      cwd: fixtureRoot,
      agentDir: join(fixtureRoot, 'agent'),
      extensionFactories: [docsExtension, fixtureProvider(capture, piAI)],
    });
    await loader.reload();

    const modelRuntime = await codingAgent.ModelRuntime.create({
      authPath: join(fixtureRoot, 'auth.json'),
      modelsPath: join(fixtureRoot, 'models.json'),
      modelsStorePath: join(fixtureRoot, 'models-store.json'),
    });
    const model = {
      id: 'fixture-model',
      name: 'Fixture model',
      api: 'pi-fixture-api',
      provider: 'pi-fixture',
      baseUrl: 'http://pi-fixture.invalid',
      reasoning: false,
      input: ['text'],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      contextWindow: 32_000,
      maxTokens: 128,
    };
    const { session } = await codingAgent.createAgentSession({
      cwd: fixtureRoot,
      agentDir: join(fixtureRoot, 'agent'),
      model,
      modelRuntime,
      resourceLoader: loader,
      sessionManager: codingAgent.SessionManager.inMemory(),
      settingsManager: codingAgent.SettingsManager.inMemory({
        compaction: { enabled: false },
        retry: { enabled: false },
      }),
      noTools: 'all',
      thinkingLevel: 'off',
    });
    await session.bindExtensions({ mode: 'print' });
    await modelRuntime.setRuntimeApiKey('pi-fixture', 'fixture-only-not-a-secret');

    try {
      for (let i = 0; i < promptCount; i += 1) await session.prompt(`fixture prompt ${i + 1}`);
    } finally {
      session.dispose();
    }
    return { capture, fixtureRoot, fixtureRootToRemove: fixture.root };
  } finally {
    restoreEnvironment(previous);
  }
}

test('Pi SDK lifecycle injects the selected packet before the first and next prompt', { skip: !sdkRoot }, async () => {
  const frame = {
    schema: 'universal-docs.context/v1',
    status: 'prepared',
    context: 'REQUESTS INSTALL PACKET',
    error: null,
  };
  const result = await runSession({ frame });
  try {
    assert.equal(result.capture.length, 2);
    for (const { context } of result.capture) {
      const modelText = context.messages.flatMap((message) => (
        Array.isArray(message.content)
          ? message.content.filter((part) => part.type === 'text').map((part) => part.text)
          : []
      ));
      assert.ok(modelText.includes('REQUESTS INSTALL PACKET'), 'provider received the extension packet');
    }
  } finally {
    await rm(result.fixtureRoot, { recursive: true, force: true });
    await rm(result.fixtureRootToRemove, { recursive: true, force: true });
  }
});

test('Pi SDK lifecycle preserves missing-context truth and continues explicitly', { skip: !sdkRoot }, async () => {
  const frame = {
    schema: 'universal-docs.context/v1',
    status: 'unavailable',
    context: '',
    error: 'delivery_unavailable',
  };
  const result = await runSession({ frame, promptCount: 1 });
  try {
    assert.equal(result.capture.length, 1);
    const modelText = result.capture[0].context.messages.flatMap((message) => (
      Array.isArray(message.content)
        ? message.content.filter((part) => part.type === 'text').map((part) => part.text)
        : []
    ));
    const missingContext = modelText.find((text) => text.includes('Documentation context unavailable'));
    assert.ok(missingContext, 'provider received the explicit missing-context message');
    assert.match(missingContext, /delivery_unavailable/);
    assert.ok(!modelText.includes('REQUESTS INSTALL PACKET'));
  } finally {
    await rm(result.fixtureRoot, { recursive: true, force: true });
    await rm(result.fixtureRootToRemove, { recursive: true, force: true });
  }
});

test('Pi SDK delivers freshly retrieved public docs for first and next prompts', {
  skip: !sdkRoot || !process.env.UNIVERSAL_DOCS_PI_LIVE_CONTEXT_CLI,
}, async () => {
  const result = await runSession({ realContextExecutable: process.env.UNIVERSAL_DOCS_PI_LIVE_CONTEXT_CLI });
  try {
    assert.equal(result.capture.length, 2);
    const packets = [];
    const fetchedAt = [];
    for (const { context } of result.capture) {
      const docsMessages = context.messages.filter((message) => Array.isArray(message.content)
        && message.content.some((part) => part.type === 'text' && part.text.includes('UNIVERSAL-DOCS PREFLIGHT CONTEXT PACKET v1')));
      assert.ok(docsMessages.length > 0, JSON.stringify(context.messages));
      const message = docsMessages.at(-1);
      assert.equal(message.role, 'user');
      const packet = message.content.find((part) => part.type === 'text' && part.text.includes('UNIVERSAL-DOCS PREFLIGHT')).text;
      assert.ok(packet.includes('Target version: "2.32.3"'));
      assert.ok(packet.includes('registry_version') && packet.includes('upstream_checked'));
      assert.ok(packet.includes('Source SHA-256:') && packet.includes('END UNTRUSTED DOCUMENTATION DATA'));
      assert.ok(!String(context.systemPrompt || '').includes('UNIVERSAL-DOCS PREFLIGHT'));
      const match = packet.match(/Fetched at \(Unix seconds\): ([0-9.]+)/);
      assert.ok(match, packet);
      fetchedAt.push(Number(match[1])); packets.push(packet);
    }
    assert.equal(new Set(fetchedAt).size, 2);
    assert.ok(!JSON.stringify(result.capture[0].context.messages).includes('fixture prompt 2'));
    if (process.env.UNIVERSAL_DOCS_HOST_EVIDENCE_DIR) {
      await mkdir(process.env.UNIVERSAL_DOCS_HOST_EVIDENCE_DIR, { recursive: true });
      await writeFile(join(process.env.UNIVERSAL_DOCS_HOST_EVIDENCE_DIR, 'pi-live-context.json'), JSON.stringify({
        schema: 'universal-docs.pi-live-proof/v1', observed_at: new Date().toISOString(),
        source_type: 'real_public_preflight', provider_type: 'in_process_fixture',
        paid_model_call: false, active_profile_touched: false, sdk_root: sdkRoot,
        context_cli: process.env.UNIVERSAL_DOCS_PI_LIVE_CONTEXT_CLI,
        fetched_at: fetchedAt, current_user_packets: packets, provider_inputs: result.capture,
      }, null, 2));
    }
  } finally {
    await rm(result.fixtureRoot, { recursive: true, force: true });
    await rm(result.fixtureRootToRemove, { recursive: true, force: true });
  }
});

test('Pi extension configuration cannot be derived from the prompt event', { skip: !sdkRoot }, async () => {
  const { default: docsExtension } = await import(extensionURL.href);
  const registrations = [];
  const pi = { on: (event, handler) => registrations.push({ event, handler }) };
  const previous = configureEnvironment({
    executable: '/trusted/context-cli',
    requestFile: '/trusted/request.json',
    cacheDir: '/trusted/cache',
    timeoutMs: 3000,
  });
  try {
    docsExtension(pi);
    assert.equal(registrations.length, 1);
    assert.equal(registrations[0].event, 'before_agent_start');
    assert.notEqual(registrations[0].handler.toString().includes('event.prompt'), true);
  } finally {
    restoreEnvironment(previous);
  }
});
