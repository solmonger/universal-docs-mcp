# Version Guard pilot

Status: Gate B passed; Slice 05 ready
Last updated: 2026-09-10
Base candidate: `27789d88bddd08c2109b4faaff59dd842bebcfa7` / `0.4.0rc2`

## Next Agent Prompt

You are implementing exactly one slice of the Version Guard pilot from the frozen base named by the controller. Read this README and your assigned slice. Do not broaden retrieval, activate a profile, publish, merge, spend, or edit another lane. Preserve the existing trusted local single-user stdio contract. Run only the focused tests named by your slice plus `ruff` on changed paths and `git diff --check`, then commit immediately and report the commit, commands, outputs, and any stop rule that fired. Update no other slice status; the controller is the checker and integrator.

Global TODO:

- [x] Slice 01 — benchmark harness and executable oracles
- [x] Slice 02 — version-sensitive fixture corpus
- [x] Slice 03 — manual-context upper-bound run and source-profile decision
- [x] Slice 04 — deterministic Python dependency-change planner
- [ ] Slice 05 — one-command init and doctor
- [ ] Slice 06 — isolated active-process dogfood
- [ ] Slice 07 — internal outcome pilot and exact-object closeout

## Goal

Turn the accepted retrieval/delivery engine into a falsifiable product wedge:

> When a Python project's exact resolved dependency changes, deliver the most relevant exact-version documentation before one coding-agent call, explain the selection, and improve executable task outcomes without silently substituting latest or flooding context.

The pilot is complete only when the exact integrated commit has:

1. a reproducible A/B benchmark with executable oracles;
2. measured no-docs versus manually selected exact-version context;
3. a recorded source-profile decision (`readme` or a narrowly justified richer source) based on that upper bound;
4. deterministic Python dependency-change planning that abstains on uncertainty;
5. one-command onboarding diagnostics for one supported harness;
6. an isolated active-process receipt with rollback and cache isolation retained;
7. an internal outcome report and independent exact-object review.

External-user retention and public release are follow-on gates, not facts this implementation can manufacture.

## Product claim and prior art boundary

Do not market generic “fresh version-specific docs via MCP”; Context7 and Grounded Docs already cover that idea. The candidate claim is narrower: **local, exact resolved-version, proactive pre-model delivery with auditable provenance and explicit abstention**, especially for upgrades, migrations, and legacy pins.

## Architectural invariants

- `lockfile.py` remains the single owner of safe manifest parsing and exact-pin semantics.
- A new planner owns dependency-diff interpretation and emits typed preflight requests; it does not fetch documents.
- `preflight.py` remains wire-neutral and does not inspect repositories, prompts, imports, stack traces, or package-manager state.
- `context_delivery.py` remains the single receipt validator and packet formatter.
- Harness adapters consume prepared/unavailable frames; they do not reimplement selection or provenance.
- Range declarations are never installed-version evidence. Ambiguity produces an explicit abstention.
- Upstream text is untrusted data. No fetched text becomes instructions or executable content.
- Existing 8 KiB packet, deadline, network, source, and cache boundaries remain intact unless a separately reviewed failing test proves a required change.
- No telemetry contains prompts, source code, absolute project paths, secrets, or upstream document bodies.

## Shared planner interface

All planner-producing/consuming lanes use this exact logical shape:

```json
{
  "schema": "universal-docs.plan/v1",
  "status": "selected",
  "reason": "exact_dependency_changed",
  "package": "pydantic",
  "ecosystem": "python",
  "previous_version": "1.10.13",
  "target_version": "2.6.1",
  "resolution_source": "requirements-lock.txt",
  "query": "migration upgrade breaking changes quick start",
  "section_ids": [],
  "context_max_bytes": 4096,
  "freshness_mode": "require_check"
}
```

Abstention:

```json
{
  "schema": "universal-docs.plan/v1",
  "status": "abstained",
  "reason": "target_version_unresolved",
  "package": "pydantic",
  "ecosystem": "python",
  "previous_version": null,
  "target_version": null,
  "resolution_source": "pyproject.toml",
  "query": null,
  "section_ids": [],
  "context_max_bytes": 4096,
  "freshness_mode": "require_check"
}
```

Planner output must not contain an absolute path. The consumer converts only `status=selected` into the existing strict `PreflightRequest` shape with `selection=requested`.

## Benchmark contract

Each case contains:

```json
{
  "id": "pydantic-v1-to-v2-config",
  "ecosystem": "python",
  "package": "pydantic",
  "target_version": "2.6.1",
  "task": "...",
  "workspace_fixture": "fixtures/pydantic-v2",
  "test_command": ["python", "-m", "pytest", "-q"],
  "context_profile": "readme",
  "expected_failure_class": "wrong_version_api"
}
```

