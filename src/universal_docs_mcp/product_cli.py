"""The single product CLI dispatcher; Slice 04 owns ``plan``."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
import selectors
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, BinaryIO, NoReturn

from . import __version__
from .cache import DEFAULT_CACHE_DIR
from .command_hook import (
    COMMAND_HOOK_BLOCK_CODES,
    MAX_HOOK_OUTPUT_BYTES,
    load_config,
)
from .planner import plan_dependency_changes, to_preflight_request

MAX_OUTPUT_BYTES = 16 * 1024
_DOCTOR_SCHEMA = "universal-docs.doctor/v1"
_INIT_SCHEMA = "universal-docs.init/v1"
_ROLLBACK_SCHEMA = "universal-docs.rollback/v1"
_DOCTOR_MAX_FILE_BYTES = 64 * 1024


def _receipt_text(value: Any, *, limit: int = 256) -> str:
    text = value if isinstance(value, str) else str(value)
    text = "".join(char if char in "\t\n\r" or ord(char) >= 32 else "?" for char in text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _doctor_check(
    check_id: str, status: str, reason: str, **metadata: Any
) -> dict[str, Any]:
    return {"id": _receipt_text(check_id), "status": _receipt_text(status),
            "reason": _receipt_text(reason), "metadata": metadata}


def _doctor_read_json(
    path: Path, reason: str
) -> tuple[dict[str, Any] | None, bytes | None, str | None]:
    """Read one bounded regular JSON file without a symlink race."""
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return None, None, reason + "_not_regular"
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if stat.S_ISLNK(opened.st_mode) or not stat.S_ISREG(opened.st_mode):
                return None, None, reason + "_not_regular"
            raw = bytearray()
            while len(raw) <= _DOCTOR_MAX_FILE_BYTES:
                chunk = os.read(fd, min(16 * 1024, _DOCTOR_MAX_FILE_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            if len(raw) > _DOCTOR_MAX_FILE_BYTES:
                return None, None, reason + "_too_large"
        finally:
            os.close(fd)
        value = json.loads(
            bytes(raw).decode("utf-8"),
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_json_constant,
        )
        if not isinstance(value, dict):
            return None, bytes(raw), reason + "_invalid"
        return value, bytes(raw), None
    except FileNotFoundError:
        return None, None, reason + "_missing"
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError):
        return None, None, reason + "_invalid"


def _doctor_installation() -> dict[str, Any]:
    """Prove that the ordinary install belongs to this interpreter and distribution."""
    names = (_HOOK_NAME, _PREFLIGHT_NAME)
    overrides = {
        name: bool(os.environ.get("UNIVERSAL_DOCS_INIT_" + name.replace("-", "_").upper()))
        for name in names
    }
    override = any(overrides.values())
    raw_executable = Path(sys.executable) if isinstance(sys.executable, str) else Path("")
    result: dict[str, Any] = {
        "status": "fail",
        "reason": "fixture_override" if override else "installed_identity_unavailable",
        "module_path": None,
        "installed_version": None,
        # Only receipt-safe identities leave this function; never expose a home path.
        "executable": raw_executable.name or None,
        "owned": False,
        "provenance": "fixture" if override else "installed",
        "scripts": {},
    }

    if override:
        result["scripts"] = {
            name: {"status": "fail", "reason": "fixture_override", "owned": False,
                   "provenance": "fixture"}
            for name in names
        }
        return result

    if not raw_executable.is_absolute():
        result["reason"] = "active_interpreter_invalid"
        return result
    try:
        executable_info = raw_executable.lstat()
    except (OSError, ValueError):
        result["reason"] = "active_interpreter_missing"
        return result
    if raw_executable.is_symlink():
        try:
            resolved_executable = raw_executable.resolve(strict=True)
            resolved_info = resolved_executable.stat()
        except (OSError, RuntimeError, ValueError):
            result["reason"] = "active_interpreter_invalid"
            return result
    else:
        resolved_executable = raw_executable
        resolved_info = executable_info
    if not stat.S_ISREG(resolved_info.st_mode):
        result["reason"] = "active_interpreter_not_regular"
        return result
    if not resolved_info.st_mode & 0o111:
        result["reason"] = "active_interpreter_not_executable"
        return result
    # Keep the lexical parent: venv console scripts live beside the shim.
    scripts_dir = raw_executable.parent
    for name in names:
        script = scripts_dir / name
        try:
            _validate_executable(script)
        except ValueError as exc:
            result["scripts"][name] = {
                "status": "fail", "reason": str(exc), "owned": False,
                "provenance": "installed",
            }
            result["reason"] = "console_script_invalid"
            return result
        result["scripts"][name] = {
            "status": "pass", "reason": "ordinary_installation", "owned": True,
            "provenance": "installed",
        }

    try:
        module = importlib.import_module("universal_docs_mcp")
    except (ImportError, OSError, TypeError, ValueError):
        result["reason"] = "package_import_failed"
        return result
    raw_module_path = getattr(module, "__file__", None)
    if not isinstance(raw_module_path, str):
        result["reason"] = "module_file_missing"
        return result
    module_path = Path(raw_module_path)
    try:
        module_info = module_path.lstat()
    except (OSError, ValueError):
        result["reason"] = "module_file_missing"
        return result
    if stat.S_ISLNK(module_info.st_mode):
        result["reason"] = "module_file_symlink"
        return result
    if not stat.S_ISREG(module_info.st_mode):
        result["reason"] = "module_file_not_regular"
        return result
    if not module_path.is_file():
        result["reason"] = "module_file_missing"
        return result

    try:
        distribution = importlib.metadata.distribution("universal-docs-mcp")
        version = distribution.version
        files = distribution.files
        if not files:
            result["reason"] = "distribution_files_missing"
            return result
        owned_files = {
            Path(str(distribution.locate_file(item))).resolve() for item in files
        }
    except importlib.metadata.PackageNotFoundError:
        result["reason"] = "distribution_missing"
        return result
    except (OSError, TypeError, ValueError, RuntimeError, AttributeError):
        result["reason"] = "distribution_files_invalid"
        return result

    owned = module_path.resolve() in owned_files
    module_version = getattr(module, "__version__", None)
    result["installed_version"] = version if isinstance(version, str) else None
    result["module_path"] = module_path.name
    result["owned"] = owned
    if not owned:
        result["reason"] = "module_not_distribution_owned"
    elif version != module_version:
        result["reason"] = "distribution_module_version_mismatch"
    elif module_version != __version__:
        result["reason"] = "package_module_version_mismatch"
    else:
        result["status"] = "pass"
        result["reason"] = "identity_match"
    return result


def _doctor_probe(
    hook: Path, adapter: Path, request: dict[str, Any] | None
) -> tuple[dict[str, Any], str]:
    """Exercise the installed hook seam and return only bounded receipt metadata."""
    try:
        try:
            config = load_config(adapter)
        except (OSError, TypeError, ValueError):
            return {}, "probe_malformed"
        command = [str(hook), "--harness", "claude", "--config", str(adapter)]
        timeout = min(max(config.timeout_ms / 1000.0, 0.1), 5.0)
        env = {"PATH": "/usr/bin:/bin", "PYTHONIOENCODING": "utf-8"}
        parent_home = os.environ.get("HOME")
        if (
            isinstance(parent_home, str)
            and parent_home
            and os.path.isabs(parent_home)
            and "\x00" not in parent_home
        ):
            env["HOME"] = parent_home
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
        stdout, stderr = bytearray(), bytearray()
        selector = selectors.DefaultSelector()
        for stream, kind in ((proc.stdout, "out"), (proc.stderr, "err")):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream.fileno(), selectors.EVENT_READ, kind)
        stdin = proc.stdin
        try:
            os.write(stdin.fileno(), b"{}\n")
        except OSError:
            pass
        stdin.close()
        status = "completed"
        deadline = __import__("time").monotonic() + timeout
        parent_exit_deadline: float | None = None
        try:
            while selector.get_map():
                now = __import__("time").monotonic()
                if proc.poll() is not None and parent_exit_deadline is None:
                    # A descendant may retain the pipes after the parent exits. Kill
                    # the session immediately, then drain only the already-buffered bytes.
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except OSError:
                        pass
                    parent_exit_deadline = now + 0.25
                limit_deadline = parent_exit_deadline or deadline
                remaining = limit_deadline - now
                if remaining <= 0:
                    status = "probe_timeout" if parent_exit_deadline is None else "probe_descendant_timeout"
                    break
                for key, _ in selector.select(min(0.05, remaining)):
                    try:
                        chunk = os.read(key.fd, 8192)
                    except (BlockingIOError, InterruptedError):
                        continue
                    if not chunk:
                        selector.unregister(key.fd)
                        continue
                    target, limit, too_large = (
                        (stdout, MAX_HOOK_OUTPUT_BYTES, "probe_stdout_too_large")
                        if key.data == "out"
                        else (stderr, 16 * 1024, "probe_stderr_too_large")
                    )
                    target.extend(chunk[: max(0, limit + 1 - len(target))])
                    if len(target) > limit:
                        status = too_large
                        break
                if status != "completed":
                    break
        finally:
            selector.close()
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                try:
                    proc.kill()
                except OSError:
                    pass
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
        if status != "completed":
            return {"timeout_ms": int(timeout * 1000)}, status
        raw = bytes(stdout)
        if proc.returncode is None or proc.returncode < 0:
            return {"returncode": proc.returncode}, "probe_signal_exit"
        if proc.returncode != 0:
            return {"returncode": proc.returncode}, "probe_nonzero_exit"
        if not raw:
            return {}, "probe_no_stdout"
        if not raw.strip():
            return {}, "probe_whitespace_stdout"
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return {}, "probe_invalid_utf8"
        try:
            payload = json.loads(text, object_pairs_hook=_pairs_no_duplicates, parse_constant=_reject_json_constant)
        except ValueError as exc:
            if str(exc) == "duplicate_json_key":
                return {}, "probe_duplicate_json_keys"
            if isinstance(exc, json.JSONDecodeError) and "Extra data" in str(exc):
                return {}, "probe_multiple_json_values"
            return {}, "probe_malformed_json"
        if not isinstance(payload, dict):
            return {}, "probe_result_not_object"
        if payload.get("decision") == "block" and isinstance(payload.get("reason"), str):
            reason = payload["reason"]
            prefix = "Universal Docs preflight blocked this prompt: "
            suffix = ". No documentation context was injected."
            if reason.startswith(prefix):
                code = reason[len(prefix) :]
                if code.endswith(suffix):
                    code = code[: -len(suffix)]
                elif code.endswith("."):
                    code = code[:-1]
                else:
                    code = ""
                if code in COMMAND_HOOK_BLOCK_CODES:
                    return {}, code
            return {}, "probe_hook_rejected"
        if set(payload) != {"hookSpecificOutput"}:
            return {}, "probe_result_extra_fields"
        output = payload.get("hookSpecificOutput")
        if not isinstance(output, dict) or set(output) != {"hookEventName", "additionalContext"}:
            return {}, "probe_result_invalid_fields"
        if output.get("hookEventName") != "UserPromptSubmit":
            return {}, "probe_result_invalid_fields"
        context = output.get("additionalContext")
        if not isinstance(context, str):
            return {}, "probe_result_missing_required_fields"
        if context.count("--- BEGIN UNTRUSTED DOCUMENTATION DATA ---") != 1 or context.count("--- END UNTRUSTED DOCUMENTATION DATA ---") != 1:
            return {}, "probe_source_contract_invalid"
        fields: dict[str, str] = {}
        allowed = {"Target package", "Target version", "Source kind", "Source version binding", "Source SHA-256", "Freshness policy", "Freshness state"}
        for line in context.splitlines():
            if ": " in line:
                key, value = line.split(": ", 1)
                if key in allowed:
                    if key in fields:
                        return {}, "probe_source_metadata_invalid"
                    fields[key] = value
        if set(fields) != allowed:
            return {}, "probe_source_metadata_missing"
        expected_package = json.dumps(request.get("package")) if request and request.get("package") else None
        expected_version = json.dumps(request.get("requested_version") or request.get("version")) if request else None
        if ((expected_package and fields.get("Target package") != expected_package) or (expected_version and fields.get("Target version") != expected_version)):
            return {}, "probe_source_selection_mismatch"
        source_sha = fields["Source SHA-256"]
        if len(source_sha) != 64 or any(c not in "0123456789abcdef" for c in source_sha):
            return {}, "probe_source_metadata_invalid"
        try:
            freshness_policy = json.loads(fields["Freshness policy"])
            freshness_state = json.loads(fields["Freshness state"])
        except (TypeError, json.JSONDecodeError):
            return {}, "probe_source_metadata_invalid"
        if freshness_policy != (request or {}).get("freshness_mode") or freshness_state != "upstream_checked":
            return {}, "probe_source_freshness_invalid"

        def packet_value(key: str) -> str:
            value = json.loads(fields[key])
            allowed_values = {
                "Source kind": {"pypi_description", "npm_readme", "github_readme", "official_markdown"},
                "Source version binding": {"registry_version", "unverified_git_ref", "versioned_url"},
            }
            if not isinstance(value, str) or value not in allowed_values[key]:
                raise ValueError("probe_source_metadata_invalid")
            return value

        return {
            "returncode": 0,
            "probe_mode": "installed_hook",
            "package": request.get("package") if request else None,
            "version": (request.get("requested_version") or request.get("version")) if request else None,
            "section_ids": list(request.get("section_ids", [])) if request else [],
            "source_kind": packet_value("Source kind"),
            "source_version_binding": packet_value("Source version binding"),
            "source_sha256": source_sha,
            "freshness_policy": freshness_policy,
            "freshness_state": freshness_state,
            "packet_sha256": hashlib.sha256(context.encode()).hexdigest(),
            "packet_bytes": len(raw),
            "source_body_omitted": True,
        }, "source_bearing"
    except (OSError, TypeError, ValueError, RecursionError):
        return {}, "probe_contract_invalid"


_HOOK_NAME = "universal-docs-command-hook"
_PREFLIGHT_NAME = "universal-docs-preflight"
_INIT_ERROR_REASONS = {
    "manifest_path_invalid",
    "manifest_path_escape",
    "project_root_invalid",
    "executable_must_be_absolute",
    "executable_unavailable",
    "executable_not_regular",
    "executable_not_executable",
    "settings_not_regular",
    "settings_invalid",
    "duplicate_json_key",
    "adapter_invalid",
    "adapter_backup_invalid",
    "settings_backup_invalid",
    "backup_conflict",
    "output_parent_invalid",
    "conflicting_universal_docs_hook",
    "write_failed",
    "recovery_failed",
    "init_invalid",
    "active_install_conflict",
    "install_state_invalid",
    "recovery_required",
    "response_too_large",
    "arguments_too_large",
    "arguments_invalid",
}

# Public rollback reasons are deliberately finite.  Exception text is never a
# receipt field: it can contain paths, secrets, terminal controls, or megabytes.
_ROLLBACK_ERROR_REASONS = {
    "project_root_invalid", "output_parent_invalid", "recovery_required",
    "install_state_invalid", "install_state_missing", "rollback_tombstone_invalid",
    "rollback_adapter_invalid", "rollback_settings_invalid", "rollback_live_mismatch",
    "rollback_hook_ambiguous_or_missing", "rollback_backup_invalid", "rollback_state_invalid",
    "rollback_failed", "recovery_failed", "response_too_large", "arguments_too_large",
    "arguments_invalid",
}


def _known_reason(exc: BaseException, allowed: set[str], fallback: str) -> str:
    value = exc.args[0] if len(exc.args) == 1 else None
    return value if isinstance(value, str) and value in allowed else fallback


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError(message)


_DUPLICATE_GUARDED_OPTIONS: dict[str, set[str]] = {
    "init": {"--project-root", "--before", "--after", "--harness", "--apply"},
    "doctor": {"--project-root"},
    "rollback": {"--project-root", "--apply"},
    "plan": {"--project-root", "--before", "--after"},
}


def _guard_duplicate_options(raw_argv: list[Any]) -> str | None:
    """Reject ambiguous scalar/flag repetition before argparse sees argv."""
    command = raw_argv[0] if raw_argv and isinstance(raw_argv[0], str) else None
    guarded = _DUPLICATE_GUARDED_OPTIONS.get(command, set()) if command else set()
    if not guarded:
        return command
    counts = {option: 0 for option in guarded}
    for token in raw_argv[1:]:
        if isinstance(token, str) and token in counts:
            counts[token] += 1
    if any(count > 1 for count in counts.values()):
        raise ValueError("arguments_invalid")
    return command


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
    doctor = commands.add_parser("doctor", add_help=False)
    doctor.add_argument("--project-root", required=True, type=Path)
    rollback = commands.add_parser("rollback", add_help=False)
    rollback.add_argument("--project-root", required=True, type=Path)
    rollback.add_argument("--apply", action="store_true")
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
        # A project root is an authority boundary; do not silently redirect it.
        if root.is_symlink():
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
    override = os.environ.get("UNIVERSAL_DOCS_INIT_" + name.replace("-", "_").upper())
    return Path(override) if override else Path(sys.executable).parent / name


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


def _read_existing(
    path: Path, *, missing: bytes | None = None, reason: str
) -> bytes | None:
    """Read a bounded regular file without following a final symlink or race."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return missing
    except OSError:
        raise ValueError(reason) from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError(reason)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise ValueError(reason) from None
    try:
        opened = os.fstat(fd)
        if stat.S_ISLNK(opened.st_mode) or not stat.S_ISREG(opened.st_mode):
            raise ValueError(reason)
        chunks: list[bytes] = []
        total = 0
        while total <= _MAX_INIT_FILE_BYTES:
            chunk = os.read(fd, min(16 * 1024, _MAX_INIT_FILE_BYTES + 1 - total))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_INIT_FILE_BYTES:
                raise ValueError(reason)
    except ValueError:
        raise
    except OSError:
        raise ValueError(reason) from None
    finally:
        os.close(fd)
    raise AssertionError("unreachable")


