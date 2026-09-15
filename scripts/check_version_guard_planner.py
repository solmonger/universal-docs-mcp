"""Produce the bounded, offline Gate B planner receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from universal_docs_mcp.planner import plan_dependency_changes

FIXTURES = [
    ("add", "", "demo==1.0.0\n", None, "selected", "exact_dependency_added"),
    (
        "upgrade",
        "demo==1.0.0\n",
        "demo==2.0.0\n",
        None,
        "selected",
        "exact_dependency_changed",
    ),
    (
        "downgrade",
        "demo==2.0.0\n",
        "demo==1.0.0\n",
        None,
        "selected",
        "exact_dependency_changed",
    ),
    (
        "explicit-range",
        "demo==1.0.0\nother==2.0.0\n",
        "demo>=2\nother==2.0.0\n",
        "demo",
        "abstained",
        "target_version_unresolved",
    ),
    ("remove", "demo==1.0.0\n", "", None, "abstained", "dependency_removed"),
    (
        "unchanged",
        "demo==1.0.0\n",
        "demo==1.0.0\n",
        None,
        "abstained",
        "dependency_unchanged",
    ),
    (
        "direct-ref",
        "demo==1.0.0\n",
        "demo @ https://example.test/demo.whl\n",
        None,
        "abstained",
        "non_registry_reference",
    ),
    (
        "previous-unresolved",
        "demo>=1\n",
        "demo==2.0.0\n",
        "demo",
        "abstained",
        "previous_version_unresolved",
    ),
    (
        "absent-amid-other",
        "demo==1.0.0\nother==2.0.0\n",
        "demo==1.1.0\nother==2.1.0\n",
        "missing",
        "abstained",
        "unknown_package",
    ),
    (
        "canonical",
        "Demo_Pkg==1.0.0\n",
        "demo-pkg==2.0.0\n",
        None,
        "selected",
        "exact_dependency_changed",
    ),
    (
        "extras",
        "demo[socks]==1.0.0\n",
        "demo[socks]==1.1.0\n",
        None,
        "selected",
        "exact_dependency_changed",
    ),
    (
        "marker",
        "demo==1.0.0\n",
        "demo==1.1.0; python_version >= '3.10'\n",
        None,
        "selected",
        "exact_dependency_changed",
    ),
    (
        "multiple",
        "a==1.0.0\nb==1.0.0\n",
        "a==2.0.0\nb==2.0.0\n",
        None,
        "abstained",
        "multiple_dependency_changes",
    ),
    (
        "multiple-explicit",
        "a==1.0.0\nb==1.0.0\n",
        "a==2.0.0\nb==2.0.0\n",
        "b",
        "selected",
        "exact_dependency_changed",
    ),
    (
        "duplicate",
        "demo==1.0.0\n",
        "demo==1.1.0\nDemo==1.1.0\n",
        None,
        "abstained",
        "duplicate_dependency",
    ),
    (
        "conflict",
        "demo==1.0.0\n",
        "demo==1.1.0\ndemo==1.2.0\n",
        None,
        "abstained",
        "conflicting_dependency_pins",
    ),
    ("malformed", "demo==1.0.0\n", "demo [\n", None, "abstained", "malformed_manifest"),
    (
        "pep621",
        "[project]\ndependencies=[]\n",
        "[project]\ndependencies=['demo==1.0.0']\n",
        None,
        "selected",
        "exact_dependency_added",
    ),
    (
        "legacy",
        "demo==0.9.0\n",
        "demo==1\n",
        None,
        "selected",
        "exact_dependency_changed",
    ),
    (
        "compound",
        "demo==1.0.0\n",
        "demo>=1.0,<2.0\n",
        None,
        "abstained",
        "target_version_unresolved",
    ),
]


def _fixture_paths(
    root: Path, fixture_id: str, before: str, after: str
) -> tuple[Path, Path]:
    directory = root / fixture_id
    directory.mkdir()
    filename = "pyproject.toml" if fixture_id == "pep621" else "requirements.txt"
    before_path = directory / "before" / filename
    after_path = directory / "after" / filename
    before_path.parent.mkdir()
    after_path.parent.mkdir()
    before_path.write_text(before)
    after_path.write_text(after)
    return before_path, after_path


def _contains_absolute_path(value: Any) -> bool:
    if isinstance(value, str):
        return value.startswith("/") or (len(value) > 2 and value[1:3] == ":\\")
    if isinstance(value, dict):
        return any(_contains_absolute_path(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_absolute_path(item) for item in value)
    return False


def _semantic_rows(rows: list[dict[str, Any]]) -> bytes:
    fields = [
        {
            "id": row["id"],
            "expected_status": row["expected_status"],
            "expected_reason": row["expected_reason"],
            "actual_status": row["actual_status"],
            "actual_reason": row["actual_reason"],
            "pass": row["pass"],
            "plan": row["plan"],
        }
        for row in rows
    ]
    return json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()


def produce(output: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    timings: list[float] = []
    with tempfile.TemporaryDirectory(prefix="version-guard-planner-") as temporary:
        root = Path(temporary).resolve()
        for (
            fixture_id,
            before,
            after,
            package,
            expected_status,
            expected_reason,
        ) in FIXTURES:
            before_path, after_path = _fixture_paths(root, fixture_id, before, after)
            started = time.perf_counter()
            plan = plan_dependency_changes(
                before_path, after_path, project_root=root, package=package
            )
            timings.append((time.perf_counter() - started) * 1000)
            payload = plan.as_dict()
            passed = plan.status == expected_status and plan.reason == expected_reason
            rows.append(
                {
                    "id": fixture_id,
                    "expected_status": expected_status,
                    "expected_reason": expected_reason,
                    "actual_status": plan.status,
                    "actual_reason": plan.reason,
                    "pass": passed,
                    "plan": payload,
                }
            )

    semantic_digest = hashlib.sha256(_semantic_rows(rows)).hexdigest()
    selected = [row for row in rows if row["actual_status"] == "selected"]
    expected_abstained = [row for row in rows if row["expected_status"] == "abstained"]
    leaked = any(_contains_absolute_path(row["plan"]) for row in rows)
    metrics = {
        "all_plans_without_absolute_paths": not leaked,
        "median_planning_ms": round(statistics.median(timings), 3),
        "pass_count": sum(row["pass"] for row in rows),
        "precision_percent": round(
            100
            * sum(row["pass"] and row["actual_status"] == "selected" for row in rows)
            / len(selected),
            3,
        )
        if selected
        else 100.0,
        "required_abstention_percent": round(
            100
            * sum(row["pass"] and row["expected_status"] == "abstained" for row in rows)
            / len(expected_abstained),
            3,
        )
        if expected_abstained
        else 100.0,
        "silent_latest_substitutions": sum(
            row["actual_status"] == "selected" and not row["plan"].get("target_version")
            for row in rows
        ),
    }
    receipt = {
        "candidate_commit": "uncommitted_maker_object",
        "fixture_count": len(rows),
        "fixture_outcomes": rows,
        "metrics": metrics,
        "schema": "universal-docs.plan-benchmark/v1",
        "semantic_digest": semantic_digest,
        "status": "computed_offline",
    }
    encoded = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if len(encoded.encode()) > 64 * 1024:
        raise ValueError("receipt_too_large")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(encoded)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = produce(args.output)
    return 0 if receipt["metrics"]["pass_count"] == receipt["fixture_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
