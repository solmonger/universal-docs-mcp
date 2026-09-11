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


def write_pair(
    tmp_path: Path, before: str, after: str, name: str = "requirements.txt"
) -> tuple[Path, Path]:
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
        (
            "pydantic==1.10.13\n",
            "pydantic==2.6.1\n",
            "exact_dependency_changed",
            "selected",
        ),
        (
            "pydantic==2.6.1\n",
            "pydantic==1.10.13\n",
            "exact_dependency_changed",
            "selected",
        ),
        ("requests==2.31.0\n", "", "dependency_removed", "abstained"),
        ("demo==1.0.0\n", "demo>=1.1.0\n", "target_version_unresolved", "abstained"),
        (
            "demo==1.0.0\n",
            "demo @ https://example.test/demo.whl\n",
            "non_registry_reference",
            "abstained",
        ),
        ("demo==1.0.0\n", "demo==1.0\n", "exact_dependency_changed", "selected"),
        ("demo==1.0.0\n", "demo==1.0.0\n", "dependency_unchanged", "abstained"),
        ("demo==1.0.0\n", "demo==1.0.1\n", "exact_dependency_changed", "selected"),
        ("Demo==1.0.0\n", "demo==1.0.1\n", "exact_dependency_changed", "selected"),
        (
            "demo==1.0.0\n",
            "demo==1.0.1; python_version >= '3.10'\n",
            "exact_dependency_changed",
            "selected",
        ),
        (
            "demo[socks]==1.0.0\n",
            "demo[socks]==1.0.1\n",
            "exact_dependency_changed",
            "selected",
        ),
        (
            "demo==1.0.0\nother==2.0.0\n",
            "demo==1.0.1\nother==2.0.0\n",
            "exact_dependency_changed",
            "selected",
        ),
        ("demo==1.0.0\n", "demo\n", "target_version_unresolved", "abstained"),
        ("demo==1.0.0\n", "demo===1.0.1\n", "exact_dependency_changed", "selected"),
        ("demo==1.0.0\n", "demo~=1.1\n", "target_version_unresolved", "abstained"),
        (
            "demo==1.0.0\n",
            "demo==1.0.1,!=1.0.2\n",
            "target_version_unresolved",
            "abstained",
        ),
        (
            "demo==1.0.0\n",
            "demo==1.0.1\nother==2.0.1\n",
            "multiple_dependency_changes",
            "abstained",
        ),
        (
            "demo==1.0.0\nother==2.0.0\n",
            "demo==1.0.1\nother==2.0.1\n",
            "multiple_dependency_changes",
            "abstained",
        ),
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
    assert (
        plan_dependency_changes(before, after, project_root=tmp_path).reason
        == "conflicting_dependency_pins"
    )
    before, after = write_pair(tmp_path, "demo==1.0.0\n", "demo==1.0.1\nDemo==1.0.1\n")
    assert (
        plan_dependency_changes(before, after, project_root=tmp_path).reason
        == "duplicate_dependency"
    )


def test_pyproject_is_supported_and_other_manifest_is_not(tmp_path):
    before, after = write_pair(
        tmp_path,
        "[project]\ndependencies=[]\n",
        "[project]\ndependencies=['demo==1.2.3']\n",
        "pyproject.toml",
    )
    assert (
        plan_dependency_changes(before, after, project_root=tmp_path).status
        == "selected"
    )
    before, after = write_pair(tmp_path, "{}", "{}", "package.json")
    assert (
        plan_dependency_changes(before, after, project_root=tmp_path).reason
        == "unsupported_manifest"
    )


def test_cli_selects_explicit_package_and_rejects_paths(tmp_path):
    before, after = write_pair(
        tmp_path, "demo==1.0.0\nother==2.0.0\n", "demo==1.0.1\nother==2.0.1\n"
    )
    output = io.BytesIO()
    code = product_cli.main(
        [
            "plan",
            "--project-root",
            str(tmp_path),
            "--before",
            str(before),
            "--after",
            str(after),
            "--package",
            "OTHER",
        ],
        stdout=output,
    )
    payload = json.loads(output.getvalue())
    assert (
        code == 0
        and payload["package"] == "other"
        and payload["target_version"] == "2.0.1"
    )
    output = io.BytesIO()
    code = product_cli.main(
        [
            "plan",
            "--project-root",
            str(tmp_path),
            "--before",
            "../outside",
            "--after",
            str(after),
        ],
        stdout=output,
    )
    assert (
        code == 1 and json.loads(output.getvalue())["reason"] == "invalid_plan_request"
    )


