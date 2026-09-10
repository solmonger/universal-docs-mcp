# OpenClaw hook adapter

This adapter is a thin native OpenClaw plugin around the shared
`context-bridge.mjs` boundary. It registers the documented
`before_prompt_build` hook and returns only `{ prependContext }`, so the packet
lands in the current user turn immediately before model input. It does not
rewrite the system prompt or session history.

## Contract

- `openclaw.plugin.json` declares `activation.onCapabilities: ["hook"]` and a
  strict schema for absolute `executable`, `requestFile`, and `cacheDir` paths.
- `openclaw.mjs` reads the resolved settings from `api.pluginConfig` and calls
  `prepareContext({ executable, requestFile, cacheDir, timeoutMs })`.
- `prepared` and `prepared_stale` packets are returned through
  `prependContext` unchanged. An unavailable result returns an explicit
  `[universal-docs context unavailable; ...]` marker and logs the bridge error.
- This is explicit unavailable/fail-open behavior. OpenClaw documents that
  `before_prompt_build` failures are logged and skipped; this hook is not a
  fail-closed gate. Use `before_agent_run` separately if a supported runner
  must block without a packet.

The plugin is non-bundled. Its OpenClaw config must explicitly enable the
plugin and both conversation permissions:

```json5
{
  plugins: {
    enabled: true,
    allow: ["universal-docs-openclaw"],
    load: { paths: ["/absolute/path/to/adapters/node"] },
    entries: {
      "universal-docs-openclaw": {
        enabled: true,
        hooks: {
          allowConversationAccess: true,
          allowPromptInjection: true,
        },
        config: {
          executable: "/absolute/path/to/universal-docs-context",
          requestFile: "/absolute/path/to/docs-request.json",
          cacheDir: "/absolute/path/to/cache",
          timeoutMs: 3000,
        },
      },
    },
  },
}
```

Do not place source context in a system-prompt section or transcript rewrite.
The resolver and receipt policy remain owned by the shared boundary and Python
core; this adapter only maps its bounded result to the OpenClaw prompt hook.

## Checks

From `adapters/node`:

```sh
node --test tests/openclaw.test.mjs
node --test tests/*.test.mjs
```

## Isolated native-host evidence

The available PATH had no `openclaw` executable. For the native lifecycle
check, OpenClaw `2026.9.3` was fetched from its official npm tarball and
installed with `--ignore-scripts` only under:

`cross-harness-design/openclaw/runtime/`

The isolated config, state, workspace, local HTTP provider, and resolver
fixture are under `cross-harness-design/openclaw/`; no active profile or
credentials were used. The pinned host loaded this plugin with
`plugins inspect --runtime --json` and reported `status: loaded`,
`shape: hook-only`, `hookCount: 1`, and typed hook
`before_prompt_build`. A real `agent --local` embedded run then reached the
local fixture provider. The provider capture contains
`FIXTURE_DOCS_PACKET receipt=UDCTX:openclaw-native` in message index `1` with
role `user`; the marker is absent from the system message. The provider
returned `FIXTURE_PROVIDER_OK`.

Receipt, commands, hashes, and the exact limitation are recorded in
`cross-harness-design/openclaw/native-hook-receipt.json`.

The native host delivery proof is real, but the resolver and provider are
fixtures. The repository `.venv/bin/universal-docs-context` was absent in this
worktree, so this run does not prove that the live Universal Docs source,
version lookup, or freshness receipt was reached. No paid model call was made.
