"""The single product CLI dispatcher; Slice 04 owns ``plan``."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, BinaryIO, NoReturn

from .planner import plan_dependency_changes, to_preflight_request

MAX_OUTPUT_BYTES = 16 * 1024
_INIT_SCHEMA = "universal-docs.init/v1"
_HOOK_NAME = "universal-docs-command-hook"
_PREFLIGHT_NAME = "universal-docs-preflight"
_INIT_ERROR_REASONS = {
    "manifest_path_invalid", "manifest_path_escape", "project_root_invalid",
    "executable_must_be_absolute", "executable_unavailable", "executable_not_regular",
    "executable_not_executable", "settings_not_regular", "settings_invalid",
    "duplicate_json_key", "adapter_invalid", "adapter_backup_invalid",
    "settings_backup_invalid", "backup_conflict", "output_parent_invalid",
    "conflicting_universal_docs_hook", "write_failed", "init_invalid",
}


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(add_help=False)
    commands = parser.add_subparsers(dest="command")
    plan = commands.add_parser("plan", add_help=False)
    plan.add_argument("--project-root", required=True, type=Path)
    plan.add_argument("--before", required=True)
    plan.add_argument("--after", required=True)
    plan.add_argument("--package")
    init = commands.add_parser("init", add_help=False)
    init.add_argument("--harness", choices=("claude-code",), required=True)
    init.add_argument("--project-root", required=True, type=Path)
    init.add_argument("--before", required=True)
    init.add_argument("--after", required=True)
    init.add_argument("--package")
    init.add_argument("--apply", action="store_true")
    return parser


def _safe_relative(value: str, root: Path) -> Path:
    path = Path(value)
    if path.is_absolute() or not value or "\x00" in value:
        raise ValueError("manifest_path_invalid")
    candidate = (root / path).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError("manifest_path_escape") from None
    return path


def _validate_root(root: Path, *, absolute_required: bool = False) -> Path:
    try:
        if absolute_required and not root.is_absolute():
            raise ValueError("project_root_invalid")
        resolved = root.expanduser().resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("project_root_invalid")
        return resolved
    except (OSError, RuntimeError):
        raise ValueError("project_root_invalid") from None


def _error(reason: str, package: str | None = None) -> dict[str, object]:
    return {
        "schema": "universal-docs.plan/v1",
        "status": "abstained",
        "reason": reason,
        "package": package,
        "ecosystem": "python",
        "previous_version": None,
        "target_version": None,
        "resolution_source": "unknown",
        "query": None,
        "section_ids": [],
        "context_max_bytes": 4096,
        "freshness_mode": "require_check",
    }


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _installation_executable(name: str) -> Path:
    """Resolve a console script beside the running interpreter, never via PATH."""
    override = os.environ.get(
        "UNIVERSAL_DOCS_INIT_" + name.replace("-", "_").upper()
    )
    return Path(override) if override else Path(sys.executable).resolve().parent / name


def _validate_executable(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("executable_must_be_absolute")
    try:
        info = path.lstat()
    except OSError:
        raise ValueError("executable_unavailable") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("executable_not_regular")
    if not os.access(path, os.X_OK):
        raise ValueError("executable_not_executable")
    return path


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_json_constant(_: str) -> Any:
    raise ValueError("non_finite_json")


_MAX_INIT_FILE_BYTES = 64 * 1024


def _read_existing(path: Path, *, missing: bytes | None = None, reason: str) -> bytes | None:
    """Read a bounded regular file without following symlinks."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return missing
    except OSError:
        raise ValueError(reason) from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError(reason)
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_INIT_FILE_BYTES + 1)
    except OSError:
        raise ValueError(reason) from None
    if len(raw) > _MAX_INIT_FILE_BYTES:
        raise ValueError(reason)
    return raw


