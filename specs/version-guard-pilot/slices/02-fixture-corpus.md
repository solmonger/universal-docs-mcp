# Slice 02 — fixture corpus

## Contract

Create at least 10 small, license-safe Python coding fixtures whose required solution differs across package versions. Every case has an executable local oracle and a baseline broken implementation or bounded edit target. Include both upgrade and legacy-pin failures, common and niche packages, and at least one deliberate no-lift control.

## API seam

Store case manifests under `benchmarks/version_guard/cases/` using the README benchmark contract. Fixtures must not require network access when their oracle runs. A corpus self-check command validates schema, unique IDs, relative safe paths, bounded file sizes, argv test commands, expected initial failure class, and that the oracle fails before an agent edit.

## Required coverage

At minimum represent Pydantic v1/v2 and SQLAlchemy 1.4/2.x semantics; remaining cases should use packages already available in the tested lock or tiny local API stubs that exactly reproduce documented version contracts. Clearly label synthetic stubs; never present them as live-package evidence.

## Visible artifact

`python scripts/check_version_guard_cases.py` prints every case ID and observed initial oracle failure, then emits a machine-readable corpus receipt.

## Verification

Run the corpus checker twice from clean temporary copies and compare case IDs/hashes. Verify every initial oracle fails for the declared reason, not import/setup failure. Run `ruff`, `compileall`, and `git diff --check` on changed paths.

## Stop rule

Stop if a case needs paid calls, arbitrary downloads, vendored proprietary content, or cannot distinguish wrong-version behavior from ordinary coding failure.
