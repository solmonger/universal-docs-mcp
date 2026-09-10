# Cross-harness fresh documentation

Status: active local implementation. No public release or live installation change.

## Contract

Give models relevant, bounded, cited documentation before a run via a host hook, not merely an instruction to call a tool. Distinguish requested/pinned/latest-observed versions and upstream-checked/cache/stale/unknown freshness. Treat every source as untrusted data. Existing single-user stdio controls remain in force; no arbitrary-URL fetcher, remote server, shell execution of source text, or automatic profile-wide setup.

## Slices and gates

| Slice | State | Acceptance |
|---|---|---|
| Typed output contracts | Locally verified; independent gate pending | All five tools have meaningful schemas; actual success/miss/failure payloads validate; invalid outbound contents cannot reach SDK error reflection; stdio and existing tests stay green. |
| Cross-version compatibility | Parent-verified; exact-object acceptance pending | Execute a current SDK client against the candidate, recording discover/fallback/negotiation and real calls. Add modern server support only if the measured compatibility requirement warrants it; do not claim July-2026 conformance from fallback. Correct wire errors and preserve cancellation/size/privacy behavior. |
| Shared preflight core | Parent-verified; exact-object acceptance pending | Wire-neutral request/result; explicit freshness policy; selected-version and source/hash/age receipt; bounded relevant context from current allowed package sources; one JSON CLI response and explicit failure exit. A range is never installed-version evidence. |
| Versioned official source | Catalog/fetch library verified; preflight connection pending | Narrow reviewed catalog entry, fixed HTTPS host/path, bounded content and exact source/version provenance. Never follow a registry docs_url as authorization. Live allowlist expansion remains separate from isolated source tests. |
| Claude Code | Candidate actual-host fixture receipt; shared-boundary repair in progress | Synchronous UserPromptSubmit adapter and actual host event/context receipt; unit-test formatting is not a host run. |
| Codex | Candidate actual-host fixture receipt; shared-boundary repair in progress | Documented prompt hook plus actual context receipt; unsupported local versions explicitly reported. |
| Hermes | Thin neutral bridge and native assembly verified; live-core join pending | Documented pre_llm_call plugin with source/receipt in actual pre-model input; retain current tool/manifest permission boundaries. |
| OpenClaw | Depends on preflight | Documented before_prompt_build seam; runtime-specific proof; no blanket claim across runtime adapters. |
| Pi | Depends on preflight | Trusted extension before_agent_start path; Pi has no native MCP, so do not advertise it as native. |
| Acceptance | Depends on candidate | Frozen source, fresh installed wheel, offline/live/fault tests, exact-object independent review, honest five-harness matrix; current live rollback remains retained. |

## Initial evidence

The typed-contract change has RED receipts for missing schemas and unsanitized outbound validation failures, followed by 164 passed / 7 live skipped and a separate ten-call live stdio smoke. These are source-candidate checks, not replacement-installation or cross-harness delivery proof. Private raw receipts stay outside release artifacts.

## Next agent prompt

Continue on `feat/cross-harness-fresh-docs`. Current package preflight and SDK 2.2→1.30 fallback are first-hand verified. The Hermes bridge consumes a shared neutral delivery frame rather than independently interpreting raw receipts; its first/next/post-compaction native assembly proof is fixture-backed, not a live-model claim. Latest parent suite: 221 passed / 10 explicitly skipped; native Hermes ran separately. Host tests are opt-in so offline CI does not depend on a personal installation; an explicitly requested but missing native interpreter is not silently skipped.

Next pickup: integrate the official-source and shared-delivery maker commits, then join the thin Hermes bridge to that real CLI. Implement OpenClaw/Pi on the same neutral frame; freeze the combined candidate for independent review and fresh installed-wheel/host checks. Do not duplicate source/freshness validation in each host. Preserve the accepted `cbcd35c` live install. All new hooks remain local candidates: no profile-wide installation, other-profile changes, publication or broadened live access without corresponding approval.

## Primary contracts

- [MCP tools, 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)
- [MCP tools, 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
- [Claude hooks](https://code.claude.com/docs/en/hooks)
- [Codex hooks](https://developers.openai.com/codex/hooks)
- [Hermes hooks](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks)
- [OpenClaw hooks](https://docs.openclaw.ai/plugins/hooks)
- [Pi extensions](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md)
