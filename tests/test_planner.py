"""Deterministic planner and CLI seam coverage (offline)."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from tests.test_context_cli import successful_result
from universal_docs_mcp import product_cli
from universal_docs_mcp.context_delivery import build_context_packet
from universal_docs_mcp.context_integrity import context_integrity
from universal_docs_mcp.planner import (
    Plan,
    canonicalize_python_name,
    plan_dependency_changes,
    to_preflight_request,
)


def write_pair(tmp_path: Path, before: str, after: str, name: str = "requirements.txt") -> tuple[Path, Path]:
    (tmp_path / "before").mkdir(exist_ok=True)
    (tmp_path / "after").mkdir(exist_ok=True)
    before_path = tmp_path / "before" / name
    after_path = tmp_path / "after" / name
    before_path.write_text(before)
    after_path.write_text(after)
    return before_path.relative_to(tmp_path), after_path.relative_to(tmp_path)


@pytest.mark.parametrize(
    ("before", "after", "reason", "status"),
    [
        ("", "pydantic==2.6.1\n", "exact_dependency_added", "selected"),
        ("pydantic==1.10.13\n", "pydantic==2.6.1\n", "exact_dependency_changed", "selected"),
        ("pydantic==2.6.1\n", "pydantic==1.10.13\n", "exact_dependency_changed", "selected"),
        ("requests==2.31.0\n", "", "dependency_removed", "abstained"),
        ("demo==1.0.0\n", "demo>=1.1.0\n", "target_version_unresolved", "abstained"),
        ("demo==1.0.0\n", "demo @ https://example.test/demo.whl\n", "non_registry_reference", "abstained"),
        ("demo==1.0.0\n", "demo==1.0\n", "exact_dependency_changed", "selected"),
        ("demo==1.0.0\n", "demo==1.0.0\n", "dependency_unchanged", "abstained"),
        ("demo==1.0.0\n", "demo==1.0.1\n", "exact_dependency_changed", "selected"),
        ("Demo==1.0.0\n", "demo==1.0.1\n", "exact_dependency_changed", "selected"),
        ("demo==1.0.0\n", "demo==1.0.1; python_version >= '3.10'\n", "exact_dependency_changed", "selected"),
        ("demo[socks]==1.0.0\n", "demo[socks]==1.0.1\n", "exact_dependency_changed", "selected"),
        ("demo==1.0.0\nother==2.0.0\n", "demo==1.0.1\nother==2.0.0\n", "exact_dependency_changed", "selected"),
        ("demo==1.0.0\n", "demo\n", "target_version_unresolved", "abstained"),
        ("demo==1.0.0\n", "demo===1.0.1\n", "exact_dependency_changed", "selected"),
        ("demo==1.0.0\n", "demo~=1.1\n", "target_version_unresolved", "abstained"),
        ("demo==1.0.0\n", "demo==1.0.1,!=1.0.2\n", "target_version_unresolved", "abstained"),
        ("demo==1.0.0\n", "demo==1.0.1\nother==2.0.1\n", "multiple_dependency_changes", "abstained"),
        ("demo==1.0.0\nother==2.0.0\n", "demo==1.0.1\nother==2.0.1\n", "multiple_dependency_changes", "abstained"),
        ("demo==1.0.0\n", "demo==1.0.1\n", "exact_dependency_changed", "selected"),
        ("demo==1.0.0\n", "demo==1.0.1\n", "exact_dependency_changed", "selected"),
        ("demo==1.0.0\n", "demo==1.0.1\n", "exact_dependency_changed", "selected"),
    ],
)
def test_planner_fixture(before, after, reason, status, tmp_path):
    before_path, after_path = write_pair(tmp_path, before, after)
    plan = plan_dependency_changes(before_path, after_path, project_root=tmp_path)
    assert plan.reason == reason
    assert plan.status == status
    assert len(json.dumps(plan.as_dict()).encode()) < 16 * 1024
    assert str(tmp_path) not in json.dumps(plan.as_dict())


def test_duplicate_and_conflicting_pins_fail_closed(tmp_path):
    before, after = write_pair(tmp_path, "demo==1.0.0\n", "demo==1.0.1\ndemo==1.0.2\n")
    assert plan_dependency_changes(before, after, project_root=tmp_path).reason == "conflicting_dependency_pins"
    before, after = write_pair(tmp_path, "demo==1.0.0\n", "demo==1.0.1\nDemo==1.0.1\n")
    assert plan_dependency_changes(before, after, project_root=tmp_path).reason == "duplicate_dependency"


def test_pyproject_is_supported_and_other_manifest_is_not(tmp_path):
    before, after = write_pair(tmp_path, "[project]\ndependencies=[]\n", "[project]\ndependencies=['demo==1.2.3']\n", "pyproject.toml")
    assert plan_dependency_changes(before, after, project_root=tmp_path).status == "selected"
    before, after = write_pair(tmp_path, "{}", "{}", "package.json")
    assert plan_dependency_changes(before, after, project_root=tmp_path).reason == "unsupported_manifest"


def test_cli_selects_explicit_package_and_rejects_paths(tmp_path):
    before, after = write_pair(tmp_path, "demo==1.0.0\nother==2.0.0\n", "demo==1.0.1\nother==2.0.1\n")
    output = io.BytesIO()
    code = product_cli.main(["plan", "--project-root", str(tmp_path), "--before", str(before), "--after", str(after), "--package", "OTHER"], stdout=output)
    payload = json.loads(output.getvalue())
    assert code == 0 and payload["package"] == "other" and payload["target_version"] == "2.0.1"
    output = io.BytesIO()
    code = product_cli.main(["plan", "--project-root", str(tmp_path), "--before", "../outside", "--after", str(after)], stdout=output)
    assert code == 1 and json.loads(output.getvalue())["reason"] == "invalid_plan_request"


def test_cli_rejects_symlink_special_and_oversize(tmp_path):
    (tmp_path / "requirements-real.txt").write_text("demo==1.0.0\n")
    (tmp_path / "requirements-link.txt").symlink_to(tmp_path / "requirements-real.txt")
    output = io.BytesIO()
    product_cli.main(["plan", "--project-root", str(tmp_path), "--before", "requirements-link.txt", "--after", "requirements-real.txt"], stdout=output)
    assert json.loads(output.getvalue())["reason"] == "malformed_manifest"
    (tmp_path / "requirements-big.txt").write_bytes(b"x" * (1024 * 1024 + 1))
    output = io.BytesIO()
    product_cli.main(["plan", "--project-root", str(tmp_path), "--before", "requirements-big.txt", "--after", "requirements-real.txt"], stdout=output)
    assert json.loads(output.getvalue())["reason"] == "malformed_manifest"
    fifo = tmp_path / "requirements-fifo.txt"
    os.mkfifo(fifo)
    output = io.BytesIO()
    product_cli.main(["plan", "--project-root", str(tmp_path), "--before", "requirements-fifo.txt", "--after", "requirements-real.txt"], stdout=output)
    assert json.loads(output.getvalue())["reason"] == "malformed_manifest"


def test_selected_plan_joins_preflight_and_delivery_fixture():
    plan = Plan("selected", "exact_dependency_changed", "fixture-docs", "python", "1.0.0", "1.2.3", "requirements.txt")
    request_model = to_preflight_request(plan)
    assert request_model.selection == "requested"
    assert request_model.exact_version == "1.2.3"
    result = successful_result("migration quick start")
    result["receipt"]["selection"]["budget_bytes"] = 4096
    result["receipt"]["integrity"] = context_integrity(
        result["context"], result["receipt"]
    )
    packet = build_context_packet(request_model, result)
    assert "Target version: \"1.2.3\"" in packet
    assert "migration quick start" in packet


def test_canonical_name_uses_pep503():
    assert canonicalize_python_name("My_Package.Name") == "my-package-name"
