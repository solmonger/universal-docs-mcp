"""Focused checks for the Slice 02 corpus contract."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
CHECKER = ROOT / "scripts" / "check_version_guard_cases.py"
RECEIPT = ROOT / "benchmarks" / "version_guard" / "corpus_receipt.json"


def test_corpus_checker_proves_all_initial_failures_are_version_errors():
    result = subprocess.run(
        [sys.executable, str(CHECKER)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    assert receipt["case_count"] >= 10
    assert len(receipt["case_ids"]) == receipt["case_count"]
    observations = [
        line for line in result.stdout.splitlines()
        if line.endswith(": wrong_version_api")
    ]
    assert len(observations) == receipt["case_count"]
    assert all(case["evidence_type"] == "synthetic_stub" for case in receipt["cases"])
    assert all(
        case["initial_failure"]["setup_failure"] is False
        for case in receipt["cases"]
    )


def test_corpus_cases_use_shared_argv_contract_and_safe_paths():
    manifests = sorted((ROOT / "benchmarks" / "version_guard" / "cases").glob("*.json"))
    assert len(manifests) >= 10
    for path in manifests:
        case = json.loads(path.read_text(encoding="utf-8"))
        assert set(case) == {
            "id", "ecosystem", "package", "target_version", "task",
            "workspace_fixture", "test_command", "context_profile",
            "expected_failure_class", "evidence_type", "evidence_note",
        }
        assert case["test_command"] == ["python", "oracle.py"]
        fixture = (ROOT / case["workspace_fixture"]).resolve()
        assert fixture.is_relative_to(ROOT.resolve())
        assert (fixture / "oracle.py").is_file()
        assert case["evidence_type"] == "synthetic_stub"
