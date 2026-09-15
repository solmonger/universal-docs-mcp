# Slice 04 — deterministic Python dependency-change planner

## Contract

Given trusted before/after manifest files under one configured project root, identify exact Python dependency additions/version changes, emit `universal-docs.plan/v1`, and abstain when exact target identity is unavailable. Feed selected plans into the existing `PreflightRequest`; do not fetch or format context in the planner.

## API seam

New module owns typed plan models and pure planning logic. Add a CLI that accepts explicit bounded before/after regular files and emits one JSON object. Reuse `lockfile.py`; do not duplicate requirement parsing. Never run git or a package manager and never inspect prompts, source files, imports, or stack traces.

## Visible artifact

A fixture changing `pydantic==1.10.13` to `pydantic==2.6.1` emits the shared selected plan; a range-only target emits the shared abstention shape. A joined test converts a selected plan into preflight and then into the existing bounded delivery packet.

## Verification

At least 20 fixtures cover additions, upgrades, downgrades/legacy pins, removals, ranges, malformed files, duplicates, extras/markers, redacted references, renamed/canonical package spellings, unchanged dependencies, and bounded multi-change selection. Gate B metrics are computed, not eyeballed. Existing lockfile, preflight, integrity, and delivery tests stay green.

## Stop rule

Stop if implementation requires task-text NLU, transitive resolution, a new manifest parser, silent latest fallback, or changes to receipt trust semantics.
