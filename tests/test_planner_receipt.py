"""Tests for the executable offline Gate B receipt."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.check_version_guard_planner import FIXTURES, produce

REQUIRED_IDS = {
    "add",
    "explicit-range",
    "remove",
    "unchanged",
    "direct-ref",
    "previous-unresolved",
    "absent-amid-other",
}


def test_receipt_producer_reconciles_rows_and_checked_in_semantics(tmp_path: Path):
    output = tmp_path / "receipt.json"
    receipt = produce(output)
    checked_in = json.loads(
        Path("benchmarks/version_guard/planner_receipt.json").read_text()
    )
    rows = receipt["fixture_outcomes"]
    assert receipt["fixture_count"] == len(FIXTURES) == len(rows)
    assert receipt["metrics"]["pass_count"] == len(rows)
    assert {row["id"] for row in rows} >= REQUIRED_IDS
    assert all(row["pass"] for row in rows)
    assert receipt["semantic_digest"] == checked_in["semantic_digest"]
    assert [
        {
            key: row[key]
            for key in (
                "id",
                "expected_status",
                "expected_reason",
                "actual_status",
                "actual_reason",
                "pass",
                "plan",
            )
        }
        for row in rows
    ] == [
        {
            key: row[key]
            for key in (
                "id",
                "expected_status",
                "expected_reason",
                "actual_status",
                "actual_reason",
                "pass",
                "plan",
            )
        }
        for row in checked_in["fixture_outcomes"]
    ]


def test_receipt_gate_b_thresholds_are_explicit(tmp_path: Path):
    receipt = produce(tmp_path / "receipt.json")
    metrics = receipt["metrics"]
    assert metrics["precision_percent"] >= 90
    assert metrics["required_abstention_percent"] == 100
    assert metrics["silent_latest_substitutions"] == 0
    assert metrics["median_planning_ms"] < 100
    assert metrics["all_plans_without_absolute_paths"]