def test_cli_rejects_symlink_special_and_oversize(tmp_path):
    (tmp_path / "requirements-real.txt").write_text("demo==1.0.0\n")
    (tmp_path / "requirements-link.txt").symlink_to(tmp_path / "requirements-real.txt")
    output = io.BytesIO()
    product_cli.main(
        [
            "plan",
            "--project-root",
            str(tmp_path),
            "--before",
            "requirements-link.txt",
            "--after",
            "requirements-real.txt",
        ],
        stdout=output,
    )
    assert json.loads(output.getvalue())["reason"] == "malformed_manifest"
    (tmp_path / "requirements-big.txt").write_bytes(b"x" * (1024 * 1024 + 1))
    output = io.BytesIO()
    product_cli.main(
        [
            "plan",
            "--project-root",
            str(tmp_path),
            "--before",
            "requirements-big.txt",
            "--after",
            "requirements-real.txt",
        ],
        stdout=output,
    )
    assert json.loads(output.getvalue())["reason"] == "malformed_manifest"
    fifo = tmp_path / "requirements-fifo.txt"
    os.mkfifo(fifo)
    output = io.BytesIO()
    product_cli.main(
        [
            "plan",
            "--project-root",
            str(tmp_path),
            "--before",
            "requirements-fifo.txt",
            "--after",
            "requirements-real.txt",
        ],
        stdout=output,
    )
    assert json.loads(output.getvalue())["reason"] == "malformed_manifest"


def test_selected_plan_joins_preflight_and_delivery_fixture():
    plan = Plan(
        "selected",
        "exact_dependency_changed",
        "fixture-docs",
        "python",
        "1.0.0",
        "1.2.3",
        "requirements.txt",
    )
    request_model = to_preflight_request(plan)
    assert request_model.selection == "requested"
    assert request_model.exact_version == "1.2.3"
    result = successful_result("migration quick start")
    result["receipt"]["selection"]["budget_bytes"] = 4096
    result["receipt"]["integrity"] = context_integrity(
        result["context"], result["receipt"]
    )
    packet = build_context_packet(request_model, result)
    assert 'Target version: "1.2.3"' in packet
    assert "migration quick start" in packet


@pytest.mark.parametrize(
    ("before", "after", "package", "reason", "status", "expected_source"),
    [
        (
            "demo==1.0.0\nother==2.0.0\n",
            "demo>=2\nother==2.0.0\n",
            "demo",
            "target_version_unresolved",
            "abstained",
            "requirements.txt",
        ),
        (
            "demo==1.0.0\nother==2.0.0\n",
            "other==2.0.0\n",
            "demo",
            "dependency_removed",
            "abstained",
            "requirements.txt",
        ),
        (
            "demo==1.0.0\nother==2.0.0\n",
            "demo==1.0.0\nother==2.0.0\n",
            "demo",
            "dependency_unchanged",
            "abstained",
            "requirements.txt",
        ),
        (
            "demo==1.0.0\nother==2.0.0\n",
            "demo @ https://example.test/demo.whl\nother==2.0.0\n",
            "demo",
            "non_registry_reference",
            "abstained",
            "requirements.txt",
        ),
        (
            "demo>=1\nother==2.0.0\n",
            "demo==2.0.0\nother==2.0.0\n",
            "demo",
            "previous_version_unresolved",
            "abstained",
            "requirements.txt",
        ),
        (
            "demo==1.0.0\nother==2.0.0\n",
            "demo==1.1.0\nother==2.0.0\n",
            "missing",
            "unknown_package",
            "abstained",
            "requirements.txt",
        ),
    ],
)
def test_explicit_package_preserves_non_actionable_outcome(
    before, after, package, reason, status, expected_source, tmp_path
):
    before_path, after_path = write_pair(tmp_path, before, after)
    plan = plan_dependency_changes(
        before_path, after_path, project_root=tmp_path, package=package
    )
    assert (plan.reason, plan.status, plan.package, plan.resolution_source) == (
        reason,
        status,
        package if package != "missing" else "missing",
        expected_source,
    )


