# Slice 06 — Proactive Harness Hooks (Hermes + Claude/Codex Dynamic Mode + Post-Tool Error Trigger)

Wave: 3 (starts after Slices 02, 04, and 05 are integrated into `feat/proactive-docs-v2`)
Depends on: Slice 02 (`select_current_packages`, `select_for_error`, `to_preflight_requests`), Slice 04 (`local_stubs.extract_local_stubs`, `source_catalog`), Slice 05 (fast cache / ETag)
Owned files:
- `src/universal_docs_mcp/context_delivery.py`
- `src/universal_docs_mcp/command_hook.py`
- `examples/hermes/universal-docs-preflight/hermes_hook.py`
- `examples/hermes/universal-docs-preflight/plugin.yaml` (only if hook declaration metadata needs `post_tool_call`)
- `tests/test_proactive_hooks.py` (new)
- Existing `tests/test_always_on_consumption.py`, `tests/test_command_hooks*.py`, `tests/test_context_delivery*.py`, `tests/test_hermes_hook*.py` must stay green.

## Contract Unlocked

Make proactive documentation actually fire and deliver useful, prominent, exact-version context across real coding sessions in Hermes, Claude Code, and Codex — on turn 1 AND mid-session when tool errors occur.

## Problems in Current Code

1. `hermes_hook.py:437` gates on a 14-word English regex, skipping real coding prompts like *"fix the pydantic validator"* or *"why is httpx timing out?"*.
2. `hermes_hook.py` only hooks `pre_llm_call` and calls single-package `select_current_package()` with `freshness_mode="require_check"` (slow) and README-only context (misses signatures).
3. If a session starts in `/Users/operator` and enters a repo mid-turn or hits an `ImportError`/`AttributeError` in a tool call, no hook triggers mid-session.
4. `command_hook.py` (Claude Code & Codex) only supports a fixed static `request.json` — it never runs dynamic repo planning (`select_current_packages`).

## API Seam

### 1. `src/universal_docs_mcp/context_delivery.py`

Preserve existing `build_context_packet()` and `deliver_result()` unchanged for single-target callers. Add:
- `build_bundle_packet(packets: list[str], *, local_stubs: list[LocalStubExcerpt] = (), max_bytes: int = MAX_PACKET_BYTES) -> str`
  - Combines 1–3 validated preflight packets (plus optional local stub excerpts when upstream README returned `no_match` or lacked requested symbols) inside a single bounded `<= MAX_PACKET_BYTES` (8 KiB) envelope.
  - Preserves every receipt header (`Receipt: UDCTX:...`, exact `Target version`, `Source URL`, `Source SHA-256`) and untrusted-data delimiters cleanly so the model immediately sees exact versions, symbols, and follow-up hint (`Use get_package_docs(package=..., version=..., query=...) for more detail.`).

### 2. `examples/hermes/universal-docs-preflight/hermes_hook.py`

- **Replace the brittle 14-word regex gate (`_is_eligible_turn`):**
  A turn is eligible when ANY of these holds:
  1. `task` mentions any dependency name (or normalized import name) resolved from the project's lockfile/manifest/venv; OR
  2. `task` matches a traceback/error pattern (`ImportError`, `ModuleNotFoundError`, `AttributeError`, `TypeError`, `ValidationError`, `Cannot find module`, `error[E0`); OR
  3. `task` contains coding/technical terms (the existing word set + `fix`, `error`, `bug`, `test`, `pytest`, `build`, `lint`, `refactor`, `endpoint`, `schema`, `async`, `client`, `hook`, `mcp`, `plugin`).
  Non-technical turns (markets, therapy, generic news, Frenchbot) with no dependency match and no coding/error signal remain ineligible (`return None`, zero network/retrieval).
- **Multi-package + fast-cache preflight in `_native_callback`:**
  - Call `select_current_packages(root, task=task, max_packages=3)` (with `configured_project_root` fallback preserved for dry roots like `/Users/operator`).
  - Use `to_preflight_requests(bundle, total_budget_bytes=6144, freshness_mode="allow_cache")` so repeat turns on exact pinned versions complete in <50ms from cache instead of hitting the network every turn.
  - When a package returns `preflight_no_match` or `documentation_not_found` (e.g., README lacks the requested symbol), automatically supplement with `local_stubs.extract_local_stubs(root, plan.package, plan.ecosystem, plan.target_version, symbols=...)` inside the same bounded packet and record `local_stub_used: true` in the receipt!
  - Preserve truthful atomic receipts in `receipt_dir` (including `packages`, `cache_evidence` per retrieved package, `local_stub_evidence`, and abstention reason).
