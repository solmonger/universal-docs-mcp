"""Thin Hermes boundary for the shared, single-process context delivery CLI.

Only trusted plugin settings select executable/request file. Receipt validation,
source selection, and packet formatting belong to universal-docs-context, not a
second implementation in this plugin. Hermes receives current-user context only.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

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
    return {
        "executable": _regular_path(ctx.get_config("executable"), executable=True),
        "request_file": _regular_path(ctx.get_config("request_file")),
        "timeout_ms": timeout,
    }


def _environment() -> dict[str, str]:
    home = os.environ.get("HOME", str(Path.home()))
    profile = get_hermes_home()
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": home,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "UNIVERSAL_DOCS_CACHE_DIR": str(profile / "cache" / "universal-docs-preflight"),
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
            env=_environment(),
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


def register(ctx: Any) -> None:
    """Inject explicit prepared/missing state; Hermes pre_llm_call is fail-open."""
    try:
        settings = _settings(ctx)
    except (OSError, ValueError, TypeError):
        ctx.register_hook("pre_llm_call", lambda **_: _missing("invalid_configuration"))
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
            raw, error = _read_child(settings)
            return _missing(error) if error else _frame_context(raw)
        except OSError:
            return _missing("cleanup_unverified")
        finally:
            lock.release()

    ctx.register_hook("pre_llm_call", callback)