def test_canonical_name_uses_pep503():
    assert canonicalize_python_name("My_Package.Name") == "my-package-name"


def test_source_backed_rich_selection_reaches_preflight(tmp_path):
    before, after = write_pair(tmp_path, "rich==13.6.0\n", "rich==13.7.1\n")
    source = tmp_path / "app.py"
    source.write_text("from rich.console import Console\nConsole(highlight=False)\n")
    plan = plan_dependency_changes(
        before,
        after,
        project_root=tmp_path,
        task="upgrade the console usage",
        source_paths=("app.py",),
    )
    payload = plan.as_dict()
    assert plan.status == "selected"
    assert "13.7.1" in payload["query"]
    assert all(term in payload["query"] for term in ("rich", "Console", "highlight"))
    assert payload["selection"]["mode"] == "source_backed"
    assert payload["selection"]["source_files_inspected"] == 1
    request = to_preflight_request(plan)
    assert request.query == payload["query"]


def test_source_backed_bare_import_abstains_without_package_signal(tmp_path):
    before, after = write_pair(tmp_path, "rich==13.6.0\n", "rich==13.7.1\n")
    (tmp_path / "app.py").write_text("import rich\n")
    plan = plan_dependency_changes(
        before,
        after,
        project_root=tmp_path,
        task="upgrade Rich",
        source_paths=("app.py",),
    )
    payload = plan.as_dict()
    assert plan.status == "abstained"
    assert plan.reason == "task_signal_not_found"
    assert payload["query"] is None
    assert "selection" not in payload


def test_source_backed_alias_and_keywords_are_deterministic(tmp_path):
    before, after = write_pair(tmp_path, "rich==13.6.0\n", "rich==13.7.1\n")
    (tmp_path / "app.py").write_text("import rich as r\nr.print('x', markup=False)\n")
    plan = plan_dependency_changes(
        before,
        after,
        project_root=tmp_path,
        task="fix output",
        source_paths=("app.py",),
    )
    selection = plan.as_dict()["selection"]
    assert selection["symbols"] == ["markup", "print"]
    assert selection["aliases"] == ["r->rich"]


def test_source_backed_unrelated_and_task_only_abstain(tmp_path):
    before, after = write_pair(tmp_path, "rich==13.6.0\n", "rich==13.7.1\n")
    (tmp_path / "app.py").write_text("import requests\nrequests.get('x')\n")
    unrelated = plan_dependency_changes(
        before,
        after,
        project_root=tmp_path,
        task="fix requests",
        source_paths=("app.py",),
    )
    assert (
        unrelated.reason == "task_signal_not_found"
        and unrelated.as_dict()["query"] is None
    )
    task_only = plan_dependency_changes(
        before, after, project_root=tmp_path, task="fix console"
    )
    assert task_only.reason == "task_signal_source_required"


def test_source_backed_invalid_inputs_fail_closed(tmp_path):
    before, after = write_pair(tmp_path, "rich==13.6.0\n", "rich==13.7.1\n")
    (tmp_path / "bad.py").write_text("from rich import *\n")
    assert (
        plan_dependency_changes(
            before, after, project_root=tmp_path, task="x", source_paths=("bad.py",)
        ).reason
        == "task_signal_ambiguous"
    )
    (tmp_path / "syntax.py").write_text("from rich import Console(\n")
    assert (
        plan_dependency_changes(
            before, after, project_root=tmp_path, task="x", source_paths=("syntax.py",)
        ).reason
        == "task_signal_invalid"
    )


def test_source_backed_overlong_attribute_abstains_without_fabrication(tmp_path):
    before, after = write_pair(tmp_path, "rich==13.6.0\n", "rich==13.7.1\n")
    identifier = "x" * 70
    (tmp_path / "app.py").write_text(f"import rich\nrich.{identifier}\n")
    plan = plan_dependency_changes(
        before, after, project_root=tmp_path, task="x", source_paths=("app.py",)
    )
    payload = plan.as_dict()
    assert plan.status == "abstained"
    assert plan.reason == "task_signal_not_found"
    assert payload["query"] is None
    assert "selection" not in payload
    assert identifier not in str(payload)


