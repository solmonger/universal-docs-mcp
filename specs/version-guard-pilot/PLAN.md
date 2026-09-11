# Implementation plan

## Operating model

The controller owns integration, benchmark execution, active-process changes, exact-object review, and durable status. Luna makers receive one artifact target each in an isolated worktree from an immutable tag. Makers never merge, activate profiles, spend, publish, or run the full suite.

## Waves

### Wave 0 — freeze

1. Commit this plan/spec on the accepted rc2 branch.
2. Tag the committed object `ref/version-guard-pilot-2026-09-10`.
3. Record the base commit/tree and prove the source checkout is clean.

### Wave 1 — measurement substrate, parallel

- Lane A: implement Slice 01 benchmark runner and unit tests.
- Lane B: implement Slice 02 fixture corpus and oracle self-checker.
- Checker: inspect both commits, rerun focused tests, integrate in dependency order, and run one joined dry fixture.

### Wave 2 — upper-bound decision

- Checker obtains exact-version context with the accepted rc2 artifact, runs paired cases using existing prepaid model credit/local models, and writes the immutable result receipt.
- If Gate A fails, dispatch one maker for a fixed reviewed migration/release-note source profile, then rerun once. Stop the product thesis if the pivot fails.

### Wave 3 — product behavior

- Lane C: implement Slice 04 deterministic Python dependency-change planner against the frozen plan schema.
- Checker runs the 20-case precision/abstention/performance gate and joined planner→preflight→delivery test.
- Lane D: implement Slice 05 `init`/`doctor` only after Gate A and Gate B pass.

### Wave 4 — installed behavior

- Checker builds the integrated artifact and installs beside the retained rc2 predecessor with a separate cache.
- Run Slice 06 against one isolated harness/profile; verify active subprocess path, source-bearing packet, second-process cache reuse, and rollback command without performing rollback.

### Wave 5 — outcome proof and closeout

- Run Slice 07 internal paired pilot.
- Freeze exact commit/artifacts and obtain independent review.
- Any accepted blocker returns to a one-artifact repair maker and fresh exact-object recheck.
- Update README/spec/Vault. Send one concise Telegram note only after final evidence is read back.

## No-fake-completion rules

- A benchmark runner is not benchmark evidence.
- A prepared packet is not model comprehension.
- A passing test is not installed active-process proof.
- Internal outcomes are not external retention.
- A review GO is not merge/publication authority.
