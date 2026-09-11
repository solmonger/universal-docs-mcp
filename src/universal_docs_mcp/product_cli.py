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
from .command_hook import MAX_HOOK_OUTPUT_BYTES, load_config
from .planner import plan_dependency_changes, to_preflight_request

MAX_OUTPUT_BYTES = 16 * 1024
_DOCTOR_SCHEMA = "universal-docs.doctor/v1"
_INIT_SCHEMA = "universal-docs.init/v1"
_ROLLBACK_SCHEMA = "universal-docs.rollback/v1"
_DOCTOR_MAX_FILE_BYTES = 64 * 1024


def _doctor_check(
    check_id: str, status: str, reason: str, **metadata: Any
) -> dict[str, Any]:
    return {"id": check_id, "status": status, "reason": reason, "metadata": metadata}


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
    """Prove the ordinary install is the one used by this interpreter.

    Fixture executable overrides are deliberately never classified as an installed
    identity; they are test seams, not ownership evidence.
    """
    override = any(
        os.environ.get("UNIVERSAL_DOCS_INIT_" + name.replace("-", "_").upper())
        for name in (_HOOK_NAME, _PREFLIGHT_NAME)
    )
    executable = Path(sys.executable).resolve()
    result = {
        "status": "fail",
        "reason": "fixture_override" if override else "installed_identity_unavailable",
        "module_path": None,
        "installed_version": None,
        "executable": str(executable),
        "owned": False,
    }
    if override:
        return result
    try:
        expected_scripts = [executable.parent / name for name in (_HOOK_NAME, _PREFLIGHT_NAME)]
        for script in expected_scripts:
            _validate_executable(script)
        module = importlib.import_module("universal_docs_mcp")
        raw_module_path = getattr(module, "__file__", None)
        if not isinstance(raw_module_path, str):
            return result
        module_path = Path(raw_module_path).resolve()
        if not module_path.is_file() or module_path.is_symlink():
            return result
        distribution = importlib.metadata.distribution("universal-docs-mcp")
        version = distribution.version
        files = distribution.files
        if not files:
            return result
        owned_files = {
            Path(str(distribution.locate_file(item))).resolve() for item in files
        }
        owned = module_path in owned_files
        result.update(
            module_path=str(module_path), installed_version=version, owned=owned
        )
        ok = owned and version == getattr(module, "__version__", None) == __version__
        result["status"] = "pass" if ok else "fail"
        result["reason"] = "identity_match" if ok else "identity_mismatch"
        return result
    except (ImportError, importlib.metadata.PackageNotFoundError, OSError, TypeError, ValueError):
        return result