def test_checker_addendum_empty_task_does_not_fall_back_generic(tmp_path):
    before, after = write_pair(tmp_path, "rich==13.6.0\n", "rich==13.7.1\n")
    plan = plan_dependency_changes(
        before, after, project_root=tmp_path, task="", source_paths=()
    )
    assert plan.reason == "task_signal_invalid"
    assert plan.as_dict()["query"] is None


def test_checker_addendum_symbols_beat_long_task(tmp_path):
    before, after = write_pair(tmp_path, "rich==13.6.0\n", "rich==13.7.1\n")
    (tmp_path / "app.py").write_text(
        "from rich.console import Console\nConsole(highlight=False)\n"
    )
    plan = plan_dependency_changes(
        before,
        after,
        project_root=tmp_path,
        task=" ".join(["verbose"] * 60),
        source_paths=("app.py",),
    )
    assert all(term in plan.query for term in ("Console", "highlight"))
    assert len(plan.query) <= 512


def test_checker_addendum_nested_chain_and_shadowing_fail_closed(tmp_path):
    before, after = write_pair(tmp_path, "rich==13.6.0\n", "rich==13.7.1\n")
    (tmp_path / "nested.py").write_text(
        "import rich\nrich.console.Console(highlight=False)\n"
    )
    nested = plan_dependency_changes(
        before, after, project_root=tmp_path, task="x", source_paths=("nested.py",)
    )
    assert all(
        term in nested.as_dict()["selection"]["symbols"]
        for term in ("console", "Console", "highlight")
    )
    (tmp_path / "shadow.py").write_text(
        "import rich as r\nimport requests as r\nr.get('x')\n"
    )
    shadowed = plan_dependency_changes(
        before, after, project_root=tmp_path, task="x", source_paths=("shadow.py",)
    )
    assert shadowed.reason == "task_signal_ambiguous"