def _validate_project_topology(root: Path) -> None:
    """Reject project control directories redirected through links or special files."""
    for relative in (
        Path(".universal-docs/adapter.json"),
        Path(".universal-docs/install-state.json"),
        Path(".universal-docs/rollback-tombstone.json"),
        Path(_RECOVERY_MARKER_RELATIVE_PATH),
        Path(".universal-docs/backups/_probe.json"),
        Path(".claude/settings.json"),
    ):
        _validate_output_parent(root, root / relative)


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


def _restore_file(
    path: Path, raw: bytes | None, mode: int | None, mtime_ns: int | None
) -> None:
    if raw is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    # Recovery must not call the injectable normal-write seam: if the forward
    # write failed, the rollback path still has to be able to restore bytes.
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.restore.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        if mode is not None:
            os.chmod(path, mode)
        if mtime_ns is not None:
            os.utime(path, ns=(mtime_ns, mtime_ns))
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


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


def _settings_has_universal_hook(settings: dict[str, Any]) -> bool:
    event = (
        settings.get("hooks", {}).get("UserPromptSubmit")
        if isinstance(settings.get("hooks"), dict)
        else None
    )
    return any(
        isinstance(item, dict) and _is_universal(item.get("command"))
        for group in event or []
        if isinstance(group, dict)
        for item in group.get("hooks", [])
    )


