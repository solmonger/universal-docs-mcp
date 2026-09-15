import { prepareContext } from './context-bridge.mjs';

const DEFAULT_TIMEOUT_MS = 3000;
export const UNAVAILABLE_PREFIX = '[universal-docs context unavailable; current docs were not loaded:';

function unavailableContext(error) {
  return `${UNAVAILABLE_PREFIX} ${error}]`;
}

export function register(api) {
  const config = api.pluginConfig ?? {};
  api.on(
    'before_prompt_build',
    async () => {
      const result = await prepareContext({
        executable: config.executable,
        requestFile: config.requestFile,
        cacheDir: config.cacheDir,
        timeoutMs: config.timeoutMs ?? DEFAULT_TIMEOUT_MS,
      });
      if (result.status === 'unavailable') {
        api.logger?.warn?.(`universal-docs context unavailable: ${result.error}`);
        return { prependContext: unavailableContext(result.error) };
      }
      return { prependContext: result.context };
    },
    { timeoutMs: config.timeoutMs ?? DEFAULT_TIMEOUT_MS },
  );
}

export default { register };