def test_checker_addendum_identical_imports_and_nested_descriptor_paths(tmp_path):
    before, after = write_pair(tmp_path, "rich==13.6.0\n", "rich==13.7.1\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "app.py").write_text("import rich\nrich.console.Console()\n")
    (tmp_path / "other.py").write_text("import rich\n")
    plan = plan_dependency_changes(
        before,
        after,
        project_root=tmp_path,
        task="x",
        source_paths=("other.py", "pkg/app.py"),
    )
    assert plan.status == "selected"
    assert "Console" in plan.as_dict()["selection"]["symbols"]
    (tmp_path / "link").symlink_to(tmp_path / "pkg", target_is_directory=True)
    rejected = plan_dependency_changes(
        before, after, project_root=tmp_path, task="x", source_paths=("link/app.py",)
    )
    assert rejected.reason == "task_signal_invalid"


def test_checker_addendum_cli_rejects_duplicate_task_but_repeats_source(tmp_path):
    before, after = write_pair(tmp_path, "rich==13.6.0\n", "rich==13.7.1\n")
    (tmp_path / "app.py").write_text("import rich\nrich.print()\n")
    output = io.BytesIO()
    code = product_cli.main(
        [
            "plan",
            "--project-root",
            str(tmp_path),
            "--before",
            str(before),
            "--after",
            str(after),
            "--task",
            "first",
            "--task",
            "second",
        ],
        stdout=output,
    )
    assert (
        code == 1 and json.loads(output.getvalue())["reason"] == "invalid_plan_request"
    )
    output = io.BytesIO()
    code = product_cli.main(
        [
            "plan",
            "--project-root",
            str(tmp_path),
            "--before",
            str(before),
            "--after",
            str(after),
            "--task",
            "x",
            "--source",
            "app.py",
            "--source",
            "app.py",
        ],
        stdout=output,
    )
    assert code == 0


def test_source_paths_are_bounded_before_iteration(tmp_path):
    before, after = write_pair(tmp_path, "rich==13.6.0\n", "rich==13.7.1\n")
    seen = []

    def paths():
        for index in range(100):
            seen.append(index)
            if index == 9:
                raise AssertionError("unbounded source path consumption")
            yield "app.py"

    plan = plan_dependency_changes(
        before,
        after,
        project_root=tmp_path,
        task="x",
        source_paths=paths(),
    )
    assert plan.reason == "task_signal_invalid"
    assert seen == list(range(9))


def test_source_provenance_is_bounded_and_does_not_emit_raw_paths(tmp_path):
    before, after = write_pair(tmp_path, "rich==13.6.0\n", "rich==13.7.1\n")
    imports = "\n".join(
        f"from rich.{('m' + str(i))} import Thing{i}" for i in range(40)
    )
    (tmp_path / "app.py").write_text(imports + "\n")
    plan = plan_dependency_changes(
        before,
        after,
        project_root=tmp_path,
        task="x",
        source_paths=("app.py",),
    )
    selection = plan.as_dict()["selection"]
    assert len(selection["aliases"]) <= 24
    assert all(len(alias) <= 128 for alias in selection["aliases"])
    assert len(json.dumps(plan.as_dict()).encode()) < 16 * 1024
    assert str(tmp_path) not in json.dumps(plan.as_dict())


def test_task_contributed_only_when_a_task_term_enters_query(tmp_path):
    before, after = write_pair(tmp_path, "rich==13.6.0\n", "rich==13.7.1\n")
    (tmp_path / "app.py").write_text("from rich.console import Console\nConsole()\n")
    too_long = "x" * 450
    no_room = plan_dependency_changes(
        before, after, project_root=tmp_path, task=too_long, source_paths=("app.py",)
    )
    assert no_room.as_dict()["selection"]["task_contributed"] is False
    room = plan_dependency_changes(
        before, after, project_root=tmp_path, task="console", source_paths=("app.py",)
    )
    assert room.as_dict()["selection"]["task_contributed"] is True


@pytest.mark.parametrize("task", [123, "x\x00y", "x\x1fy", "x\x7fy", " " * 5000])
def test_invalid_task_values_fail_closed(tmp_path, task):
    before, after = write_pair(tmp_path, "rich==13.6.0\n", "rich==13.7.1\n")
    assert (
        plan_dependency_changes(
            before, after, project_root=tmp_path, task=task, source_paths=()
        ).reason
        == "task_signal_invalid"
    )


def test_descriptor_capability_failure_abstains(monkeypatch, tmp_path):
    before, after = write_pair(tmp_path, "rich==13.6.0\n", "rich==13.7.1\n")
    (tmp_path / "app.py").write_text("import rich\nrich.print()\n")
    from universal_docs_mcp import planner

    with monkeypatch.context() as capability:
        capability.setattr(planner.os, "supports_dir_fd", set())
        with pytest.raises(OSError, match="descriptor_relative"):
            planner._read_source_scoped(tmp_path, Path("app.py"))
    monkeypatch.setattr(
        planner,
        "_read_source_scoped",
        lambda root, relative: (_ for _ in ()).throw(
            OSError("descriptor_relative_traversal_unavailable")
        ),
    )
    plan = plan_dependency_changes(
        before, after, project_root=tmp_path, task="x", source_paths=("app.py",)
    )
    assert plan.reason == "task_signal_invalid"


def test_real_alias_named_symbols_does_not_collide(tmp_path):
    before, after = write_pair(tmp_path, "rich==13.6.0\n", "rich==13.7.1\n")
    (tmp_path / "app.py").write_text(
        "import rich as __symbols__\n__symbols__.print()\n"
    )
    plan = plan_dependency_changes(
        before, after, project_root=tmp_path, task="x", source_paths=("app.py",)
    )
    assert plan.status == "selected"
    assert "print" in plan.as_dict()["selection"]["symbols"]
    assert "__symbols__->rich" in plan.as_dict()["selection"]["aliases"]


def test_cli_source_options_reach_planner(tmp_path):
    before, after = write_pair(tmp_path, "rich==13.6.0\n", "rich==13.7.1\n")
    (tmp_path / "app.py").write_text(
        "from rich.console import Console\nConsole(highlight=False)\n"
    )
    output = io.BytesIO()
    code = product_cli.main(
        [
            "plan",
            "--project-root",
            str(tmp_path),
            "--before",
            str(before),
            "--after",
            str(after),
            "--task",
            "console",
            "--source",
            "app.py",
        ],
        stdout=output,
    )
    payload = json.loads(output.getvalue())
    assert code == 0 and payload["selection"]["mode"] == "source_backed"