The runner creates a fresh copy of the tracked fixture, runs a coding-agent command supplied outside the repository, records provider/model/generation ID, then executes the declared oracle. The repository stores task inputs, exact context hashes, diffs, test output, timing, and aggregate results—not credentials or hidden reasoning. Repeated runs must use a new workspace and response-cache bypass.

Primary metrics:

- executable task success;
- wrong-version API calls;
- turns and wall time to green.

Secondary metrics:

- selector precision/recall and correct abstention;
- setup-to-first verified packet;
- context bytes and median added latency.

Receipt counts, citations, cache hits, or injection success alone are proxy metrics, not outcome proof.

## Decision gates

### Gate A — source-context upper bound

Run at least 10 version-sensitive cases with identical agent/model settings in two arms:

- control: no Universal Docs context;
- manual: checker-selected exact-version context capped at 4 KiB.

Prefer paired deterministic runs; if model nondeterminism prevents exact repeats, record seeds where supported and use at least three runs per arm/case within the existing prepaid balance. No new credit purchase is authorized by this spec.

Pass to planner implementation when either:

- manual context improves executable successes by at least 3 of 10 paired cases without regressing more than 1 case; or
- wrong-version API failures fall by at least 30% and median time does not worsen by more than 20%.

If neither threshold is met, stop planner work. Inspect failures and run one bounded source-profile pivot (migration/release-note material from a fixed reviewed source path). If the pivot also fails, record `product_thesis_not_supported` and do not build onboarding or release work.

### Gate B — planner

On at least 20 deterministic fixtures:

- precision ≥90%;
- correct abstention = 100% for ranges, ambiguous pins, non-registry references, and unsupported manifests;
- zero silent-latest substitutions;
- median planning time <100 ms on tracked fixtures;
- no absolute paths or document text in plan output.

Task-text, stack-trace, import-graph, transitive-dependency, and ML selection are explicitly excluded.

### Gate C — onboarding

One command in a fresh isolated environment must:

- identify the exact installed package/version/module path;
- verify stdio discovery and one source-bearing call;
- verify the chosen harness integration without activating unrelated profiles;
- print rollback/cache locations and structured unknown/failure states;
- reach a verified result in under 10 minutes without root access.

A green subprocess exit without the source-bearing receipt is failure.

### Gate D — pilot

Run at least 20 paired internal cases. Promotion to external-user pilot requires:

- automatic arm reduces wrong-version failures by at least 30%;
- automatic arm does not reduce executable successes;
- median delivery overhead <2 seconds on cache hit and explicitly measured on refresh;
- selection precision/abstention meets Gate B;
- exact integrated object receives independent review GO.

Three-to-five external users and week-two retention remain required before public-release claims. The repository may ship a recruitment kit, but cannot claim retention without real users.

## Scope firewalls

Not in this effort:

- arbitrary URL crawling, generic full-site indexing, full symbol/API coverage;
- npm lockfile, Poetry groups, workspace traversal, recursive includes, docs.rs;
- more harnesses, Pi CLI/native MCP, remote/multi-tenant service;
- July-2026 server conformance, publisher authentication;
- public publication, marketplace listing, merge to main, or broad profile rollout.

## Slice graph

```text
01 benchmark runner ─┐
                     ├─> 03 upper-bound run/source decision ─> 04 planner ─> 05 onboarding
02 fixture corpus ───┘                                             │              │
                                                                  └──────┬───────┘
                                                                         v
                                                               06 isolated dogfood
                                                                         v
                                                               07 pilot + review
```

Slices 01 and 02 may run in parallel from the same frozen tag. Every later slice starts from a controller-integrated, verified commit. One Luna dispatch owns one artifact target.

## Review map

The controller/checker must, for every maker commit:

1. inspect full `git show` and affected consumers;
2. rerun the exact focused tests in the lane;
3. integrate only after verifying target-path conflicts;
4. run joined producer→consumer tests after combining independent lanes;
5. freeze the final object for an independent correctness/privacy and product-evidence review;
6. repair RED→GREEN and re-review any changed successor object.

## References

- `docs/CONTEXT_DELIVERY.md`
- `docs/COMMAND_HOOKS.md`
- `specs/cross-harness-fresh-docs/README.md`
- accepted evidence: `/Users/operator/.hermes/workflows/universal-docs-hardening/acceptance-27789d8/`
- product council: `/Users/operator/.hermes/workflows/universal-docs-hardening/acceptance-27789d8/product-council-2026-09-10/transcript_repaired.md`
