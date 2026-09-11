"""Offline, argv-only benchmark harness for the Version Guard pilot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = "universal-docs.benchmark/v1"
ARMS = ("control", "manual", "automatic")
MAX_MANIFEST_BYTES = 256 * 1024
MAX_CASES = 100
MAX_CASE_KEYS = 13
MAX_STRING_BYTES = 16 * 1024
MAX_ARGV_ITEMS = 32
MAX_ARG_BYTES = 4 * 1024
MAX_CONTEXT_BYTES = 64 * 1024
MAX_ORACLE_BYTES = 64 * 1024
MAX_FIXTURE_FILES = 256
MAX_FIXTURE_FILE_BYTES = 256 * 1024
MAX_FIXTURE_TOTAL_BYTES = 2 * 1024 * 1024
MAX_SUBPROCESS_OUTPUT_BYTES = 64 * 1024
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


def _bounded_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise BenchmarkError(f"{label} must be a non-empty string")
    if len(value.encode("utf-8")) > MAX_STRING_BYTES:
        raise BenchmarkError(f"{label} exceeds bounded manifest string size")
    return value


def _argv(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > MAX_ARGV_ITEMS:
        raise BenchmarkError(f"{label} must contain 1..{MAX_ARGV_ITEMS} arguments")
    result = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item or "\x00" in item:
            raise BenchmarkError(f"{label}[{index}] must be a non-empty string")
        if len(item.encode("utf-8")) > MAX_ARG_BYTES:
            raise BenchmarkError(f"{label}[{index}] exceeds bounded argument size")
        result.append(item)
    return result


def validate_manifest(manifest: Any) -> list[dict[str, Any]]:
    if not isinstance(manifest, dict) or not isinstance(manifest.get("cases"), list):
        raise BenchmarkError("manifest must contain a cases array")
    if not manifest["cases"] or len(manifest["cases"]) > MAX_CASES:
        raise BenchmarkError(f"manifest cases must contain 1..{MAX_CASES} entries")
    cases = []
    seen: set[str] = set()
    required = {"id", "ecosystem", "package", "target_version", "task", "workspace_fixture", "test_command"}
    optional = {"context_files", "manual_context_file", "automatic_context_file", "oracle_file", "context_profile", "expected_failure_class", "evidence_type", "evidence_note"}
    for raw in manifest["cases"]:
        if not isinstance(raw, dict):
            raise BenchmarkError("each case must be an object")
        if len(raw) > MAX_CASE_KEYS or not set(raw) <= required | optional:
            raise BenchmarkError("case keys exceed the strict allowed set")
        if not required <= set(raw):
            raise BenchmarkError("case is missing a required field")
        case_id = _bounded_string(raw["id"], "case id")
        if case_id in seen:
            raise BenchmarkError("case ids must be unique, non-empty strings")
        seen.add(case_id)
        for key in required - {"test_command"}:
            _bounded_string(raw[key], f"case {case_id} {key}")
        fixture_path = Path(raw["workspace_fixture"])
        if fixture_path.is_absolute() or ".." in fixture_path.parts:
            raise BenchmarkError(f"case {case_id} workspace_fixture must be relative")
        case = dict(raw)
        case["test_command"] = _argv(raw["test_command"], f"case {case_id} test_command")
        if "context_files" in raw:
            values = raw["context_files"]
            if not isinstance(values, dict) or len(values) > len(ARMS):
                raise BenchmarkError(f"case {case_id} context_files must be a bounded object")
            for arm, value in values.items():
                if arm not in ARMS:
                    raise BenchmarkError(f"case {case_id} context_files has unknown arm")
                _bounded_string(value, f"case {case_id} {arm} context file")
        for key in ("manual_context_file", "automatic_context_file", "oracle_file", "context_profile", "expected_failure_class", "evidence_type", "evidence_note"):
            if key in raw:
                _bounded_string(raw[key], f"case {case_id} {key}")
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


def _validate_fixture_tree(root: Path) -> None:
    files = 0
    total = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".benchmark" in relative.parts:
            continue
        info = path.lstat()
        if path.is_symlink():
            raise BenchmarkError(f"fixture symlink is not allowed: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise BenchmarkError(f"fixture special file is not allowed: {relative}")
        files += 1
        if files > MAX_FIXTURE_FILES:
            raise BenchmarkError("fixture file count exceeds bound")
        if info.st_size > MAX_FIXTURE_FILE_BYTES:
            raise BenchmarkError(f"fixture file exceeds bound: {relative}")
        total += info.st_size
        if total > MAX_FIXTURE_TOTAL_BYTES:
            raise BenchmarkError("fixture total bytes exceed bound")


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
    info = path.lstat()
    if not path.is_file() or path.is_symlink():
        raise BenchmarkError("context file must be a regular file")
    if info.st_size > MAX_CONTEXT_BYTES:
        raise BenchmarkError("context file exceeds bounded bytes")
    data = path.read_bytes()
    return _sha256(data), len(data)


def _oracle_info(root: Path, value: str | None) -> tuple[Path | None, str | None, int]:
    if value is None:
        return None, None, 0
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise BenchmarkError("oracle_file must be relative")
    candidate = root.resolve()
    for component in relative.parts:
        candidate /= component
        try:
            info = candidate.lstat()
        except FileNotFoundError as exc:
            raise BenchmarkError("oracle file not found") from exc
        if stat.S_ISLNK(info.st_mode):
            raise BenchmarkError("oracle file path contains a symlink")
    try:
        info = candidate.lstat()
    except FileNotFoundError as exc:
        raise BenchmarkError("oracle file not found") from exc
    if not stat.S_ISREG(info.st_mode):
        raise BenchmarkError("oracle file must be a regular file")
    if info.st_size > MAX_ORACLE_BYTES:
        raise BenchmarkError("oracle file exceeds bounded bytes")
    data = candidate.read_bytes()
    return candidate, _sha256(data), len(data)


def _run_bounded(
    argv: list[str], *, cwd: Path, env: dict[str, str], timeout: float
) -> dict[str, Any]:
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        shell=False,
        start_new_session=(os.name == "posix"),
    )
    digest = hashlib.sha256()
    captured = bytearray()
    truncated = False

    def drain() -> None:
        nonlocal truncated
        assert process.stdout is not None
        while True:
            chunk = process.stdout.read(65536)
            if not chunk:
                return
            remaining = MAX_SUBPROCESS_OUTPUT_BYTES - len(captured)
            if remaining > 0:
                kept = chunk[:remaining]
                captured.extend(kept)
                digest.update(kept)
            if len(chunk) > max(remaining, 0):
                truncated = True

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    # Kill the entire process group to clean up descendants that may still hold
    # the stdout pipe open. This is needed on both timeout AND normal exit: if a
    # child inherited the pipe, the drain thread's read() will never see EOF
    # until the child is gone, so reader.join() would block indefinitely.
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    process.wait()
    if process.stdout is not None:
        process.stdout.close()
    reader.join()
    return {
        "returncode": None if timed_out else process.returncode,
        "status": "timeout" if timed_out else ("passed" if process.returncode == 0 else "failed"),
        "output_sha256": digest.hexdigest(),
        "output_bytes": len(captured),
        "output_truncated": truncated,
    }


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
    _validate_fixture_tree(fixture)
    context_name = _context_value(case, arm)
    context = None if context_name is None else _relative_path(manifest_root, context_name, "context file")
    if context is not None and not context.is_file():
        raise BenchmarkError(f"context file not found for {case['id']}")
    context_sha256, context_bytes = _context_info(context)
    oracle, oracle_sha256, oracle_bytes = _oracle_info(manifest_root, case.get("oracle_file"))

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="version-guard-benchmark-") as temporary:
        workspace = Path(temporary) / "workspace"
        shutil.copytree(fixture, workspace)
        _write_inputs(workspace, case, context)
        before = _snapshot(workspace)
        agent_status = "passed"
        agent_error = None
        agent_output = {"output_sha256": _sha256(b""), "output_bytes": 0, "output_truncated": False}
        try:
            agent_output = _run_bounded(
                agent_argv, cwd=workspace, env=_env(workspace), timeout=timeout_seconds
            )
            agent_exit = agent_output["returncode"]
            agent_status = agent_output["status"]
            if agent_status == "timeout":
                agent_error = "agent_timeout"
        except (FileNotFoundError, OSError):
            agent_status = "error"
            agent_exit = None
            agent_error = "agent_not_executable"

        test_status = "not_run"
        test_exit = None
        test_output = {"output_sha256": _sha256(b""), "output_bytes": 0, "output_truncated": False}
        test_command = _argv(case["test_command"], "test_command")
        if agent_status == "passed":
            try:
                if oracle is None:
                    test_output = _run_bounded(
                        test_command,
                        cwd=workspace,
                        env=_env(workspace),
                        timeout=timeout_seconds,
                    )
                else:
                    test_output = _run_bounded(
                        [sys.executable, str(oracle), str(workspace)],
                        cwd=workspace,
                        env=_env(workspace),
                        timeout=timeout_seconds,
                    )
                test_exit = test_output["returncode"]
                test_status = test_output["status"]
            except (FileNotFoundError, OSError):
                test_status = "error"
        after = _snapshot(workspace)
        test_receipt = {
            "command": (
                ["python3", "<external-oracle>", "<workspace>"]
                if oracle is not None
                else [_safe_text(item) for item in test_command]
            ),
            "command_status": test_status,
            "exit_code": test_exit,
            "output_sha256": test_output["output_sha256"],
            "output_bytes": test_output["output_bytes"],
            "output_truncated": test_output["output_truncated"],
        }
        if oracle is not None:
            test_receipt.update({"oracle_sha256": oracle_sha256, "oracle_bytes": oracle_bytes})
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
            "test": test_receipt,
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
    manifest_bytes = args.manifest.read_bytes()
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise BenchmarkError("manifest_too_large")
    manifest = json.loads(manifest_bytes.decode("utf-8"))
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
