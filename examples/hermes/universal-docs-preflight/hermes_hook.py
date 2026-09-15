"""Thin Hermes boundary for the shared, single-process context delivery CLI.

Only trusted plugin settings select executable/request file. Receipt validation,
source selection, and packet formatting belong to universal-docs-context, not a
second implementation in this plugin. Hermes receives current-user context only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import signal
import sqlite3
import stat
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from hermes_constants import get_hermes_home

SCHEMA = "universal-docs.context/v1"
MAX_CHILD_OUTPUT_BYTES = 16 * 1024 + 1
MAX_CONTEXT_BYTES = 8 * 1024
MAX_RETURN_BYTES = 16 * 1024


def _missing(error: str) -> dict[str, str]:
    return {
        "context": f"DOCS_PREFLIGHT_STATUS=missing\nDOCS_PREFLIGHT_ERROR={error}\n"
        "No documentation was verified or injected. This hook does not veto the Hermes turn."
    }


def _regular_path(value: Any, *, executable: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value.encode()) > 4096:
        raise ValueError("invalid_configuration")
    path = Path(value)
    if not path.is_absolute() or not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("invalid_configuration")
    if executable and not os.access(path, os.X_OK):
        raise ValueError("invalid_configuration")
    return value


def _settings(ctx: Any) -> dict[str, Any]:
    if os.name != "posix":
        raise ValueError("unsupported_platform")
    timeout = ctx.get_config("timeout_ms", 3000)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not 100 <= timeout <= 10_000
    ):
        raise ValueError("invalid_configuration")
    mode = ctx.get_config("mode", "static")
    if mode not in {"static", "native"}:
        raise ValueError("invalid_configuration")
    disabled_raw = ctx.get_config("disabled_profiles", "")
    if not isinstance(disabled_raw, str) or len(disabled_raw) > 4096:
        raise ValueError("invalid_configuration")
    disabled_profiles = {
        name.strip() for name in disabled_raw.split(",") if name.strip()
    }
    profile_home = get_hermes_home().resolve()
    profile_name = (
        profile_home.name if profile_home.parent.name == "profiles" else "default"
    )
    if profile_name in disabled_profiles:
        raise ValueError("profile_disabled")
    cache_raw = ctx.get_config(
        "cache_dir", str(profile_home / "cache" / "universal-docs-preflight")
    )
    if not isinstance(cache_raw, str) or not Path(cache_raw).is_absolute():
        raise ValueError("invalid_configuration")
    cache_dir = Path(cache_raw).resolve(strict=False)
    if cache_dir.exists() and (cache_dir.is_symlink() or not cache_dir.is_dir()):
        raise ValueError("invalid_configuration")
    settings = {
        "mode": mode,
        "executable": _regular_path(ctx.get_config("executable"), executable=True),
        "timeout_ms": timeout,
        "profile_name": profile_name,
        "cache_dir": cache_dir,
    }
    if mode == "static":
        settings["request_file"] = _regular_path(ctx.get_config("request_file"))
    return settings


def _environment(settings: dict[str, Any] | None = None) -> dict[str, str]:
    home = os.environ.get("HOME", str(Path.home()))
    cache_dir = (
        settings["cache_dir"]
        if settings is not None
        else get_hermes_home() / "cache" / "universal-docs-preflight"
    )
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": home,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "UNIVERSAL_DOCS_CACHE_DIR": str(cache_dir),
    }


def _stop_group(process: subprocess.Popen) -> None:
    # Reap an already-exited leader, but still clean its owned descendants.
    process.poll()
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=0.15)
    except subprocess.TimeoutExpired:
        pass
    # Reap the leader before signalling remaining members; probing an unreaped
    # macOS process group with signal 0 can report EPERM during exit.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=0.25)
    except subprocess.TimeoutExpired as exc:
        raise OSError("cleanup_unverified") from exc


def _read_child(settings: dict[str, Any]) -> tuple[bytes, str | None]:
    deadline = time.monotonic() + settings["timeout_ms"] / 1000
    try:
        process = subprocess.Popen(
            [settings["executable"], "--request-file", settings["request_file"]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            close_fds=True,
            start_new_session=True,
            cwd=str(Path(settings["executable"]).parent),
            env=_environment(settings),
        )
    except (OSError, ValueError):
        return b"", "spawn_failed"
    selector = selectors.DefaultSelector()
    output = bytearray()
    try:
        assert process.stdout is not None
        fd = process.stdout.fileno()
        os.set_blocking(fd, False)
        selector.register(fd, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return b"", "timeout"
            for key, _ in selector.select(min(remaining, 0.05)):
                try:
                    chunk = os.read(
                        key.fd, min(8192, MAX_CHILD_OUTPUT_BYTES + 1 - len(output))
                    )
                except (BlockingIOError, InterruptedError):
                    continue
                if not chunk:
                    selector.unregister(key.fd)
                    break
                output.extend(chunk)
                if len(output) > MAX_CHILD_OUTPUT_BYTES:
                    return b"", "output_limit"
        try:
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return b"", "timeout"
        if process.returncode != 0:
            return b"", "delivery_nonzero"
        return bytes(output), None
    except (OSError, ValueError):
        return b"", "child_io_failed"
    finally:
        selector.close()
        try:
            # Signal before closing the reader: closing first gives a blocked
            # writer SIGPIPE and races group signalling against an unreaped exit.
            _stop_group(process)
        finally:
            if process.stdout is not None:
                process.stdout.close()


def _unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non_finite")


def _frame_context(raw: bytes) -> dict[str, str]:
    try:
        frame = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
        if not isinstance(frame, dict) or set(frame) != {
            "schema",
            "status",
            "context",
            "error",
        }:
            return _missing("invalid_delivery_frame")
        if frame["schema"] != SCHEMA:
            return _missing("invalid_delivery_frame")
        if frame["status"] == "unavailable":
            return _missing("delivery_unavailable")
        if (
            frame["status"] not in {"prepared", "prepared_stale"}
            or frame["error"] is not None
        ):
            return _missing("invalid_delivery_frame")
        context = frame["context"]
        if (
            not isinstance(context, str)
            or not context.strip()
            or len(context.encode("utf-8")) > MAX_CONTEXT_BYTES
        ):
            return _missing("invalid_delivery_frame")
        return {"context": f"DOCS_PREFLIGHT_STATUS={frame['status']}\n{context}"}
    except (ValueError, TypeError, RecursionError):
        return _missing("invalid_delivery_frame")


def _hermes_home() -> Path:
    """Active Hermes home, honoring context-local profile overrides.

    Under multiplexed serving the process environment alone is not enough:
    Hermes binds the current turn's profile scope through ``get_hermes_home()``
    (the same precedence the cache scope already uses).
    """
    return get_hermes_home().expanduser()


def _git_root(cwd: Path) -> Path:
    """Walk up from a trusted working directory to the nearest ``.git`` root."""
    for candidate in (cwd, *cwd.parents):
        marker = candidate / ".git"
        if marker.exists() and not marker.is_symlink():
            return candidate
    return cwd


def _session_state(session_id: str) -> tuple[Path, Path, str] | None:
    """Resolve Hermes-owned session state plus the evidence source used.

    Order: a validated session database row (its stored git root when that
    root contains the session cwd, otherwise the root derived from the row's
    cwd), then Hermes's absolute ``TERMINAL_CWD`` binding, then the host
    process cwd. Returns ``(cwd, root, source)``.
    """
    if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
        return None
    home = _hermes_home()
    dbs = [home / "state.db", home / "hermes.db", home / "data" / "state.db"]
    for db in dbs:
        try:
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.2) as con:
                row = con.execute(
                    "SELECT cwd, git_repo_root FROM sessions WHERE id=?", (session_id,)
                ).fetchone()
            if not row or not isinstance(row[0], str) or not row[0]:
                continue
            cwd_raw = Path(row[0])
            if cwd_raw.is_symlink():
                continue
            cwd = cwd_raw.resolve(strict=True)
            if not cwd.is_dir():
                continue
            if isinstance(row[1], str) and row[1]:
                root_raw = Path(row[1])
                if not root_raw.is_symlink():
                    candidate = root_raw.resolve(strict=True)
                    if candidate.is_dir() and (
                        cwd == candidate or candidate in cwd.parents
                    ):
                        return cwd, candidate, "session_row_git_root"
            # Hermes persists git metadata best-effort; a row with only a cwd
            # is still trusted evidence for that working directory.
            return cwd, _git_root(cwd), "session_row_cwd"
        except (OSError, sqlite3.Error, ValueError):
            continue
    try:
        terminal = os.environ.get("TERMINAL_CWD")
        cwd_raw = Path(terminal) if terminal else Path.cwd()
        if not cwd_raw.is_absolute() or cwd_raw.is_symlink():
            return None
        cwd = cwd_raw.resolve(strict=True)
        if not cwd.is_dir():
            return None
        return cwd, _git_root(cwd), "terminal_cwd" if terminal else "process_cwd"
    except (OSError, ValueError):
        return None


def _atomic_receipt(directory: Path, receipt: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{receipt['session_id']}-{uuid.uuid4().hex}.json"
    payload = (
        json.dumps(receipt, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        + "\n"
    ).encode()
    fd = os.open(
        directory / ("." + name),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(directory / ("." + name), directory / name)
    finally:
        try:
            (directory / ("." + name)).unlink()
        except FileNotFoundError:
            pass


def _request_file(directory: Path, request: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f".request-{uuid.uuid4().hex}.json"
    payload = (
        json.dumps(request, allow_nan=False, separators=(",", ":")) + "\n"
    ).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return path


def _cache_evidence(cache_dir: Path, plan: Any, context: str) -> dict[str, Any] | None:
    """Bind delivered context to the exact structured cache row used by preflight."""
    db_path = cache_dir / "cache.db"
    try:
        st = db_path.lstat()
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            return None
        key = f"docrequest-v4:{plan.ecosystem}:{plan.package}:{plan.target_version}"
        uri = f"file:{quote(str(db_path))}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=0.2) as con:
            con.execute("PRAGMA query_only=ON")
            row = con.execute(
                "SELECT value, fetched_at, value_bytes FROM docs_cache WHERE key=?",
                (key,),
            ).fetchone()
        if not row:
            return None
        value, fetched_at, value_bytes = row
        parsed = json.loads(value)
        source_url = parsed.get("source_url")
        if (
            parsed.get("version") != plan.target_version
            or not isinstance(source_url, str)
            or not source_url.startswith("https://")
            or source_url not in context
            or not isinstance(fetched_at, (int, float))
            or not isinstance(value_bytes, int)
        ):
            return None
        return {
            "cache_key": key,
            "cache_fetched_at": fetched_at,
            "cache_value_bytes": value_bytes,
            "cache_value_sha256": hashlib.sha256(value.encode()).hexdigest(),
            "source": parsed.get("source"),
            "source_url": source_url,
        }
    except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        return None


def _native_callback(
    settings: dict[str, Any], payload: dict[str, Any]
) -> dict[str, str] | None:
    from universal_docs_mcp.planner import select_current_package, to_preflight_request

    session = payload.get("session_id")
    if not isinstance(session, str) or not session:
        return _missing("missing_session_state")
    task = (
        payload.get("user_message")
        if isinstance(payload.get("user_message"), str)
        else None
    )
    # Dependency presence alone is not an eligible technical turn.
    if not task or not re.search(
        r"\b(code|coding|debug|implement|package|dependency|api|sdk|migration|library|import|upgrade|install|technical)\b",
        task,
        re.I,
    ):
        return None
    receipt_dir = _hermes_home() / "receipts" / "universal-docs"
    receipt: dict[str, Any] = {
        "schema": "universal-docs.hermes-consumption/v1",
        "status": "abstained",
        "reason": "session_state_unavailable",
        "session_id": session,
        "package": None,
        "target_version": None,
        "resolution": {"cwd": None, "root": None, "source": None},
    }
    state = _session_state(session)
    if state is None:
        _atomic_receipt(receipt_dir, receipt)
        return _missing("session_state_unavailable")
    cwd, root, resolution = state
    receipt["resolution"] = {"cwd": str(cwd), "root": str(root), "source": resolution}
    plan = select_current_package(root, task=task)
    receipt.update(
        {
            "status": plan.status,
            "reason": plan.reason,
            "package": plan.package,
            "target_version": plan.target_version,
        }
    )
    if plan.status != "selected":
        _atomic_receipt(receipt_dir, receipt)
        return _missing(plan.reason)
    request_path: Path | None = None
    try:
        request = to_preflight_request(plan).model_dump(mode="json", exclude_none=True)
        request["deadline_ms"] = max(1000, min(settings["timeout_ms"] - 100, 9000))
        request_path = _request_file(receipt_dir / ".requests", request)
        child_settings = dict(settings, request_file=str(request_path))
        raw, error = _read_child(child_settings)
        framed = _missing(error) if error else _frame_context(raw)
        context = framed["context"]
        retrieved = (
            "DOCS_PREFLIGHT_STATUS=prepared\n" in context
            or "DOCS_PREFLIGHT_STATUS=prepared_stale\n" in context
        )
        cache_evidence = (
            _cache_evidence(settings["cache_dir"], plan, context) if retrieved else None
        )
        retrieved = retrieved and cache_evidence is not None
        receipt.update(
            {
                "status": "retrieved" if retrieved else "failed",
                "error": None
                if retrieved
                else (error or "cache_evidence_missing_or_mismatched"),
                "query": plan.query,
                "selection": plan.selection,
                "context_sha256": hashlib.sha256(context.encode()).hexdigest()
                if retrieved
                else None,
                "cache_evidence": cache_evidence,
            }
        )
        _atomic_receipt(receipt_dir, receipt)
        return framed if retrieved else _missing(receipt["error"])
    except Exception:
        receipt.update({"status": "failed", "error": "preflight_failed"})
        _atomic_receipt(receipt_dir, receipt)
        return _missing("preflight_failed")
    finally:
        if request_path is not None:
            try:
                request_path.unlink()
            except FileNotFoundError:
                pass


def register(ctx: Any) -> None:
    """Inject explicit prepared/missing state; Hermes pre_llm_call is fail-open."""
    try:
        settings = _settings(ctx)
    except (OSError, ValueError, TypeError):
        # Native mode is opt-in; malformed static settings must not activate it.
        return
    lock = threading.Lock()
    last_turn_id: str | None = None

    def callback(**payload: Any) -> dict[str, str] | None:
        nonlocal last_turn_id
        turn_id = payload.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            return _missing("missing_turn_identity")
        if not lock.acquire(blocking=False):
            return _missing("delivery_busy")
        try:
            if turn_id == last_turn_id:
                return None
            last_turn_id = turn_id
            if settings["mode"] == "native":
                return _native_callback(settings, payload)
            raw, error = _read_child(settings)
            return _missing(error) if error else _frame_context(raw)
        except OSError:
            return _missing("cleanup_unverified")
        finally:
            lock.release()

    ctx.register_hook("pre_llm_call", callback)