def _settings_with_hook(
    settings: dict[str, Any], command: str
) -> tuple[dict[str, Any], bool]:
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
    return (
        json.dumps(value, ensure_ascii=False, indent=2, separators=(",", ": ")) + "\n"
    ).encode("utf-8")


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


# State-backed onboarding helpers and the public dispatcher share this module so the
# plan/init wire contract remains centralized while rollback identity stays explicit.
_STATE_SCHEMA = "universal-docs.install-state/v1"
_STATE_RELATIVE_PATH = ".universal-docs/install-state.json"
_TOMBSTONE_RELATIVE_PATH = ".universal-docs/rollback-tombstone.json"
_RECOVERY_MARKER_RELATIVE_PATH = ".universal-docs/recovery-marker.json"
_RECOVERY_MARKER_SCHEMA = "universal-docs.recovery-marker/v2"
_RECOVERY_REQUIRED_REASON = "recovery_required"
_HASH_RE = __import__("re").compile(r"^[0-9a-f]{64}$")


def _relative_state_path(
    value: Any, root: Path, *, reason: str = "install_state_invalid"
) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(reason)
    path = Path(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(reason)
    candidate = root / path
    try:
        _validate_output_parent(root, candidate)
    except ValueError:
        raise ValueError(reason) from None
    return path


def _state_hash(value: Any, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise ValueError("install_state_invalid")
    return value


def _read_state(root: Path) -> tuple[dict[str, Any] | None, bytes | None]:
    path = root / _STATE_RELATIVE_PATH
    _validate_output_parent(root, path)
    raw = _read_existing(path, missing=None, reason="install_state_invalid")
    if raw is None:
        return None, None
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ValueError("install_state_invalid") from None
    if (
        not isinstance(value, dict)
        or value.get("schema") != _STATE_SCHEMA
        or set(value) != {"schema", "paths", "adapter", "hook", "settings"}
    ):
        raise ValueError("install_state_invalid")
    paths = value.get("paths")
    if not isinstance(paths, dict) or set(paths) != {"adapter", "settings", "state"}:
        raise ValueError("install_state_invalid")
    for key, expected in (
        ("adapter", ".universal-docs/adapter.json"),
        ("settings", ".claude/settings.json"),
        ("state", _STATE_RELATIVE_PATH),
    ):
        if _relative_state_path(paths.get(key), root) != Path(expected):
            raise ValueError("install_state_invalid")
    adapter = value.get("adapter")
    hook = value.get("hook")
    settings = value.get("settings")
    if (
        not isinstance(adapter, dict)
        or not isinstance(hook, dict)
        or not isinstance(settings, dict)
    ):
        raise ValueError("install_state_invalid")
    if (
        set(adapter) != {"sha256", "preimage_sha256", "backup"}
        or set(hook) != {"command_sha256", "argv_sha256"}
        or set(settings) != {"preimage_sha256"}
    ):
        raise ValueError("install_state_invalid")
    _state_hash(adapter.get("sha256"))
    _state_hash(adapter.get("preimage_sha256"), nullable=True)
    backup = adapter.get("backup")
    if backup is not None:
        backup_path = _relative_state_path(backup, root)
        if backup_path.parts[:2] != (".universal-docs", "backups"):
            raise ValueError("install_state_invalid")
        if Path(backup).name != f"adapter-{adapter['preimage_sha256']}.json":
            raise ValueError("install_state_invalid")
    elif adapter.get("preimage_sha256") is not None:
        raise ValueError("install_state_invalid")
    for key in ("command_sha256", "argv_sha256"):
        _state_hash(hook.get(key))
    _state_hash(settings.get("preimage_sha256"), nullable=True)
    return value, raw


def _read_tombstone(root: Path) -> tuple[dict[str, Any] | None, bytes | None]:
    path = root / _TOMBSTONE_RELATIVE_PATH
    _validate_output_parent(root, path)
    raw = _read_existing(path, missing=None, reason="rollback_tombstone_invalid")
    if raw is None:
        return None, None
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ValueError("rollback_tombstone_invalid") from None
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "adapter_sha256", "preimage_sha256"}
        or value.get("schema") != "universal-docs.rollback-tombstone/v1"
    ):
        raise ValueError("rollback_tombstone_invalid")
    _state_hash(value["adapter_sha256"])
    _state_hash(value["preimage_sha256"], nullable=True)
    return value, raw