- **Mid-session error trigger (`post_tool_call`):**
  - Register a second lightweight hook if `ctx.register_hook` supports `"post_tool_call"` (gracefully ignore if an older test harness mock only expects `"pre_llm_call"` — check `try/except` or inspect `ctx` safely so existing unit tests stay green!).
  - When a tool output (terminal/test run) contains a Python/JS/Rust library error (`ImportError`, `AttributeError`, `TypeError`, `Cannot find module`, `error[E0`), call `select_for_error(root, error_text=output)` and fetch bounded docs + local stub for the failing package/symbol, injecting them before the next LLM step (deduplicated per `(session_id, package, version, error_signature)` so it never spams the same error twice in one session).
- **Keep Frenchbot & `disabled_profiles` opt-out strictly enforced.**

### 3. `src/universal_docs_mcp/command_hook.py` (Claude Code & Codex)

- Preserve 100% of existing static `--config` / `--request-file` / explicit-target modes and `COMMAND_HOOK_BLOCK_CODES`.
- Add `--dynamic` mode (mutually exclusive with `--config` and `--request-file`):
  ```text
  universal-docs-command-hook --harness {claude,codex} --dynamic \
    --preflight-executable /abs/path/to/universal-docs-preflight \
    [--project-root /abs/path/to/project] [--timeout-ms 5000]
  ```
  - Reads the host `UserPromptSubmit` JSON event from stdin (extracting `prompt` / `user_message` and `cwd` only as bounded strings; validates `cwd` is an existing non-symlink dir or falls back to `--project-root`).
  - Runs `select_current_packages(root, task=prompt, max_packages=3)` locally.
  - **Fail-open on non-technical turns / abstentions in `--dynamic` mode:** Unlike static mode (which blocks on missing fixed docs), dynamic mode returns `{}` (allow prompt, inject nothing) when a prompt is ineligible or abstains cleanly — it NEVER blocks the user from chatting in Claude Code or Codex!
  - When packages are selected, invokes `preflight_executable` with `freshness_mode="allow_cache"`, validates receipts via `build_context_packet` / `build_bundle_packet`, and returns `format_claude_output(packet)` / `format_codex_output(packet)`.

## Human / Checker Playable Verification

```bash
# 1. Simulate Claude Code UserPromptSubmit in --dynamic mode on this repo
printf '{"prompt":"fix pydantic ValidationError in validation.py","cwd":"%s"}\n' "$PWD" | \
  env -u PYTHONPATH -u VIRTUAL_ENV .venv/bin/universal-docs-command-hook \
    --harness claude --dynamic \
    --preflight-executable "$PWD/.venv/bin/universal-docs-preflight"
```
Expected: outputs valid Claude `hookSpecificOutput.additionalContext` containing exact `pydantic` version docs (or local stub) without blocking!

## RED→GREEN Test Cases (`tests/test_proactive_hooks.py`)

1. Hermes hook fires on *"fix the pydantic validator"* (previously skipped by 14-word regex) when `pydantic` is in the project lockfile/venv.
2. Hermes hook skips *"summarize today's weather and news"* (non-technical turn → returns `None`, zero receipts/fetches).
3. Hermes hook retrieves a 2-package bundle (`pydantic` + `httpx`) inside <= 8 KiB with valid receipts for both and `freshness_mode="allow_cache"`.
4. Hermes hook falls back to `local_stubs` when upstream README returns `no_match` for a specific symbol, framing it as untrusted local stub data.
5. Hermes `post_tool_call` error hook triggers on a failing pytest `ImportError`/`AttributeError` traceback, fetches docs for the failing installed package, and deduplicates repeat occurrences in the same session.
6. Hermes hook stays completely disabled for `frenchbot` / `disabled_profiles`.
7. `command_hook.py --dynamic` with Claude/Codex `UserPromptSubmit` event: injects exact-version context for technical prompts, returns `{}` (no block) for non-technical prompts or empty dirs.
8. All existing `tests/test_always_on_consumption.py`, `tests/test_command_hooks*.py`, `tests/test_context_delivery*.py`, and `tests/test_hermes_hook*.py` stay 100% green.

## What Must Stay Green

- Full offline suite + `ruff check` + `ruff format --check` + `git diff --check`.
- After integration: fresh wheel build (`.venv/bin/python -m build --no-isolation`) and live canary run confirming real-world retrieval on lockfile/range/multi-dep repos.
