# Gate A — exact-version context upper bound

**Verdict: PASS.** Manual context improved executable success from **4/10** to **8/10** (**+4**), with **4 positive flips**, **0 regressions**, no runner errors/timeouts, and median time changing from **24.73s** to **25.33s** (**+2.45%**). This exceeds the written +3/10 threshold.

## Source decision

Use bounded official **exact-tag migration/API excerpts** for upgrade tasks. Registry README packets flipped the Rich case, but source inspection showed they omitted required Pydantic and SQLAlchemy migration symbols. The richer fixed-version profile is therefore justified.

## Per-pair outcomes

| Repeat | Case | Control | Manual | Transition |
|---:|---|---|---|---|
| 1 | `pydantic-v1-to-v2-model-dump` | pass | pass | ceiling |
| 1 | `pydantic-v1-to-v2-validator` | fail | fail | unchanged_failure |
| 1 | `rich-12-to-13-console` | fail | pass | flip |
| 1 | `sqlalchemy-14-to-2-execute` | pass | pass | ceiling |
| 1 | `sqlalchemy-14-to-2-select` | fail | pass | flip |
| 2 | `pydantic-v1-to-v2-model-dump` | pass | pass | ceiling |
| 2 | `pydantic-v1-to-v2-validator` | fail | fail | unchanged_failure |
| 2 | `rich-12-to-13-console` | fail | pass | flip |
| 2 | `sqlalchemy-14-to-2-execute` | pass | pass | ceiling |
| 2 | `sqlalchemy-14-to-2-select` | fail | pass | flip |

## Evidence boundary

These are blind **synthetic behavioral fixtures**. Oracle bytes and target symbols were outside the workspace, internal IDs were hidden, and sandbox rules denied repository/evidence/private-snapshot oracle reads. This proves a mechanistic upper bound, not real-package efficacy, user retention, or rollout readiness.

Raw sanitized run receipts, manifest, and context hashes are stored beside this report. Exact context bytes and private adapter evidence remain in the controller evidence workspace.