def _read_recovery_marker(root: Path) -> tuple[dict[str, Any] | None, bytes | None]:
    path = root / _RECOVERY_MARKER_RELATIVE_PATH
    _validate_output_parent(root, path)
    raw = _read_existing(path, missing=None, reason="recovery_marker_invalid")
    if raw is None:
        return None, None
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ValueError("recovery_marker_invalid") from None
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema",
            "state_sha256",
            "adapter_sha256",
            "settings_sha256",
            "settings_backup_sha256",
            "preimage_sha256",
        }
        or value.get("schema") != _RECOVERY_MARKER_SCHEMA
    ):
        raise ValueError("recovery_marker_invalid")
    try:
        for key in (
            "state_sha256",
            "adapter_sha256",
            "settings_sha256",
            "settings_backup_sha256",
        ):
            _state_hash(value[key])
        _state_hash(value["preimage_sha256"], nullable=True)
    except ValueError:
        raise ValueError("recovery_marker_invalid") from None
    return value, raw


def _assert_no_recovery_marker(root: Path) -> None:
    marker, _ = _read_recovery_marker(root)
    if marker is not None:
        raise ValueError(_RECOVERY_REQUIRED_REASON)


def _recovery_marker_bytes(
    state_raw: bytes,
    state: dict[str, Any],
    settings_raw: bytes,
    settings_backup_raw: bytes,
) -> bytes:
    return _json_bytes(
        {
            "schema": _RECOVERY_MARKER_SCHEMA,
            "state_sha256": _sha256(state_raw),
            "adapter_sha256": state["adapter"]["sha256"],
            "settings_sha256": _sha256(settings_raw),
            "settings_backup_sha256": _sha256(settings_backup_raw),
            "preimage_sha256": state["adapter"]["preimage_sha256"],
        }
    )


def _state_for(
    root: Path,
    *,
    adapter: bytes,
    adapter_preimage: bytes | None,
    settings_preimage: bytes | None,
    hook: Path,
    adapter_path: Path,
) -> dict[str, Any]:
    argv = [str(hook), "--harness", "claude", "--config", str(adapter_path)]
    command = shlex.join(argv)
    return {
        "schema": _STATE_SCHEMA,
        "paths": {
            "adapter": ".universal-docs/adapter.json",
            "settings": ".claude/settings.json",
            "state": _STATE_RELATIVE_PATH,
        },
        "adapter": {
            "sha256": _sha256(adapter),
            "preimage_sha256": _sha256(adapter_preimage)
            if adapter_preimage is not None
            else None,
            "backup": f".universal-docs/backups/adapter-{_sha256(adapter_preimage)}.json"
            if adapter_preimage is not None
            else None,
        },
        "hook": {
            "command_sha256": _sha256(command.encode()),
            "argv_sha256": _sha256(json.dumps(argv, separators=(",", ":")).encode()),
        },
        "settings": {
            "preimage_sha256": _sha256(settings_preimage)
            if settings_preimage is not None
            else None
        },
    }


def _state_live_matches(
    state: dict[str, Any],
    adapter: bytes | None,
    settings: dict[str, Any],
    hook: Path,
    adapter_path: Path,
) -> bool:
    if adapter is None or _sha256(adapter) != state["adapter"]["sha256"]:
        return False
    command = _hook_command(hook, adapter_path)
    if _sha256(command.encode()) != state["hook"]["command_sha256"]:
        return False
    argv = [str(hook), "--harness", "claude", "--config", str(adapter_path)]
    if (
        _sha256(json.dumps(argv, separators=(",", ":")).encode())
        != state["hook"]["argv_sha256"]
    ):
        return False
    event = (
        settings.get("hooks", {}).get("UserPromptSubmit")
        if isinstance(settings.get("hooks"), dict)
        else None
    )
    matches = [
        item
        for group in event or []
        if isinstance(group, dict)
        for item in group.get("hooks", [])
        if isinstance(item, dict)
        and set(item) == {"type", "command", "timeout"}
        and item.get("type") == "command"
        and item.get("command") == command
        and item.get("timeout") == 30
    ]
    return len(matches) == 1


_DOCTOR_CACHE_MAX_ENTRIES = 256
_DOCTOR_CACHE_MAX_NAME_BYTES = 255
# These are the only filesystem entries DocsCache owns.  Doctor deliberately
# validates topology only; SQLite schema/content remains DocsCache's concern.
_DOCTOR_CACHE_OWNED_NAMES = frozenset(("cache.db", "cache.db-wal", "cache.db-shm"))
_DOCTOR_CACHE_UNSAFE_REASON = "cache_owned_entry_unsafe"
_DOCTOR_CACHE_ENTRY_ERROR_REASON = "cache_owned_entry_unreadable"


def _doctor_cache_metadata(scope: str, *, root_status: str, identity: str | None,
                           available: bool, entries: dict[str, Any] | None = None,
                           reason: str = "cache_owner_classified") -> dict[str, Any]:
    return {
        "scope": scope,
        "owner": "DocsCache",
        "provenance": "production_default" if scope == "default_user_installation" else "fixture_override",
        "path_semantics": "~/.cache/universal-docs-mcp" if scope == "default_user_installation" else "explicit fixture path (not production ownership)",
        "identity": identity,
        "available": available,
        "root_status": root_status,
        "reason": reason,
        "entries": entries or {
            "validity": "topology_only",
            "total": 0,
            "regular": 0,
            "symlink": 0,
            "directory": 0,
            "special": 0,
            "unreadable": 0,
            "oversized_names": 0,
            "names_truncated": False,
        },
    }


def _doctor_cache_entries(cache: Path) -> tuple[dict[str, Any], str]:
    counts = {
        "validity": "topology_only",
        "total": 0,
        "regular": 0,
        "symlink": 0,
        "directory": 0,
        "special": 0,
        "unreadable": 0,
        "oversized_names": 0,
        "names_truncated": False,
    }
    try:
        with os.scandir(cache) as directory:
            for entry in directory:
                if counts["total"] >= _DOCTOR_CACHE_MAX_ENTRIES:
                    counts["names_truncated"] = True
                    break
                counts["total"] += 1
                try:
                    name = entry.name
                    name_bytes = os.fsencode(name)
                    if len(name_bytes) > _DOCTOR_CACHE_MAX_NAME_BYTES:
                        counts["oversized_names"] += 1
                    mode = entry.stat(follow_symlinks=False).st_mode
                except (OSError, UnicodeError, ValueError):
                    counts["unreadable"] += 1
                    if entry.name in _DOCTOR_CACHE_OWNED_NAMES:
                        return counts, "unreadable_owned"
                    continue
                if stat.S_ISLNK(mode):
                    counts["symlink"] += 1
                    unsafe = name in _DOCTOR_CACHE_OWNED_NAMES
                elif stat.S_ISREG(mode):
                    counts["regular"] += 1
                    unsafe = False
                elif stat.S_ISDIR(mode):
                    counts["directory"] += 1
                    unsafe = name in _DOCTOR_CACHE_OWNED_NAMES
                else:
                    counts["special"] += 1
                    unsafe = name in _DOCTOR_CACHE_OWNED_NAMES
                if unsafe:
                    return counts, "unsafe_owned"
    except (OSError, RuntimeError):
        return counts, "error"
    return counts, "ok"