def _parse_settings(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        reason = str(exc) if str(exc) == "duplicate_json_key" else "settings_invalid"
        raise ValueError(reason) from None
    if not isinstance(value, dict):
        raise ValueError("settings_invalid")
    return value


def _read_settings(path: Path) -> tuple[dict[str, Any], bytes | None]:
    raw = _read_existing(path, missing=None, reason="settings_not_regular")
    if raw is None:
        return {}, None
    return _parse_settings(raw), raw


def _backup_path(root: Path, kind: str, preimage: bytes) -> Path:
    return root / ".universal-docs" / "backups" / f"{kind}-{_sha256(preimage)}.json"


def _validate_backup(path: Path, preimage: bytes, *, kind: str) -> None:
    raw = _read_existing(path, missing=None, reason=f"{kind}_backup_invalid")
    if raw is not None and raw != preimage:
        raise ValueError("backup_conflict")


def _write_backup(path: Path, raw: bytes, *, kind: str) -> None:
    """Create a content-addressed backup without ever replacing one."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        _validate_backup(path, raw, kind=kind)
        return
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def _restore_file(path: Path, raw: bytes | None, mode: int | None, mtime_ns: int | None) -> None:
    if raw is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    _atomic_write(path, raw)
    if mode is not None:
        os.chmod(path, mode)
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))


def _hook_command(hook: Path, adapter: Path) -> str:
    return shlex.join([str(hook), "--harness", "claude", "--config", str(adapter)])


def _validate_output_parent(root: Path, path: Path) -> None:
    """Reject existing output path components that could redirect writes."""
    try:
        relative_parent = path.parent.relative_to(root)
    except ValueError:
        raise ValueError("output_parent_invalid") from None
    current = root
    for component in relative_parent.parts:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise ValueError("output_parent_invalid") from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("output_parent_invalid")


def _is_universal(command: Any) -> bool:
    return isinstance(command, str) and _HOOK_NAME in command


def _settings_with_hook(settings: dict[str, Any], command: str) -> tuple[dict[str, Any], bool]:
    result = json.loads(json.dumps(settings))
    hooks = result.get("hooks")
    if hooks is None:
        hooks = {}
        result["hooks"] = hooks
    if not isinstance(hooks, dict):
        raise ValueError("settings_invalid")
    event = hooks.get("UserPromptSubmit")
    if event is None:
        event = []
        hooks["UserPromptSubmit"] = event
    if not isinstance(event, list):
        raise ValueError("settings_invalid")
    found = []
    for group in event:
        if not isinstance(group, dict) or not isinstance(group.get("hooks", []), list):
            raise ValueError("settings_invalid")
        for item in group["hooks"]:
            if not isinstance(item, dict):
                raise ValueError("settings_invalid")
            if _is_universal(item.get("command")):
                found.append(item)
    if len(found) > 1 or (found and found[0].get("command") != command):
        raise ValueError("conflicting_universal_docs_hook")
    if found:
        return result, False
    event.append({"hooks": [{"type": "command", "command": command, "timeout": 30}]})
    return result, True


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, separators=(",", ": ")) + "\n").encode("utf-8")


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _init_receipt(args: argparse.Namespace, root: Path) -> tuple[dict[str, Any], int]:
    plan = plan_dependency_changes(
        _safe_relative(args.before, root),
        _safe_relative(args.after, root),
        project_root=root,
        package=args.package,
    )
    receipt: dict[str, Any] = {
        "schema": _INIT_SCHEMA,
        "mode": "apply" if args.apply else "dry-run",
        "status": plan.status,
        "reason": plan.reason,
        "plan": plan.as_dict(),
        "proposed_relative_paths": [".universal-docs/adapter.json", ".claude/settings.json"],
        "hashes": {},
        "hook_identity": {"event": "UserPromptSubmit", "type": "command", "name": _HOOK_NAME},
        "rollback": {"instructions": "Remove the generated adapter and remove only the Universal Docs hook; never restore the whole settings file."},
    }
    if plan.status != "selected":
        return receipt, 1
    hook = _validate_executable(_installation_executable(_HOOK_NAME))
    preflight = _validate_executable(_installation_executable(_PREFLIGHT_NAME))
    request = to_preflight_request(plan)
    adapter_path = root / ".universal-docs" / "adapter.json"
    settings_path = root / ".claude" / "settings.json"
    _validate_output_parent(root, adapter_path)
    _validate_output_parent(root, settings_path)
    _validate_output_parent(root, root / ".universal-docs" / "backups" / "_potential_backup.json")
    adapter = _json_bytes({"preflight_command": [str(preflight)], "request": request.model_dump(mode="json", exclude_none=True), "timeout_ms": 30_000})
    command = _hook_command(hook, adapter_path)
    settings, old_settings = _read_settings(settings_path)
    old_adapter = _read_existing(adapter_path, missing=None, reason="adapter_invalid")
    if old_adapter is not None:
        try:
            value = json.loads(old_adapter.decode("utf-8"), parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise ValueError("adapter_invalid") from None
        if not isinstance(value, dict):
            raise ValueError("adapter_invalid")
    proposed, hook_added = _settings_with_hook(settings, command)
    settings_bytes = _json_bytes(proposed)
    adapter_changed = old_adapter != adapter
    settings_changed = hook_added and old_settings != settings_bytes
    changed = adapter_changed or settings_changed
    receipt["changed"] = changed
    receipt["hashes"] = {".universal-docs/adapter.json": _sha256(adapter), ".claude/settings.json": _sha256(settings_bytes)}
    receipt["hook_identity"].update({"command_sha256": _sha256(command.encode()), "command_template": shlex.join([str(hook), "--harness", "claude", "--config", ".universal-docs/adapter.json"])})
    receipt["rollback"].update({
        "settings_changed": settings_changed,
        "adapter": {
            "action": "restore_preimage" if old_adapter is not None else "remove_generated",
            "relative_path": ".universal-docs/adapter.json",
        },
        "settings": {"action": "remove_universal_docs_hook"},
    })

    backups: list[dict[str, str]] = []
    backup_writes: list[tuple[Path, bytes, str]] = []
    if adapter_changed and old_adapter is not None:
        path = _backup_path(root, "adapter", old_adapter)
        _validate_output_parent(root, path)
        _validate_backup(path, old_adapter, kind="adapter")
        backups.append({"kind": "adapter", "relative_path": str(path.relative_to(root)), "sha256": _sha256(old_adapter)})
        if not path.exists():
            backup_writes.append((path, old_adapter, "adapter"))
    if settings_changed and old_settings is not None:
        path = _backup_path(root, "settings", old_settings)
        _validate_output_parent(root, path)
        _validate_backup(path, old_settings, kind="settings")
        backups.append({"kind": "settings", "relative_path": str(path.relative_to(root)), "sha256": _sha256(old_settings)})
        if not path.exists():
            backup_writes.append((path, old_settings, "settings"))
    if backups:
        receipt["backups"] = backups

    if args.apply and changed:
        adapter_stat = adapter_path.stat() if old_adapter is not None else None
        try:
            for path, raw, kind in backup_writes:
                _write_backup(path, raw, kind=kind)
            if adapter_changed:
                _atomic_write(adapter_path, adapter)
            if settings_changed:
                _atomic_write(settings_path, settings_bytes)
        except (OSError, RuntimeError, ValueError):
            try:
                _restore_file(
                    adapter_path,
                    old_adapter,
                    stat.S_IMODE(adapter_stat.st_mode) if adapter_stat else None,
                    adapter_stat.st_mtime_ns if adapter_stat else None,
                )
            except (OSError, RuntimeError):
                pass
            raise ValueError("write_failed") from None
    return receipt, 0


def _emit(payload: dict[str, Any], output: BinaryIO) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) + 1 > MAX_OUTPUT_BYTES:
        encoded = json.dumps({"schema": _INIT_SCHEMA, "status": "abstained", "reason": "response_too_large"}, separators=(",", ":")).encode()
    output.write(encoded + b"\n")
    output.flush()


def main(argv: list[str] | None = None, *, stdout: BinaryIO | None = None) -> int:
    """Emit exactly one bounded JSON object and no diagnostics."""
    output = stdout or sys.stdout.buffer
    args: argparse.Namespace | None = None
    try:
        args = _parser().parse_args(argv)
        if args.command == "plan":
            root = _validate_root(args.project_root)
            before = _safe_relative(args.before, root)
            after = _safe_relative(args.after, root)
            payload = plan_dependency_changes(before, after, project_root=root, package=args.package).as_dict()
        elif args.command == "init":
            root = _validate_root(args.project_root, absolute_required=True)
            payload, code = _init_receipt(args, root)
            _emit(payload, output)
            return code
        else:
            raise ValueError("command_required")
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        if args is not None and args.command == "init":
            reason = str(exc)
            if reason not in _INIT_ERROR_REASONS:
                reason = "write_failed" if args.apply else "init_invalid"
            _emit({"schema": _INIT_SCHEMA, "mode": "apply" if args.apply else "dry-run", "status": "abstained", "reason": reason}, output)
            return 1
        payload = _error("invalid_plan_request")
    _emit(payload, output)
    return 0 if payload.get("status") == "selected" else 1


if __name__ == "__main__":
    raise SystemExit(main())
