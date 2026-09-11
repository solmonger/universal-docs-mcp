# Slice 03 — upper-bound run and source decision

## Owner

Controller/checker. This is execution evidence, not a maker implementation task.

## Contract

Run Gate A with the integrated benchmark and corpus. Bind each manual context to the accepted rc2 source URL/version/hash and preserve exact context bytes outside the aggregate report. Keep model settings identical between paired arms and response caching disabled.

Before the full run, require a paired canary with a blind oracle: the agent-visible workspace must not expose the target API name or oracle implementation, and the agent process must be sandboxed from the oracle root. A control+manual ceiling caused by local answer leakage is `invalid_efficacy_design`, not a product pass or failure. Preserve that receipt, repair the design, and rerun the canary before spending the full matrix.

## Visible artifact

`benchmarks/version_guard/results/<run-id>/` containing manifest, per-run receipts, aggregate JSON, Markdown report, context hash manifest, and environment/model identity. Commit only sanitized evidence suitable for the repository; keep raw/private provider records in the acceptance workspace.

## Verification

Programmatically reconcile declared totals with enumerated runs. Re-run at least one case from the saved manifest. Record Gate A as `pass`, `pivot_required`, or `product_thesis_not_supported` with the exact predicate values.

## Stop rule

Do not dispatch Slice 04 if Gate A fails. Permit one bounded fixed-source profile pivot; if it also fails, stop Slices 04–07 and close honestly.