def _doctor_cache(root: Path) -> tuple[dict[str, Any], str]:
    """Classify the production cache without creating it or reading bodies.

    ``UNIVERSAL_DOCS_CACHE_DIR`` is deliberately retained only as an explicit
    test/fixture seam; it never changes the declared production owner.
    """
    configured = os.environ.get("UNIVERSAL_DOCS_CACHE_DIR")
    if configured is not None:
        cache = Path(configured).expanduser()
        scope = "explicit"
        identity_seed = f"DocsCache:fixture:{cache}"
        if not cache.is_absolute():
            return _doctor_cache_metadata(scope, root_status="invalid", identity=None,
                                          available=False, reason="explicit_path_not_absolute"), "fail"
    else:
        cache = DEFAULT_CACHE_DIR
        scope = "default_user_installation"
        identity_seed = "DocsCache:production-default:~/.cache/universal-docs-mcp"
    identity = _sha256(identity_seed.encode())
    try:
        info = cache.lstat()
    except FileNotFoundError:
        return _doctor_cache_metadata(scope, root_status="missing", identity=identity,
                                      available=False, reason="cache_missing_first_run"), "pass"
    except OSError:
        return _doctor_cache_metadata(scope, root_status="inaccessible", identity=identity,
                                      available=False, reason="cache_root_inaccessible"), "unknown"
    if stat.S_ISLNK(info.st_mode):
        return _doctor_cache_metadata(scope, root_status="symlink", identity=identity,
                                      available=False, reason="cache_root_symlink"), "fail"
    if not stat.S_ISDIR(info.st_mode):
        kind = "file" if stat.S_ISREG(info.st_mode) else "special"
        return _doctor_cache_metadata(scope, root_status=kind, identity=identity,
                                      available=False, reason=f"cache_root_{kind}"), "fail"
    entries, entry_status = _doctor_cache_entries(cache)
    if entry_status == "unsafe_owned":
        return _doctor_cache_metadata(scope, root_status="directory", identity=identity,
                                      available=False, entries=entries,
                                      reason=_DOCTOR_CACHE_UNSAFE_REASON), "fail"
    if entry_status == "unreadable_owned":
        return _doctor_cache_metadata(scope, root_status="directory", identity=identity,
                                      available=False, entries=entries,
                                      reason=_DOCTOR_CACHE_ENTRY_ERROR_REASON), "unknown"
    if entry_status != "ok":
        return _doctor_cache_metadata(scope, root_status="inaccessible", identity=identity,
                                      available=False, entries=entries,
                                      reason="cache_entries_inaccessible"), "unknown"
    return _doctor_cache_metadata(scope, root_status="directory", identity=identity,
                                  available=True, entries=entries), "pass"


def _doctor_receipt(root: Path) -> tuple[dict[str, Any], int]:
    _validate_project_topology(root)
    checks: list[dict[str, Any]] = []
    identity = _doctor_installation()
    checks.append(
        _doctor_check(
            "installed_identity",
            identity["status"],
            identity["reason"],
            module_identity=Path(identity["module_path"]).name
            if identity.get("module_path")
            else None,
            installed_version=identity.get("installed_version"),
            executable_identity=Path(identity["executable"]).name
            if identity.get("executable")
            else None,
        )
    )
    adapter_path = root / ".universal-docs" / "adapter.json"
    settings_path = root / ".claude" / "settings.json"
    adapter, adapter_raw, adapter_error = _doctor_read_json(adapter_path, "adapter")
    settings, settings_raw, settings_error = _doctor_read_json(
        settings_path, "settings"
    )
    checks.append(
        _doctor_check(
            "adapter_readback",
            "fail" if adapter_error else "pass",
            adapter_error or "valid",
        )
    )
    checks.append(
        _doctor_check(
            "settings_readback",
            "fail" if settings_error else "pass",
            settings_error or "valid",
        )
    )
    try:
        state, state_raw = _read_state(root)
        state_status, state_reason = (
            ("pass", "valid")
            if state is not None
            else ("fail", "install_state_missing")
        )
    except ValueError as exc:
        state, state_raw, state_status, state_reason = (
            None, None, "fail", _known_reason(exc, _INIT_ERROR_REASONS | _ROLLBACK_ERROR_REASONS, "doctor_invalid")
        )
    try:
        marker, _ = _read_recovery_marker(root)
        if marker is not None:
            state_status, state_reason = "fail", _RECOVERY_REQUIRED_REASON
    except ValueError as exc:
        marker, _, state_status, state_reason = (
            None, None, "fail", _known_reason(exc, _INIT_ERROR_REASONS | _ROLLBACK_ERROR_REASONS, "doctor_invalid")
        )
    try:
        tombstone, _ = _read_tombstone(root)
        if tombstone is not None and state is not None:
            state_status, state_reason = "fail", "rollback_tombstone_inconsistent"
        elif tombstone is not None:
            current = _read_existing(
                root / ".universal-docs/adapter.json",
                missing=None,
                reason="rollback_tombstone_invalid",
            )
            if (
                (tombstone["preimage_sha256"] is None and current is not None)
                or (
                    tombstone["preimage_sha256"] is not None
                    and (current is None or _sha256(current) != tombstone["adapter_sha256"])
                )
            ):
                state_status, state_reason = "fail", "rollback_tombstone_invalid"
            tombstone_settings, _, tombstone_settings_error = _doctor_read_json(
                root / ".claude/settings.json", "settings"
            )
            if tombstone_settings_error or tombstone_settings is None or _settings_has_universal_hook(tombstone_settings):
                state_status, state_reason = "fail", "rollback_tombstone_invalid"
    except ValueError as exc:
        state_status, state_reason = "fail", _known_reason(
            exc, _INIT_ERROR_REASONS | _ROLLBACK_ERROR_REASONS, "doctor_invalid"
        )
    if marker is not None:
        state_status, state_reason = "fail", _RECOVERY_REQUIRED_REASON
    checks.append(
        _doctor_check(
            "install_state",
            state_status,
            state_reason,
            state_sha256=_sha256(state_raw) if state_raw else None,
            generated_adapter_sha256=state.get("adapter", {}).get("sha256")
            if state
            else None,
            settings_preimage_sha256=state.get("settings", {}).get("preimage_sha256")
            if state
            else None,
        )
    )
    hook = _installation_executable(_HOOK_NAME)
    preflight = _installation_executable(_PREFLIGHT_NAME)
    try:
        _validate_executable(hook)
        _validate_executable(preflight)
        executable_ok = True
    except ValueError:
        executable_ok = False
    override_used = any(
        os.environ.get("UNIVERSAL_DOCS_INIT_" + name.replace("-", "_").upper())
        for name in (_HOOK_NAME, _PREFLIGHT_NAME)
    )
    checks.append(
        _doctor_check(
            "executables",
            "pass" if executable_ok else "fail",
            "fixture_override"
            if executable_ok and override_used
            else (
                "regular_current_installation"
                if executable_ok
                else "executable_invalid"
            ),
            hook=Path(hook).name,
            preflight=Path(preflight).name,
            provenance="fixture" if override_used else "installed",
        )
    )
    cache, cache_status = _doctor_cache(root)
    checks.append(
        _doctor_check(
            "cache_scope",
            cache_status,
            cache["reason"],
            scope=cache["scope"],
            identity=cache["identity"],
            available=cache["available"],
        )
    )
    expected = _hook_command(hook, adapter_path)
    event = (
        settings.get("hooks", {}).get("UserPromptSubmit")
        if isinstance(settings, dict) and isinstance(settings.get("hooks"), dict)
        else None
    )
    matches = [
        item
        for group in event or []
        if isinstance(group, dict)
        for item in group.get("hooks", [])
        if isinstance(item, dict)
        and set(item) == {"type", "command", "timeout"}
        and item.get("type") == "command"
        and item.get("command") == expected
        and item.get("timeout") == 30
    ]
    hook_ok = len(matches) == 1
    checks.append(
        _doctor_check(
            "harness_seam",
            "pass" if hook_ok else "fail",
            "exact_hook" if hook_ok else "hook_missing_conflicting_or_mismatched",
            count=len(matches),
        )
    )
    adapter_ok = False
    try:
        parsed = load_config(adapter_path)
        adapter_ok = (
            parsed.command == (str(preflight),)
            and parsed.timeout_ms == 30_000
            and isinstance(adapter, dict)
            and isinstance(adapter.get("request"), dict)
        )
    except (OSError, ValueError, TypeError):
        pass
    checks.append(
        _doctor_check(
            "adapter_contract",
            "pass" if adapter_ok else "fail",
            "exact_contract" if adapter_ok else "adapter_contract_invalid",
            live_sha256=_sha256(adapter_raw) if adapter_raw else None,
        )
    )
    state_live = bool(
        state
        and settings is not None
        and _state_live_matches(state, adapter_raw, settings, hook, adapter_path)
    )
    checks.append(
        _doctor_check(
            "install_state_live_binding",
            "pass" if state_live else "fail",
            "hashes_and_hook_match" if state_live else "state_live_mismatch",
        )
    )
    probe_meta, probe_reason = (
        _doctor_probe(
            hook,
            adapter_path,
            adapter.get("request") if isinstance(adapter, dict) else None,
        )
        if hook_ok and adapter_ok and executable_ok
        else ({}, "probe_prerequisite_failed")
    )
    if probe_reason == "source_bearing":
        # The real hook/preflight may own a first-run cache write.  Re-read
        # topology after it, but never make classification itself create state.
        cache, cache_status = _doctor_cache(root)
        cache_check_index = next(
            index for index, check in enumerate(checks) if check["id"] == "cache_scope"
        )
        checks[cache_check_index] = _doctor_check(
            "cache_scope",
            cache_status,
            cache["reason"],
            scope=cache["scope"],
            identity=cache["identity"],
            available=cache["available"],
            owner=cache["owner"],
            provenance=cache["provenance"],
            root_status=cache["root_status"],
            entries=cache["entries"],
        )
    checks.append(
        _doctor_check(
            "source_probe",
            "pass" if probe_reason == "source_bearing" else "fail",
            probe_reason,
            **probe_meta,
        )
    )
    status = "pass" if all(c["status"] == "pass" for c in checks) else "fail"
    command = ["universal-docs", "rollback", "--project-root", "<project-root>", "--apply"]
    receipt = {
        "schema": _DOCTOR_SCHEMA,
        "status": status,
        "checks": checks,
        "cache": cache,
        "source_probe": probe_meta,
        "rollback": {
            "instructions": "Remove only the state-bound adapter and hook; preserve unrelated settings.",
            "command": shlex.join(command),
            "argv": command,
            "state_sha256": _sha256(state_raw) if state_raw else None,
            "live_adapter_sha256": _sha256(adapter_raw) if adapter_raw else None,
        },
    }
    return _bounded_doctor_receipt(receipt), 0 if status == "pass" else 1


