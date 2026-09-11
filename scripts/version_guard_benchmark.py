"""Offline, argv-only benchmark harness for the Version Guard pilot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SCHEMA = "universal-docs.benchmark/v1"
ARMS = ("control", "manual", "automatic")
_SECRET = re.compile(
    r"(?i)(token|secret|password|api[_-]?key|authorization)([=:])([^\s,;]+)"
)


class BenchmarkError(ValueError):
    """Raised when a benchmark manifest violates the bounded contract."""


def _safe_text(value: Any) -> str:
    text = str(value)
    text = _SECRET.sub(r"\1\2[redacted]", text)
    text = re.sub(r"(?<![A-Za-z0-9_])/(?:Users|home|private|tmp|var)/[^\s,;]+", "[path]", text)
    return text


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative_path(root: Path, value: str, label: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise BenchmarkError(f"{label} must be relative")
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise BenchmarkError(f"{label} escapes its root") from exc
    return resolved


def _argv(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value or any(
        not isinstance(item, str) or not item or "\x00" in item for item in value
    ):
        raise BenchmarkError(f"{label} must be a non-empty argv array")
    return list(value)


def validate_manifest(manifest: Any) -> list[dict[str, Any]]:
    if not isinstance(manifest, dict) or not isinstance(manifest.get("cases"), list):
        raise BenchmarkError("manifest must contain a cases array")
    if not manifest["cases"] or len(manifest["cases"]) > 100:
        raise BenchmarkError("manifest cases must contain 1..100 entries")
    cases = []
    seen: set[str] = set()
    for raw in manifest["cases"]:
        if not isinstance(raw, dict):
            raise BenchmarkError("each case must be an object")
        required = ("id", "ecosystem", "package", "target_version", "task", "workspace_fixture", "test_command")
        if any(key not in raw for key in required):
            raise BenchmarkError("case is missing a required field")
        case_id = raw["id"]
        if not isinstance(case_id, str) or not case_id or case_id in seen or len(case_id) > 160:
            raise BenchmarkError("case ids must be unique, non-empty strings")
        seen.add(case_id)
        if not all(isinstance(raw[key], str) and raw[key] for key in required if key != "test_command"):
            raise BenchmarkError(f"invalid scalar field in case {case_id}")
        fixture_value = raw["workspace_fixture"]
        fixture_path = Path(fixture_value)
        if fixture_path.is_absolute() or ".." in fixture_path.parts:
            raise BenchmarkError(f"case {case_id} workspace_fixture must be relative")
        case = dict(raw)
        case["test_command"] = _argv(raw["test_command"], f"case {case_id} test_command")
        if "context_files" in raw and not isinstance(raw["context_files"], dict):
            raise BenchmarkError(f"case {case_id} context_files must be an object")
        cases.append(case)
    return cases


def _context_value(case: dict[str, Any], arm: str) -> str | None:
    values = case.get("context_files", {})
    value = values.get(arm)
    if value is None and arm != "control":
        value = case.get(f"{arm}_context_file")
    if value is not None and (not isinstance(value, str) or not value):
        raise BenchmarkError(f"case {case['id']} {arm} context file is invalid")
    return value


def _snapshot(root: Path) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".benchmark" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        snapshot[relative] = path.read_bytes()
    return snapshot


def _diff_hash(before: dict[str, bytes], after: dict[str, bytes]) -> str:
    changed = []
    for name in sorted(set(before) | set(after)):
        if before.get(name) != after.get(name):
            marker = after.get(name)
            changed.append(name.encode() + b"\0" + (marker if marker is not None else b"<deleted>"))
    return _sha256(b"".join(changed))


def _context_info(path: Path | None) -> tuple[str | None, int]:
    if path is None:
        return None, 0
    data = path.read_bytes()
    return _sha256(data), len(data)


def _env(workspace: Path) -> dict[str, str]:
    # Deliberately construct a tiny environment; do not inherit credentials.
    return {
        "PATH": os.defpath,
        "HOME": str(workspace / ".benchmark" / "home"),
        "TMPDIR": str(workspace / ".benchmark" / "tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def _write_inputs(workspace: Path, case: dict[str, Any], context: Path | None) -> None:
    metadata = {
        "id": case["id"],
        "ecosystem": case["ecosystem"],
        "package": case["package"],
        "target_version": case["target_version"],
        "task": _safe_text(case["task"]),
    }
    control = workspace / ".benchmark"
    (control / "home").mkdir(parents=True)
    (control / "tmp").mkdir()
    (control / "task.json").write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")
    if context is not None:
        shutil.copyfile(context, control / "context.txt")


def run_case(
    case: dict[str, Any],
    arm: str,
    fixture_root: Path,
    manifest_root: Path,
    agent_argv: list[str],
    *,
    provider: str | None = None,
    model: str | None = None,
    generation_id: str | None = None,
    timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    if arm not in ARMS:
        raise BenchmarkError(f"unknown arm: {arm}")
    agent_argv = _argv(agent_argv, "agent command")
    fixture = _relative_path(fixture_root, case["workspace_fixture"], "workspace_fixture")
    if not fixture.is_dir():
        raise BenchmarkError(f"workspace fixture not found for {case['id']}")
    context_name = _context_value(case, arm)
    context = None if context_name is None else _relative_path(manifest_root, context_name, "context file")
    if context is not None and not context.is_file():
        raise BenchmarkError(f"context file not found for {case['id']}")

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="version-guard-benchmark-") as temporary:
        workspace = Path(temporary) / "workspace"
        shutil.copytree(fixture, workspace)
        _write_inputs(workspace, case, context)
        before = _snapshot(workspace)
        context_sha256, context_bytes = _context_info(context)
        agent_status = "passed"
        agent_error = None
        try:
            completed = subprocess.run(
                agent_argv,
                cwd=workspace,
                env=_env(workspace),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout_seconds,
                check=False,
                shell=False,
                text=False,
            )
            agent_exit = completed.returncode
            if agent_exit != 0:
                agent_status = "failed"
        except subprocess.TimeoutExpired:
            agent_status = "timeout"
            agent_exit = None
            agent_error = "agent_timeout"
        except (FileNotFoundError, OSError):
            agent_status = "error"
            agent_exit = None
            agent_error = "agent_not_executable"

        test_status = "not_run"
        test_exit = None
        test_output = b""
        if agent_status == "passed":
            try:
                tested = subprocess.run(
                    _argv(case["test_command"], "test_command"),
                    cwd=workspace,
                    env=_env(workspace),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=timeout_seconds,
                    check=False,
                    shell=False,
                    text=False,
                )
                test_exit = tested.returncode
                test_output = tested.stdout
                test_status = "passed" if test_exit == 0 else "failed"
            except subprocess.TimeoutExpired as exc:
                test_status = "timeout"
                test_output = (exc.stdout or b"") if isinstance(exc.stdout, bytes) else b""
            except (FileNotFoundError, OSError):
                test_status = "error"
        after = _snapshot(workspace)
        result = {
            "id": case["id"],
            "arm": arm,
            "ecosystem": case["ecosystem"],
            "package": case["package"],
            "target_version": case["target_version"],
            "provider": provider,
            "model": model,
            "generation_id": generation_id,
            "context_sha256": context_sha256,
            "context_bytes": context_bytes,
            "workspace_diff_sha256": _diff_hash(before, after),
            "agent": {"status": agent_status, "exit_code": agent_exit, "error": agent_error},
            "test": {
                "command": [_safe_text(item) for item in case["test_command"]],
                "command_status": test_status,
                "exit_code": test_exit,
                "output_sha256": _sha256(test_output),
            },
            "elapsed_seconds": round(time.monotonic() - started, 6),
        }
        return result


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(results, key=lambda item: (item["id"], item["arm"]))
    passed = sum(item["test"]["command_status"] == "passed" for item in ordered)
    timeouts = sum(
        item["agent"]["status"] == "timeout" or item["test"]["command_status"] == "timeout"
        for item in ordered
    )
    errors = sum(
        item["agent"]["status"] == "error" or item["test"]["command_status"] == "error"
        for item in ordered
    )
    return {
        "cases": len(ordered),
        "passed": passed,
        "failed": len(ordered) - passed - timeouts - errors,
        "timeouts": timeouts,
        "errors": errors,
        "by_arm": {
            arm: sum(item["test"]["command_status"] == "passed" for item in ordered if item["arm"] == arm)
            for arm in ARMS
        },
    }


def run_benchmark(
    manifest: dict[str, Any],
    fixture_root: Path,
    manifest_root: Path,
    agent_argv: list[str],
    *,
    arms: tuple[str, ...] = ARMS,
    provider: str | None = None,
    model: str | None = None,
    generation_id: str | None = None,
    timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    cases = validate_manifest(manifest)
    if not arms or any(arm not in ARMS for arm in arms):
        raise BenchmarkError("arms must be named control, manual, or automatic")
    results = [
        run_case(
            case,
            arm,
            fixture_root,
            manifest_root,
            agent_argv,
            provider=provider,
            model=model,
            generation_id=generation_id,
            timeout_seconds=timeout_seconds,
        )
        for case in cases
        for arm in arms
    ]
    return {
        "schema": SCHEMA,
        "provider": provider,
        "model": model,
        "generation_id": generation_id,
        "results": sorted(results, key=lambda item: (item["id"], item["arm"])),
        "aggregate": aggregate(results),
    }


def markdown_summary(report: dict[str, Any]) -> str:
    aggregate_data = report["aggregate"]
    lines = [
        "# Version Guard benchmark",
        "",
        f"Schema: `{report['schema']}`",
        f"Cases: {aggregate_data['cases']} | passed: {aggregate_data['passed']} | "
        f"failed: {aggregate_data['failed']} | timeouts: {aggregate_data['timeouts']} | errors: {aggregate_data['errors']}",
        "",
        "| Case | Arm | Test | Context bytes | Diff hash |",
        "|---|---|---|---:|---|",
    ]
    for result in report["results"]:
        lines.append(
            f"| {result['id']} | {result['arm']} | {result['test']['command_status']} | "
            f"{result.get('context_bytes', 0)} | `{result.get('workspace_diff_sha256', '')[:12]}` |"
        )
    return "\n".join(lines) + "\n"


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--fixture-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary-output", type=Path, required=True)
    parser.add_argument("--agent-command", nargs="+")
    parser.add_argument("--agent-command-json")
    parser.add_argument("--provider")
    parser.add_argument("--model")
    parser.add_argument("--generation-id")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--arm", action="append", choices=ARMS)
    args = parser.parse_args()
    if bool(args.agent_command) == bool(args.agent_command_json):
        parser.error("provide exactly one of --agent-command or --agent-command-json")
    agent_argv = (
        json.loads(args.agent_command_json) if args.agent_command_json else args.agent_command
    )
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    report = run_benchmark(
        manifest,
        args.fixture_root,
        args.manifest.parent,
        agent_argv,
        arms=tuple(args.arm or ARMS),
        provider=args.provider,
        model=args.model,
        generation_id=args.generation_id,
        timeout_seconds=args.timeout_seconds,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.summary_output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.summary_output.write_text(markdown_summary(report), encoding="utf-8")
    print(json.dumps({"artifact": str(args.output), "summary": str(args.summary_output), **report["aggregate"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
