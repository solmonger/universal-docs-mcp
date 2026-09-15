# Always-On Universal Docs Consumption — Canonical Specification

Status: implementation contract
Scope: Hermes coding and technical-research sessions using Universal Docs MCP

## Outcome

Universal Docs consumption is automatic for eligible third-party package/API work. Availability, registration, process health, tool visibility, planner execution, retrieval, and organic adoption remain separate claims.

The required chain is:

`eligible turn -> trusted project evidence -> exact-version selection -> task-aware plan -> bounded retrieval -> injected context + durable receipt`

A model choosing to load a skill or search for an MCP tool is not the trigger and is not sufficient evidence of this outcome.

## Eligible turns

The Hermes hook evaluates every turn, including delegated-worker turns, but retrieves only when local project evidence connects the task to a third-party package or external SDK/API. Eligible work includes implementation, debugging, code review, dependency changes, migration, compatibility analysis, architecture comparison, and package/API technical research.

Markets, news, papers, therapy, generic factual research, and other turns without package/API evidence are ineligible and must not invoke Universal Docs.

## Trusted evidence boundary

1. The hook obtains the session working directory or Git root from Hermes-owned runtime state: a validated session database row, or Hermes's absolute `TERMINAL_CWD` workspace binding when the persisted row is blank. Prompt text, repository content, and tool output never select paths.
2. It reads only supported manifests and bounded regular source files beneath that root using descriptor-relative, no-follow semantics.
3. Package identity must come from a locally parsed registry dependency pin. Prompt text may disambiguate among locally proven candidates but may not invent a package, version, source path, registry, or URL.
4. Exact versions are required. Ranges, conflicting pins, nonregistry references, malformed manifests, missing source attribution, and ambiguous candidate sets abstain.
5. Retrieved documentation is untrusted upstream data and is injected as reference context, never as instructions.

## Selection

### Current package/API work

The selector reads one supported current manifest and at most eight bounded source files. It attributes imports and symbols using the reviewed task-aware source parser. A package is selectable only when:

- the manifest has one exact registry pin for it; and
- source attribution proves use of that package, or the normalized task text unambiguously names that locally pinned package for technical research.

Multiple remaining candidates abstain rather than selecting arbitrarily.

### Dependency changes

When before/after manifest evidence exists, the existing dependency-change planner runs first. It preserves previous and target versions, source-backed symbols, task contribution, and all current fail-closed behavior.

## Retrieval and bounds

- At most one automatic package selection per hook evaluation.
- At most one compact documentation retrieval, plus one outline/section refinement only when the selected query requires it.
- Injected context is capped at 4 KiB by default and must preserve package, ecosystem, requested/resolved version, query, source, source URL, fetch time, cache status, version binding, freshness result, selection mode, inspected-source count, symbols, and aliases when present.
- A bounded timeout or upstream failure must not stall the agent indefinitely.

## Receipts and abstention

Every eligible evaluation writes a machine-readable receipt, including abstentions and failures. Receipt statuses are `selected`, `abstained`, `retrieved`, and `failed`; reasons are stable machine-readable tokens. A `retrieved` receipt must bind the returned context to the exact `docrequest-v4:{ecosystem}:{package}:{version}` cache key, cache fetch time, source and source URL, byte count, cache-value SHA-256, and injected-context SHA-256. The cache source URL must occur in the injected context; a valid frame without that binding fails closed as `cache_evidence_missing_or_mismatched`. Registration, process presence, handshake, `cache_stats`, and assistant-authored smoke probes do not count as consumption.

The hook injects a concise abstention marker only when the absence of documentation materially affects the task. It never fabricates documentation or silently falls back to latest versions.

## Delegation

Package-related delegated workers receive the same hook evaluation independently. Parent prompts and skills are not treated as inherited evidence. A parent may pass task intent, but the worker must resolve its own trusted root, exact version, source attribution, retrieval, and receipt.

## Profile boundary

The integration is enabled by default only for profiles that permit coding or technical research. A profile with an explicit restricted-tool design is an opt-out boundary. Frenchbot remains excluded: its family-assistant tool surface, disabled discovery, and absence of Universal Docs must not be overwritten by global rollout or profile-repair scripts.

New profile creation is an integration boundary. Clone/inheritance may carry the hook only when the source profile permits it; plain restricted-profile creation does not.

## Installation versus behavior

Configuration must register the MCP server and enable the automatic hook. Tool registration alone is incomplete. The hook implementation must be pinned to a reviewed artifact, use the shared Universal Docs runtime/cache unless a profile intentionally isolates them, and remain repeat-safe across reinstall/restart.

## Acceptance gates

1. Unit tests prove current exact-pin selection, source attribution, ambiguity abstention, path confinement, bounded inputs, prompt-path rejection, receipt truthfulness, timeout behavior, and restricted-profile opt-out.
2. Existing dependency-change planner and MCP suites remain green.
3. A fresh ordinary Hermes coding session, without manually loading `package-docs-lookup` or naming an MCP tool, produces a completed exact-version retrieval and matching receipt/cache evidence before using package behavior.
4. A fresh delegated package task independently produces its own retrieval evidence.
5. An unrelated research/therapy turn produces no Universal Docs retrieval.
6. Frenchbot completes a fresh turn with no Universal Docs registration, hook, skill, or retrieval.
7. Organic efficacy is reported separately over a later observation window; canaries prove the path, not broad adoption.

## Rollout claims

Passing this specification authorizes only the locally configured Hermes profiles covered by the verified installation. It does not authorize publication, marketplace release, external cohorts, broad public rollout, paid API expansion, or re-enabling deliberately restricted profiles.