def _init_receipt(args: argparse.Namespace, root: Path) -> tuple[dict[str, Any], int]:
    _assert_no_recovery_marker(root)
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
        "proposed_relative_paths": [
            ".universal-docs/adapter.json",
            ".claude/settings.json",
            _STATE_RELATIVE_PATH,
        ],
        "hashes": {},
        "hook_identity": {
            "event": "UserPromptSubmit",
            "type": "command",
            "name": _HOOK_NAME,
        },
        "rollback": {
            "instructions": "Use only install-state.json to remove the generated hook and restore the hash-bound adapter preimage."
        },
    }
    if plan.status != "selected":
        return receipt, 1
    hook = _validate_executable(_installation_executable(_HOOK_NAME))
    preflight = _validate_executable(_installation_executable(_PREFLIGHT_NAME))
    adapter_path, settings_path, state_path = (
        root / ".universal-docs/adapter.json",
        root / ".claude/settings.json",
        root / _STATE_RELATIVE_PATH,
    )
    for path in (
        adapter_path,
        settings_path,
        state_path,
        root / ".universal-docs/backups/_probe.json",
    ):
        _validate_output_parent(root, path)
    request = to_preflight_request(plan)
    adapter_bytes = _json_bytes(
        {
            "preflight_command": [str(preflight)],
            "request": request.model_dump(mode="json", exclude_none=True),
            "timeout_ms": 30_000,
        }
    )
    settings, old_settings = _read_settings(settings_path)
    old_adapter = _read_existing(adapter_path, missing=None, reason="adapter_invalid")
    if old_adapter is not None:
        try:
            value = json.loads(
                old_adapter.decode("utf-8"), parse_constant=_reject_json_constant
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise ValueError("adapter_invalid") from None
        if not isinstance(value, dict):
            raise ValueError("adapter_invalid")
    existing_state, existing_state_raw = _read_state(root)
    _read_tombstone(root)
    command = _hook_command(hook, adapter_path)
    proposed, hook_added = _settings_with_hook(settings, command)
    settings_bytes = _json_bytes(proposed)
    state = _state_for(
        root,
        adapter=adapter_bytes,
        adapter_preimage=old_adapter,
        settings_preimage=old_settings,
        hook=hook,
        adapter_path=adapter_path,
    )
    receipt["changed"] = (
        old_adapter != adapter_bytes
        or old_settings != settings_bytes
        or existing_state_raw is None
    )
    receipt["hashes"] = {
        ".universal-docs/adapter.json": _sha256(adapter_bytes),
        ".claude/settings.json": _sha256(settings_bytes),
        _STATE_RELATIVE_PATH: _sha256(_json_bytes(state)),
    }
    receipt["hook_identity"].update(
        {
            "command_sha256": state["hook"]["command_sha256"],
            "argv_sha256": state["hook"]["argv_sha256"],
            "command_template": shlex.join(
                [
                    str(hook),
                    "--harness",
                    "claude",
                    "--config",
                    ".universal-docs/adapter.json",
                ]
            ),
        }
    )
    backup_specs = []
    if old_adapter is not None:
        backup_specs.append(
            (_backup_path(root, "adapter", old_adapter), old_adapter, "adapter")
        )
    if old_settings is not None:
        backup_specs.append(
            (_backup_path(root, "settings", old_settings), old_settings, "settings")
        )
    receipt["backups"] = [
        {
            "kind": kind,
            "relative_path": str(path.relative_to(root)),
            "sha256": _sha256(raw),
        }
        for path, raw, kind in backup_specs
    ]
    _assert_receipt_fits(receipt, _INIT_SCHEMA)
    if existing_state is not None:
        live_matches = _state_live_matches(
            existing_state, old_adapter, settings, hook, adapter_path
        )
        desired_identity_matches = (
            existing_state["adapter"]["sha256"] == state["adapter"]["sha256"]
            and existing_state["hook"]["command_sha256"]
            == state["hook"]["command_sha256"]
            and existing_state["hook"]["argv_sha256"] == state["hook"]["argv_sha256"]
        )
        if live_matches and desired_identity_matches:
            receipt["changed"] = False
            return receipt, 0
        raise ValueError("active_install_conflict")
    if not args.apply or not receipt["changed"]:
        return receipt, 0
    for path, raw, kind in backup_specs:
        _validate_output_parent(root, path)
        _validate_backup(path, raw, kind=kind)
    created_backups: list[Path] = []
    adapter_stat = adapter_path.stat() if old_adapter is not None else None
    settings_stat = settings_path.stat() if old_settings is not None else None
    state_raw = _json_bytes(state)
    tombstone_path = root / _TOMBSTONE_RELATIVE_PATH
    old_tombstone_raw = _read_existing(
        tombstone_path, missing=None, reason="rollback_tombstone_invalid"
    )
    backup_specs = []
    if old_adapter is not None:
        backup_specs.append(
            (_backup_path(root, "adapter", old_adapter), old_adapter, "adapter")
        )
    if old_settings is not None:
        backup_specs.append(
            (_backup_path(root, "settings", old_settings), old_settings, "settings")
        )
    try:
        for path, raw, kind in backup_specs:
            _validate_output_parent(root, path)
            existed = path.exists()
            _write_backup(path, raw, kind=kind)
            if not existed:
                created_backups.append(path)
        if old_adapter != adapter_bytes:
            _atomic_write(adapter_path, adapter_bytes)
        if old_settings != settings_bytes:
            _atomic_write(settings_path, settings_bytes)
        _atomic_write(state_path, state_raw)
        tombstone_path.unlink(missing_ok=True)
    except (OSError, RuntimeError, ValueError):
        recovery_failed = False
        try:
            _restore_file(
                adapter_path,
                old_adapter,
                stat.S_IMODE(adapter_stat.st_mode) if adapter_stat else None,
                adapter_stat.st_mtime_ns if adapter_stat else None,
            )
            _restore_file(
                settings_path,
                old_settings,
                stat.S_IMODE(settings_stat.st_mode) if settings_stat else None,
                settings_stat.st_mtime_ns if settings_stat else None,
            )
            _restore_file(state_path, existing_state_raw, None, None)
            _restore_file(
                tombstone_path,
                old_tombstone_raw,
                0o600 if old_tombstone_raw is not None else None,
                None,
            )
            for path in created_backups:
                path.unlink(missing_ok=True)
        except (OSError, RuntimeError, ValueError):
            recovery_failed = True
            # A state file is the sole activation marker.  Best effort removal
            # prevents a partially recovered install from claiming live files.
            try:
                state_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise ValueError("recovery_failed" if recovery_failed else "write_failed") from None
    return receipt, 0


def _rollback_receipt(root: Path, apply: bool) -> tuple[dict[str, Any], int]:
    _validate_project_topology(root)
    _assert_no_recovery_marker(root)
    state, state_raw = _read_state(root)
    adapter_path, settings_path, state_path = (
        root / ".universal-docs/adapter.json",
        root / ".claude/settings.json",
        root / _STATE_RELATIVE_PATH,
    )
    if state is None:
        tombstone, _ = _read_tombstone(root)
        if tombstone is not None:
            adapter = _read_existing(
                adapter_path, missing=None, reason="rollback_tombstone_invalid"
            )
            if (adapter is None and tombstone["preimage_sha256"] is not None) or (
                adapter is not None and _sha256(adapter) != tombstone["adapter_sha256"]
            ):
                raise ValueError("rollback_tombstone_invalid")
            settings, _, settings_error = _doctor_read_json(
                root / ".claude/settings.json", "settings"
            )
            if settings_error or settings is None or _settings_has_universal_hook(settings):
                raise ValueError("rollback_tombstone_invalid")
            return {
                "schema": _ROLLBACK_SCHEMA,
                "mode": "apply" if apply else "dry-run",
                "status": "selected",
                "reason": "already_rolled_back",
                "changed": False,
            }, 0
        adapter = _read_existing(
            adapter_path, missing=None, reason="rollback_state_invalid"
        )
        settings, _, err = _doctor_read_json(settings_path, "settings")
        if (
            adapter is None
            and not err
            and not (
                isinstance(settings, dict)
                and any(
                    isinstance(item, dict)
                    and _HOOK_NAME in str(item.get("command", ""))
                    for group in settings.get("hooks", {}).get("UserPromptSubmit", [])
                    if isinstance(group, dict)
                    for item in group.get("hooks", [])
                )
            )
        ):
            return {
                "schema": _ROLLBACK_SCHEMA,
                "mode": "apply" if apply else "dry-run",
                "status": "selected",
                "reason": "already_rolled_back",
                "changed": False,
            }, 0
        raise ValueError("install_state_missing")
    adapter_raw = _read_existing(
        adapter_path, missing=None, reason="rollback_adapter_invalid"
    )
    settings, settings_raw, settings_error = _doctor_read_json(
        settings_path, "settings"
    )
    if settings_error or settings is None:
        raise ValueError("rollback_settings_invalid")
    hook = _validate_executable(_installation_executable(_HOOK_NAME))
    command = _hook_command(hook, adapter_path)
    argv = [str(hook), "--harness", "claude", "--config", str(adapter_path)]
    if (
        adapter_raw is None
        or _sha256(adapter_raw) != state["adapter"]["sha256"]
        or _sha256(command.encode()) != state["hook"]["command_sha256"]
        or _sha256(json.dumps(argv, separators=(",", ":")).encode())
        != state["hook"]["argv_sha256"]
    ):
        raise ValueError("rollback_live_mismatch")
    event = (
        settings.get("hooks", {}).get("UserPromptSubmit")
        if isinstance(settings.get("hooks"), dict)
        else None
    )
    matches = [
        (group, item)
        for group in event or []
        if isinstance(group, dict)
        for item in group.get("hooks", [])
        if isinstance(item, dict)
        and set(item) == {"type", "command", "timeout"}
        and item.get("type") == "command"
        and item.get("command") == command
        and item.get("timeout") == 30
    ]
    if len(matches) != 1:
        raise ValueError("rollback_hook_ambiguous_or_missing")
    new_settings = deepcopy(settings)
    new_event = new_settings["hooks"]["UserPromptSubmit"]
    for group in new_event:
        if isinstance(group, dict) and isinstance(group.get("hooks"), list):
            group["hooks"] = [
                item
                for item in group["hooks"]
                if not (
                    isinstance(item, dict)
                    and item.get("type") == "command"
                    and item.get("command") == command
                )
            ]
    new_settings["hooks"]["UserPromptSubmit"] = [
        group for group in new_event if isinstance(group, dict) and group.get("hooks")
    ]
    new_settings_raw = _json_bytes(new_settings)
    preimage_hash = state["adapter"]["preimage_sha256"]
    restore_raw = None
    backup = state["adapter"]["backup"]
    if backup is not None:
        backup_path = root / backup
        restore_raw = _read_existing(
            backup_path, missing=None, reason="rollback_backup_invalid"
        )
        if restore_raw is None or _sha256(restore_raw) != preimage_hash:
            raise ValueError("rollback_backup_invalid")
    elif preimage_hash is not None:
        raise ValueError("rollback_state_invalid")
    receipt = {
        "schema": _ROLLBACK_SCHEMA,
        "mode": "apply" if apply else "dry-run",
        "status": "selected",
        "reason": "exact_state_binding",
        "changed": True,
        "adapter": {
            "live_sha256": _sha256(adapter_raw),
            "restored_sha256": preimage_hash,
        },
        "settings": {
            "live_sha256": _sha256(settings_raw or b""),
            "state_sha256": _sha256(state_raw or b""),
        },
        "operations": [
            "restore_adapter" if restore_raw is not None else "remove_adapter",
            "remove_exact_universal_docs_hook",
            "remove_install_state",
            "write_tombstone_last",
        ],
        "hook": {"command_sha256": state["hook"]["command_sha256"]},
        "body_omitted": True,
    }
    if not apply:
        _assert_receipt_fits(receipt, _ROLLBACK_SCHEMA)
        return receipt, 0
    _assert_receipt_fits(receipt, _ROLLBACK_SCHEMA)
    old_adapter_stat, old_settings_stat, old_state_stat = (
        adapter_path.stat(),
        settings_path.stat(),
        state_path.stat(),
    )
    tombstone_path = root / _TOMBSTONE_RELATIVE_PATH
    marker_path = root / _RECOVERY_MARKER_RELATIVE_PATH
    _validate_output_parent(root, tombstone_path)
    _validate_output_parent(root, marker_path)
    tombstone_raw = _json_bytes(
        {
            "schema": "universal-docs.rollback-tombstone/v1",
            "adapter_sha256": _sha256(restore_raw)
            if restore_raw is not None
            else _sha256(adapter_raw),
            "preimage_sha256": preimage_hash,
        }
    )
    # The marker is write-ahead: it is the first mutation and therefore exists
    # before the backup, settings, adapter, state, or tombstone can change.
    old_tombstone_value, old_tombstone = _read_tombstone(root)
    del old_tombstone_value
    current_settings_backup = _backup_path(root, "settings", settings_raw)
    _validate_output_parent(root, current_settings_backup)
    assert state_raw is not None and settings_raw is not None
    _validate_backup(current_settings_backup, settings_raw, kind="settings")
    created_backup = not current_settings_backup.exists()
    marker_raw = _recovery_marker_bytes(state_raw, state, settings_raw, settings_raw)
    try:
        _atomic_write(marker_path, marker_raw)
    except (OSError, RuntimeError, ValueError):
        raise ValueError("rollback_failed") from None

    try:
        _write_backup(current_settings_backup, settings_raw, kind="settings")
        _atomic_write(settings_path, new_settings_raw)
        _restore_file(adapter_path, restore_raw, None, None)
        state_path.unlink()
        _atomic_write(tombstone_path, tombstone_raw)
    except (OSError, RuntimeError, ValueError):
        recovery_failed = False

        def recover(operation) -> None:
            nonlocal recovery_failed
            try:
                operation()
            except (OSError, RuntimeError, ValueError):
                recovery_failed = True

        # Attempt every compensation while the write-ahead marker remains.
        recover(
            lambda: _restore_file(
                settings_path,
                settings_raw,
                stat.S_IMODE(old_settings_stat.st_mode),
                old_settings_stat.st_mtime_ns,
            )
        )
        recover(
            lambda: _restore_file(
                adapter_path,
                adapter_raw,
                stat.S_IMODE(old_adapter_stat.st_mode),
                old_adapter_stat.st_mtime_ns,
            )
        )
        recover(
            lambda: _restore_file(
                state_path,
                state_raw,
                stat.S_IMODE(old_state_stat.st_mode),
                old_state_stat.st_mtime_ns,
            )
        )
        recover(
            lambda: _restore_file(
                tombstone_path,
                old_tombstone,
                0o600 if old_tombstone is not None else None,
                None,
            )
        )
        if created_backup:
            recover(lambda: current_settings_backup.unlink(missing_ok=True))
        if not recovery_failed:
            try:
                marker_path.unlink(missing_ok=True)
            except (OSError, RuntimeError, ValueError):
                recovery_failed = True
        raise ValueError("recovery_failed" if recovery_failed else "rollback_failed") from None

    # Cleanup is the final transaction step. Failure is not success: the marker
    # remains and all public entry points continue to refuse the project.
    try:
        marker_path.unlink(missing_ok=True)
    except (OSError, RuntimeError, ValueError):
        raise ValueError("recovery_failed") from None
    return receipt, 0


def _receipt_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii") + b"\n"


def _bounded_doctor_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep check identity load-bearing while dropping diagnostic bulk."""
    if len(_receipt_bytes(payload)) <= MAX_OUTPUT_BYTES:
        return payload
    compact = deepcopy(payload)
    compact["checks"] = [
        {"id": item.get("id"), "status": item.get("status"),
         "reason": item.get("reason"), "metadata": {}}
        for item in payload.get("checks", [])
        if isinstance(item, dict)
    ]
    compact["cache"] = {}
    compact["source_probe"] = {}
    compact["rollback"] = {"instructions": "rollback receipt metadata omitted"}
    if len(_receipt_bytes(compact)) <= MAX_OUTPUT_BYTES:
        return compact
    # Check identity and aggregate status are the non-optional contract.
    return {
        "schema": _DOCTOR_SCHEMA,
        "status": payload.get("status", "fail"),
        "checks": compact["checks"],
        "cache": {},
        "source_probe": {},
        "rollback": {},
    }


def _assert_receipt_fits(payload: dict[str, Any], schema: str) -> None:
    if len(_receipt_bytes(payload)) > MAX_OUTPUT_BYTES:
        raise ValueError("response_too_large")


def _emit(
    payload: dict[str, Any], output: BinaryIO, *, schema: str = _INIT_SCHEMA
) -> None:
    encoded = _receipt_bytes(payload)
    if len(encoded) > MAX_OUTPUT_BYTES:
        encoded = _receipt_bytes(
            {"schema": schema, "status": "fail", "reason": "response_too_large"}
        )
    try:
        written = output.write(encoded)
    except TypeError:
        # String streams are a supported test/embedding seam.
        written = output.write(encoded.decode("ascii"))
        expected = len(encoded.decode("ascii"))
    else:
        expected = len(encoded)
    if written is not None and written != expected:
        raise OSError("output_write_failed")
    output.flush()


def main(argv: list[str] | None = None, *, stdout: BinaryIO | None = None) -> int:
    """Emit exactly one bounded JSON object and no diagnostics."""
    output = stdout or sys.stdout.buffer
    args: argparse.Namespace | None = None
    raw_argv = argv if argv is not None else sys.argv[1:]
    emitting = False
    try:
        try:
            argv_bytes = sum(len(os.fsencode(item)) + 1 for item in raw_argv)
        except (TypeError, UnicodeError, ValueError):
            raise ValueError("arguments_too_large") from None
        if argv_bytes > 128 * 1024 or len(raw_argv) > 512:
            raise ValueError("arguments_too_large")
        _guard_duplicate_options(raw_argv)
        args = _parser().parse_args(argv)
        if args.command == "plan":
            root = _validate_root(args.project_root)
            before = _safe_relative(args.before, root)
            after = _safe_relative(args.after, root)
            payload = plan_dependency_changes(
                before, after, project_root=root, package=args.package
            ).as_dict()
        elif args.command == "init":
            root = _validate_root(args.project_root, absolute_required=True)
            payload, code = _init_receipt(args, root)
            emitting = True
            _emit(payload, output)
            return code
        elif args.command == "doctor":
            root = _validate_root(args.project_root, absolute_required=True)
            payload, code = _doctor_receipt(root)
            emitting = True
            _emit(payload, output, schema=_DOCTOR_SCHEMA)
            return code
        elif args.command == "rollback":
            root = _validate_root(args.project_root, absolute_required=True)
            payload, code = _rollback_receipt(root, args.apply)
            emitting = True
            _emit(payload, output, schema=_ROLLBACK_SCHEMA)
            return code
        else:
            raise ValueError("command_required")
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        if emitting:
            raise
        command = (
            args.command if args is not None else (raw_argv[0] if raw_argv else None)
        )
        if command == "doctor":
            _emit(
                {
                    "schema": _DOCTOR_SCHEMA,
                    "status": "fail",
                    "reason": _known_reason(exc, _INIT_ERROR_REASONS | {"project_root_invalid", "command_required"}, "doctor_invalid"),
                },
                output,
                schema=_DOCTOR_SCHEMA,
            )
            return 1
        if command == "rollback":
            _emit(
                {
                    "schema": _ROLLBACK_SCHEMA,
                    "status": "fail",
                    "reason": _known_reason(exc, _ROLLBACK_ERROR_REASONS | {"command_required"}, "rollback_invalid"),
                },
                output,
                schema=_ROLLBACK_SCHEMA,
            )
            return 1
        if command == "init":
            reason = str(exc)
            if reason not in _INIT_ERROR_REASONS:
                reason = "write_failed" if getattr(args, "apply", False) else "init_invalid"
            mode = "apply" if getattr(args, "apply", False) else "dry-run"
            _emit(
                {
                    "schema": _INIT_SCHEMA,
                    "mode": mode,
                    "status": "abstained",
                    "reason": reason,
                },
                output,
            )
            return 1
        payload = _error("invalid_plan_request")
    _emit(payload, output)
    return 0 if payload.get("status") == "selected" else 1


if __name__ == "__main__":
    raise SystemExit(main())
