# Cross-harness fresh documentation

Status: active local implementation. No public release or live installation change.

## Contract

Give models relevant, bounded, cited documentation before a run via a host hook, not merely an instruction to call a tool. Distinguish requested/pinned/latest-observed versions and upstream-checked/cache/stale/unknown freshness. Treat every source as untrusted data. Existing single-user stdio controls remain in force; no arbitrary-URL fetcher, remote server, shell execution of source text, or automatic profile-wide setup.

## Slices and gates

| Slice | State | Acceptance |
|---|---|---|
| Typed output contracts | Locally verified; independent gate pending | All five tools have meaningful schemas; actual success/miss/failure payloads validate; invalid outbound contents cannot reach SDK error reflection; stdio and existing tests stay green. |
| Cross-version compatibility | Isolated implementation active | Execute a current SDK client against the candidate, recording discover/fallback/negotiation and real calls. Add modern server support only if the measured compatibility requirement warrants it; do not claim July-2026 conformance from fallback. Correct wire errors and preserve cancellation/size/privacy behavior. |
| Shared preflight core | Isolated implementation active | Wire-neutral request/result; explicit freshness policy; selected-version and source/hash/age receipt; bounded relevant context from current allowed package sources; one JSON CLI response and explicit failure exit. A range is never installed-version evidence. |
| Versioned official source | Catalog/fetch library verified; preflight connection pending | Narrow reviewed catalog entry, fixed HTTPS host/path, bounded content and exact source/version provenance. Never follow a registry docs_url as authorization. Live allowlist expansion remains separate from isolated source tests. |
| Claude Code | Depends on preflight | Synchronous UserPromptSubmit adapter and actual host event/context receipt; unit-test formatting is not a host run. |
| Codex | Depends on preflight | Documented prompt hook plus actual context receipt; unsupported local versions explicitly reported. |
| Hermes | Depends on preflight | Documented pre_llm_call plugin with source/receipt in actual pre-model input; retain current tool/manifest permission boundaries. |
| OpenClaw | Depends on preflight | Documented before_prompt_build seam; runtime-specific proof; no blanket claim across runtime adapters. |
| Pi | Depends on preflight | Trusted extension before_agent_start path; Pi has no native MCP, so do not advertise it as native. |
| Acceptance | Depends on candidate | Frozen source, fresh installed wheel, offline/live/fault tests, exact-object independent review, honest five-harness matrix; current live rollback remains retained. |

## Initial evidence

The typed-contract change has RED receipts for missing schemas and unsanitized outbound validation failures, followed by 164 passed / 7 live skipped and a separate ten-call live stdio smoke. These are source-candidate checks, not replacement-installation or cross-harness delivery proof. Private raw receipts stay outside release artifacts.

## Next agent prompt

Continue this spec on `feat/cross-harness-fresh-docs`. The typed-contract implementation is the first checkpoint, not the completed product. In isolated lanes, build the freshness-aware preflight and execute current-client compatibility tests; share the same retrieval logic rather than forking it per harness. Preserve the accepted `cbcd35c` live install. Every feature needs behavioral RED→GREEN, a focused commit, and actual consumer proof before claims. Do not install profile-wide hooks, modify other Hermes profiles, publish, or broaden live access without the corresponding approval.

## Primary contracts

- [MCP tools, 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)
- [MCP tools, 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
- [Claude hooks](https://code.claude.com/docs/en/hooks)
- [Codex hooks](https://developers.openai.com/codex/hooks)
- [Hermes hooks](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks)
- [OpenClaw hooks](https://docs.openclaw.ai/plugins/hooks)
- [Pi extensions](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md)
