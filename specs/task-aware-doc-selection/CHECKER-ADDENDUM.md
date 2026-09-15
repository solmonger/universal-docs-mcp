# Checker Addendum — Required Repairs

Status: frozen after checker rejection of maker commit `23c0752362ee0c996dbc4ceb31a6a5caaadb1ee2`

The first maker candidate is not accepted. Its happy-path tests passed, but a controller probe against the exact committed object demonstrated false attribution and silent fallback.

## Fresh reproduced failures

The controller loaded the candidate from this worktree and observed:

1. `task=""`, no sources -> selected with the old generic query. An explicitly supplied invalid/empty task must not silently mean “mode not requested.”
2. A 450-character task plus `from rich.console import Console; Console(highlight=False)` -> query omitted both `Console` and `highlight`. Package-relevant symbols must have reserved query budget and cannot be displaced by task prose.
3. `import rich; rich.console.Console(highlight=False)` -> selected with an empty symbol list. Nested attribute chains rooted at a verified package alias must contribute bounded attribute/call symbols.
4. `import rich as r; import requests as r; r.get(...)` -> selected Rich context with symbol `get`. Rebinding or shadowing a tracked alias/imported symbol must cause `task_signal_ambiguous`, never false attribution.
5. Repeated scalar `--task first --task second` -> exit `0`, using `second`. Duplicate `--task` must be rejected as `invalid_plan_request`. Preserve the intentional repeatability of `--source`.

## Required implementation corrections

- Distinguish omitted task (`None`) from explicitly supplied empty/invalid task.
- Reserve query space in this priority order: package + exact version + migration intent, selected symbols, then bounded task terms. The final query remains <=512 characters.
- Walk attribute chains to their root, and collect identifier-shaped attributes for calls rooted in a verified package alias/imported symbol.
- Track bindings conservatively. If a tracked package alias or imported symbol is rebound by another import, assignment, annotated/augmented assignment, function/class definition, parameter, loop/comprehension target, context-manager target, exception target, pattern binding, or deletion, abstain as ambiguous. A correct implementation may be more conservative, but not less.
- Repeated identical package imports across source files are not automatically conflicting; only incompatible bindings or unsafe scope ambiguity should abstain.
- Add `--task` to duplicate scalar guarding; do not make `--source` scalar.
- Replace source path pre-check/open with descriptor-relative traversal from a trusted root descriptor, using `O_DIRECTORY`, `O_NOFOLLOW`, and `dir_fd` as already proven in `lockfile._read_scoped`. Enforce the 32 KiB/file and 128 KiB total bounds while reading in a loop. Do not rely on one `os.read` call or ancestor `lstat` checks.
- Do not import a private lockfile helper. Factor a small shared bounded scoped-reader only if that remains a narrow, tested change; otherwise implement the same proven pattern locally.
- Add tests for every reproduced failure and for descriptor-relative nested-directory reads, symlinked ancestor rejection, repeated identical imports, and source-order determinism.

## Acceptance remains unchanged

All original slice requirements remain mandatory. No benchmark, Hermes, model, network, activation, merge, push, or rollout work is authorized.

## Verification adjudication: pre-existing Ruff formatting debt

The original contract's blanket instruction to run `ruff format --check` on every changed Python file assumed those files were formatted on the protected base. That assumption is false.

Using the same Ruff version, interpreter, file list, and command on exact base `570417c34acf82de91f41707f22e2899fc6d25e7` returned exit `1` with `5 files would be reformatted`. On selector candidate `11de4bb798e1a277a6cf676ae8383a1655b45061`, the same command returned exit `1` with `3 files would be reformatted`; `planner.py` and `test_planner.py` are now formatted. The remaining failures are the pre-existing legacy formatting of `product_cli.py`, `test_product_cli_init.py`, and `test_product_cli_receipt_adversarial.py`.

Mass-formatting those large legacy files would create an unrelated review surface and contradict the slice's scoped-change rule. Therefore, for this slice, the formatting gate is baseline-aware:

- `ruff check` must pass for every changed Python file;
- `ruff format --check` must pass for `planner.py` and `test_planner.py`, the files formatted within this slice;
- the identical full changed-file formatter command must not report more failing files than the exact protected base;
- `git diff --check` must pass;
- no behavior, safety, test, or blocker/high review requirement is waived.

This adjudication supersedes only the blanket full-file formatting sentence. It does not waive new formatting debt or permit unrelated formatting cleanup.
