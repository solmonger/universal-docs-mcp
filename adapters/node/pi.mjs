import { prepareContext } from './context-bridge.mjs';

const CONFIG_KEYS = Object.freeze({
  executable: 'UNIVERSAL_DOCS_PI_EXECUTABLE',
  requestFile: 'UNIVERSAL_DOCS_PI_REQUEST_FILE',
  cacheDir: 'UNIVERSAL_DOCS_PI_CACHE_DIR',
  timeoutMs: 'UNIVERSAL_DOCS_PI_TIMEOUT_MS',
});

function trustedConfig(env = process.env) {
  const timeoutText = env[CONFIG_KEYS.timeoutMs];
  return {
    executable: env[CONFIG_KEYS.executable],
    requestFile: env[CONFIG_KEYS.requestFile],
    cacheDir: env[CONFIG_KEYS.cacheDir],
    timeoutMs: timeoutText === undefined ? 3000 : Number(timeoutText),
  };
}

function messageContent(result) {
  if (result.status === 'prepared' || result.status === 'prepared_stale') {
    return result.context;
  }
  return [
    'Documentation context unavailable; continue without claiming current documentation was loaded.',
    `Reason: ${result.error ?? 'unknown'}`,
  ].join('\n');
}

export default function universalDocsPiExtension(pi) {
  pi.on('before_agent_start', async () => {
    let result;
    try {
      result = await prepareContext(trustedConfig());
    } catch (error) {
      result = { status: 'unavailable', error: error instanceof Error ? error.message : String(error) };
    }
    return {
      message: {
        customType: 'universal-docs-pi',
        content: messageContent(result),
        display: true,
      },
    };
  });
}

export { messageContent, trustedConfig };
