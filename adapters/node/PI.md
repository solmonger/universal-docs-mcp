# Pi documentation preflight extension

This is a candidate Pi extension, not an active Pi installation. Pi has no native MCP support; this adapter calls the existing shared Node boundary and injects its result through Pi's documented `before_agent_start` lifecycle event.

## Implementation

`pi.mjs` registers exactly one handler:

- `before_agent_start` calls `prepareContext()` once for every accepted prompt.
- Trusted fixed configuration comes only from these explicit environment variables:
  - `UNIVERSAL_DOCS_PI_EXECUTABLE`
  - `UNIVERSAL_DOCS_PI_REQUEST_FILE`
  - `UNIVERSAL_DOCS_PI_CACHE_DIR`
  - optional `UNIVERSAL_DOCS_PI_TIMEOUT_MS` (default `3000`)
- The executable, request file, and cache directory are passed unchanged to the shared bridge. The bridge enforces absolute paths, bounded output, timeout, filtered environment, and process cleanup.
- The event prompt, cwd, transcript, command text, and model settings are not read or sent to the CLI.
- Prepared and prepared-stale packets are returned as a Pi custom message (`customType: universal-docs-pi`); the system prompt and prior messages are not rewritten.
- Unavailable results are **explicit warning-and-continue**, not fail-closed: the current model input receives `Documentation context unavailable...` plus the bridge error. The handler catches unexpected bridge exceptions and produces the same explicit missing-context marker.

The request and cache scope therefore remain fixed by trusted host configuration. This extension does not claim MCP support or model-controlled tool use.

## Verification

### Runtime pin and isolation

- Node: `v26.7.0`
- npm: `11.19.0`
- Official package: `@earendil-works/pi-coding-agent@0.85.1`
- npm integrity recorded during lookup: `sha512-FGRN+OHbWaefBPGaTggAdLjrIHW+s2PzLyglz/5dfLzb9of7uuXMXYC0fJIeZTw+shS32o2cuQ9jF7YSDuL/oQ==`
- Isolated SDK install: `cross-harness-design/pi/`, installed with `npm install --ignore-scripts --no-audit --no-fund`; no global install, credentials, or `~/.pi` state was used.
- The test uses Pi's actual `DefaultResourceLoader`, `createAgentSession`, `SessionManager.inMemory()`, `session.bindExtensions({ mode: "print" })`, and `ModelRuntime`. Each run uses a temporary cwd, agent directory, auth path, models path, and models-store path.
- The fixture provider is a registered local `streamSimple` provider. It has a non-secret fixture credential and a loopback placeholder URL, but its stream is entirely in-process; no HTTP request or paid model call is possible.

Run:

```sh
PI_SDK_DIR=/Users/operator/.hermes/workflows/universal-docs-hardening/cross-harness-design/pi \
  npm test
```

Result: **11 passed, 0 failed**. The Pi-only lifecycle cases prove:

1. The first and next prompt each invoke the extension and deliver `REQUESTS INSTALL PACKET` to the fixture provider's actual `context.messages` model input.
2. An unavailable bridge result delivers the explicit `delivery_unavailable` marker and the prompt still completes.
3. Extension configuration is not derived from `event.prompt`.

The test provider sees Pi's normalized model messages (the custom message becomes a user text message at the provider boundary), which is the load-bearing assertion rather than a homemade host callback.

### Request/source probes

The committed lifecycle test uses a deterministic **static native-host CLI fixture** so the Pi test has no network dependency. Its request shape is the requested real package request:

```json
{
  "package": "requests",
  "ecosystem": "python",
  "selection": "requested",
  "requested_version": "2.32.3",
  "query": "install",
  "freshness_mode": "require_check"
}
```

The real local `.venv/bin/universal-docs-context` was also run separately, with isolated cache scope, against both bounded requests:

- `actual-requests.json`: exit `0`, `status: prepared`, PyPI `requests` `2.32.3`, query `install`, upstream-checked packet, source `https://pypi.org/pypi/requests/2.32.3/json`.
- `actual-mcp-tools.json`: exit `0`, `status: prepared`, official source `mcp-tools` `2026-07-28`, query `tools/list`, upstream-checked packet, source `https://modelcontextprotocol.io/specification/2026-07-28/server/tools.md`.

Those files are evidence only and are outside the committed adapter. The Pi test's static CLI fixture is not presented as proof of a live upstream fetch; the separate CLI records provide that source/freshness probe.

## Code hashes

SHA-256 at verification time:

| Artifact | SHA-256 |
|---|---|
| `adapters/node/pi.mjs` | `b754906ec711ffa148a933264ca992fbdfa137ecc43264b50e5af6b10cf6c652` |
| `adapters/node/tests/pi.lifecycle.test.mjs` | `e54c9f0698b656361b1ad14ce2b7625e62f2ec914e8f2c6863c77856cab5a7ed` |
| shared `adapters/node/context-bridge.mjs` (unchanged) | `bd7a7a4cf25f615b6dd9f974fceb6823c1c5387f6b3969009419a26bc8ad95ea` |
| isolated SDK `package.json` | `f1738e4b42203e5f22bcb513f13fb2fb224f1e98d1f129ff042f87048665a94c` |
| isolated SDK `package-lock.json` | `5b7ab275593f1b6a36bff23f2b4f32ccbbad5ae739aacc5dd823798d68016305` |

## Limits

- `pi` is absent from PATH, so this is an actual Pi SDK/extension lifecycle proof, not a Pi CLI binary proof.
- No active Pi configuration was created or edited, and no package was installed into `~/.pi`.
- This adapter is enrichment with explicit missing-context signaling; it does not block a prompt when documentation preparation is unavailable.