def _doctor_probe(
    hook: Path, adapter: Path, request: dict[str, Any] | None
) -> tuple[dict[str, Any], str]:
    """Run the real wrapper with bounded pipes and kill its process group."""
    try:
        config = load_config(adapter)
        command = [str(hook), "--harness", "claude", "--config", str(adapter)]
        timeout = min(max(config.timeout_ms / 1000.0, 0.1), 5.0)
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            close_fds=True,
            env={"PATH": "/usr/bin:/bin", "PYTHONIOENCODING": "utf-8"},
        )
        stdout, stderr = bytearray(), bytearray()
        selector = selectors.DefaultSelector()
        for stream, kind in (
            (proc.stdin, "in"),
            (proc.stdout, "out"),
            (proc.stderr, "err"),
        ):
            os.set_blocking(stream.fileno(), False)
            selector.register(
                stream.fileno(),
                selectors.EVENT_WRITE if kind == "in" else selectors.EVENT_READ,
                kind,
            )
        event, sent, status = b"{}\n", 0, "completed"
        deadline = __import__("time").monotonic() + timeout
        try:
            while selector.get_map():
                remaining = deadline - __import__("time").monotonic()
                if remaining <= 0:
                    status = "probe_timeout"
                    break
                for key, _ in selector.select(min(0.05, remaining)):
                    if key.data == "in":
                        try:
                            sent += os.write(key.fd, event[sent:])
                        except OSError:
                            sent = len(event)
                        if sent == len(event):
                            selector.unregister(key.fd)
                            proc.stdin.close()
                        continue
                    try:
                        chunk = os.read(key.fd, 8192)
                    except (BlockingIOError, InterruptedError):
                        continue
                    if not chunk:
                        selector.unregister(key.fd)
                        continue
                    target, limit = (
                        (stdout, MAX_HOOK_OUTPUT_BYTES)
                        if key.data == "out"
                        else (stderr, 16 * 1024)
                    )
                    target.extend(chunk[: max(0, limit + 1 - len(target))])
                    if len(target) > limit:
                        status = "probe_output_too_large"
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
        stdout = bytes(stdout)
        if proc.returncode != 0:
            return {"returncode": proc.returncode}, "preflight_nonzero"
        if len(stdout) > MAX_HOOK_OUTPUT_BYTES:
            return {"stdout_bytes": len(stdout)}, "probe_output_too_large"
        payload = json.loads(
            stdout.decode("utf-8"),
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_json_constant,
        )
        context = (
            payload.get("hookSpecificOutput", {}).get("additionalContext")
            if isinstance(payload, dict)
            else None
        )
        if (
            not isinstance(context, str)
            or context.count("--- BEGIN UNTRUSTED DOCUMENTATION DATA ---") != 1
            or context.count("--- END UNTRUSTED DOCUMENTATION DATA ---") != 1
        ):
            return {"returncode": proc.returncode}, "source_less_or_malformed"
        fields = {}
        allowed = {
            "Target package",
            "Target version",
            "Source kind",
            "Source version binding",
            "Source SHA-256",
            "Freshness policy",
            "Freshness state",
        }
        for line in context.splitlines():
            if ": " in line:
                key, value = line.split(": ", 1)
                if key in allowed:
                    if key in fields:
                        return {}, "source_metadata_mismatch"
                    fields[key] = value
        if set(fields) != allowed:
            return {}, "source_metadata_mismatch"
        expected_package = (
            json.dumps(request.get("package"))
            if request and request.get("package")
            else None
        )
        expected_version = (
            json.dumps(request.get("requested_version") or request.get("version"))
            if request
            else None
        )
        if (
            expected_package
            and fields.get("Target package") != expected_package
            or expected_version
            and fields.get("Target version") != expected_version
        ):
            return {}, "source_metadata_mismatch"
        source_sha = fields.get("Source SHA-256", "")
        if len(source_sha) != 64 or any(
            c not in "0123456789abcdef" for c in source_sha
        ):
            return {}, "source_metadata_mismatch"
        freshness_policy = fields.get("Freshness policy", "")
        freshness_state = fields.get("Freshness state", "")
        try:
            freshness_policy = json.loads(freshness_policy)
            freshness_state = json.loads(freshness_state)
        except (TypeError, json.JSONDecodeError):
            pass
        if (
            freshness_policy != (request or {}).get("freshness_mode")
            or freshness_state != "upstream_checked"
        ):
            return {}, "source_metadata_mismatch"
        mode = (
            "fixture"
            if any(
                os.environ.get("UNIVERSAL_DOCS_INIT_" + n.replace("-", "_").upper())
                for n in (_HOOK_NAME, _PREFLIGHT_NAME)
            )
            else "live_registry"
        )

        def packet_value(key: str) -> str | None:
            value = fields.get(key)
            if value is None:
                return None
            try:
                decoded = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                return value
            return decoded if isinstance(decoded, str) else value

        return {
            "returncode": 0,
            "probe_mode": mode,
            "package": request.get("package") if request else None,
            "version": request.get("requested_version") if request else None,
            "source_kind": packet_value("Source kind"),
            "source_version_binding": packet_value("Source version binding"),
            "source_sha256": source_sha,
            "freshness_policy": freshness_policy,
            "freshness_state": freshness_state,
            "packet_sha256": hashlib.sha256(context.encode()).hexdigest(),
            "packet_bytes": len(stdout),
            "source_body_omitted": True,
        }, "source_bearing"
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        RecursionError,
    ):
        return {}, "probe_malformed"


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
    "init_invalid",
    "active_install_conflict",
    "install_state_invalid",
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
    for relative in (Path(".universal-docs/adapter.json"), Path(".universal-docs/install-state.json"), Path(".universal-docs/rollback-tombstone.json"), Path(".claude/settings.json")):
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


def _doctor_cache(root: Path) -> tuple[dict[str, Any], str]:
    configured = os.environ.get("UNIVERSAL_DOCS_CACHE_DIR")
    if configured is not None:
        cache = Path(configured).expanduser()
        scope = "explicit"
        if not cache.is_absolute():
            return {"scope": scope, "identity": None, "available": False}, "fail"
    else:
        cache = DEFAULT_CACHE_DIR
        scope = "default_user_installation"
    try:
        current = Path(cache.anchor)
        for component in cache.parts[1:]:
            current /= component
            if not current.exists():
                break
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                return {"scope": scope, "identity": None, "available": False}, "fail"
        if cache.is_symlink() or (cache.exists() and not cache.is_dir()):
            return {"scope": scope, "identity": None, "available": False}, "fail"
        resolved = cache.resolve(strict=False)
        identity = _sha256(str(resolved).encode())
        return {
            "scope": scope,
            "identity": identity,
            "available": cache.is_dir(),
        }, "pass" if cache.is_dir() else "unknown"
    except (OSError, RuntimeError):
        return {"scope": "unknown", "identity": None, "available": False}, "unknown"


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
        state, state_raw, state_status, state_reason = None, None, "fail", str(exc)
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
            if tombstone["preimage_sha256"] is not None and (
                current is None or _sha256(current) != tombstone["adapter_sha256"]
            ):
                state_status, state_reason = "fail", "rollback_tombstone_invalid"
    except ValueError as exc:
        state_status, state_reason = "fail", str(exc)
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
            hook=str(hook),
            preflight=str(preflight),
            provenance="fixture" if override_used else "installed",
        )
    )
    cache, cache_status = _doctor_cache(root)
    checks.append(
        _doctor_check(
            "cache_scope",
            cache_status,
            "cache_owner_classified",
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
    if probe_reason == "source_bearing" and cache_status == "unknown":
        cache, cache_status = _doctor_cache(root)
        checks[5] = _doctor_check(
            "cache_scope",
            cache_status,
            "cache_owner_classified",
            scope=cache["scope"],
            identity=cache["identity"],
            available=cache["available"],
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
    command = ["universal-docs", "rollback", "--project-root", str(root), "--apply"]
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
    return receipt, 0 if status == "pass" else 1


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
        except OSError:
            pass
        raise ValueError("write_failed") from None
    return receipt, 0


def _rollback_receipt(root: Path, apply: bool) -> tuple[dict[str, Any], int]:
    _validate_project_topology(root)
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
        "hook": {"command_sha256": state["hook"]["command_sha256"]},
        "body_omitted": True,
    }
    if not apply:
        return receipt, 0
    old_adapter_stat, old_settings_stat, old_state_stat = (
        adapter_path.stat(),
        settings_path.stat(),
        state_path.stat(),
    )
    tombstone_path = root / _TOMBSTONE_RELATIVE_PATH
    _validate_output_parent(root, tombstone_path)
    tombstone_raw = _json_bytes(
        {
            "schema": "universal-docs.rollback-tombstone/v1",
            "adapter_sha256": _sha256(restore_raw)
            if restore_raw is not None
            else _sha256(adapter_raw),
            "preimage_sha256": preimage_hash,
        }
    )
    old_tombstone = _read_existing(
        tombstone_path, missing=None, reason="rollback_tombstone_invalid"
    )
    current_settings_backup = _backup_path(root, "settings", settings_raw)
    _validate_output_parent(root, current_settings_backup)
    created_backup = not current_settings_backup.exists()
    try:
        _write_backup(current_settings_backup, settings_raw, kind="settings")
        _atomic_write(settings_path, new_settings_raw)
        _restore_file(adapter_path, restore_raw, None, None)
        state_path.unlink()
        _atomic_write(tombstone_path, tombstone_raw)
    except (OSError, RuntimeError, ValueError):
        try:
            _restore_file(
                settings_path,
                settings_raw,
                stat.S_IMODE(old_settings_stat.st_mode),
                old_settings_stat.st_mtime_ns,
            )
            _restore_file(
                adapter_path,
                adapter_raw,
                stat.S_IMODE(old_adapter_stat.st_mode),
                old_adapter_stat.st_mtime_ns,
            )
            _restore_file(
                state_path,
                state_raw,
                stat.S_IMODE(old_state_stat.st_mode),
                old_state_stat.st_mtime_ns,
            )
            _restore_file(
                tombstone_path,
                old_tombstone,
                0o600 if old_tombstone is not None else None,
                None,
            )
            if created_backup:
                current_settings_backup.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValueError("rollback_failed") from None
    return receipt, 0


def _emit(
    payload: dict[str, Any], output: BinaryIO, *, schema: str = _INIT_SCHEMA
) -> None:
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()
    if len(encoded) + 1 > MAX_OUTPUT_BYTES:
        encoded = json.dumps(
            {"schema": schema, "status": "fail", "reason": "response_too_large"},
            separators=(",", ":"),
        ).encode()
    output.write(encoded + b"\n")
    output.flush()


def main(argv: list[str] | None = None, *, stdout: BinaryIO | None = None) -> int:
    """Emit exactly one bounded JSON object and no diagnostics."""
    output = stdout or sys.stdout.buffer
    args: argparse.Namespace | None = None
    raw_argv = argv if argv is not None else sys.argv[1:]
    try:
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
            _emit(payload, output)
            return code
        elif args.command == "doctor":
            root = _validate_root(args.project_root, absolute_required=True)
            payload, code = _doctor_receipt(root)
            _emit(payload, output, schema=_DOCTOR_SCHEMA)
            return code
        elif args.command == "rollback":
            root = _validate_root(args.project_root, absolute_required=True)
            payload, code = _rollback_receipt(root, args.apply)
            _emit(payload, output, schema=_ROLLBACK_SCHEMA)
            return code
        else:
            raise ValueError("command_required")
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        command = (
            args.command if args is not None else (raw_argv[0] if raw_argv else None)
        )
        if command == "doctor":
            _emit(
                {
                    "schema": _DOCTOR_SCHEMA,
                    "status": "fail",
                    "reason": str(exc) or "doctor_invalid",
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
                    "reason": str(exc) or "rollback_invalid",
                },
                output,
                schema=_ROLLBACK_SCHEMA,
            )
            return 1
        if command == "init":
            reason = str(exc)
            if reason not in _INIT_ERROR_REASONS:
                reason = "write_failed" if args.apply else "init_invalid"
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
