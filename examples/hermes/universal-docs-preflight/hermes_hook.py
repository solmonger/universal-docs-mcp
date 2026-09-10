"""Hermes per-turn adapter for the universal-docs preflight executable.

This candidate deliberately keeps the host boundary narrow: trusted plugin settings select
one absolute executable and a fixed documentation target; inbound hook payloads are used only
by Hermes to place the returned context on the current user message.  The child process gets a
strict JSON request, a filtered environment, no shell, and bounded time/output.
"""

from __future__ import annotations

import json
import math
import os
import selectors
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

SCHEMA = "universal-docs.preflight/v1"
DEFAULT_CONTEXT_MAX_BYTES = 8_000
MAX_CONTEXT_BYTES = 12_000
DEFAULT_DEADLINE_MS = 3_000
MAX_DEADLINE_MS = 10_000
MAX_CHILD_OUTPUT_BYTES = 128 * 1024
MAX_RETURN_BYTES = 16 * 1024
MAX_RECEIPT_BYTES = 8 * 1024
MAX_REQUEST_BYTES = 8 * 1024
MAX_TEXT_FIELD_BYTES = 4 * 1024

_SAFE_ENV = {
    "PATH": "/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}


class _ConfigError(ValueError):
    """A trusted plugin setting is absent or outside the adapter's bounds."""


def _text_setting(value: Any, name: str, *, required: bool = False) -> str:
    if value is None:
        if required:
            raise _ConfigError(f"missing_{name}")
        return ""
    if not isinstance(value, str) or not value.strip():
        raise _ConfigError(f"invalid_{name}")
    value = value.strip()
    if len(value.encode("utf-8")) > MAX_TEXT_FIELD_BYTES:
        raise _ConfigError(f"oversize_{name}")
    return value


def _bounded_int(value: Any, name: str, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise _ConfigError(f"invalid_{name}")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise _ConfigError(f"invalid_{name}") from None
    return max(low, min(high, number))


def _settings_from_context(ctx: Any) -> dict[str, Any]:
    """Read only plugin-relative settings; never inspect the inbound hook payload."""
    get = ctx.get_config
    executable = _text_setting(get("executable"), "executable", required=True)
    if not os.path.isabs(executable) or not Path(executable).is_file():
        raise _ConfigError("invalid_executable")
    package = _text_setting(get("package"), "package", required=True)
    ecosystem = _text_setting(get("ecosystem", "python"), "ecosystem") or "python"
    selection = _text_setting(get("selection", "latest"), "selection") or "latest"
    if selection not in {"latest", "requested"}:
        raise _ConfigError("invalid_selection")
    requested_version = get("requested_version")
    if requested_version is not None:
        requested_version = _text_setting(requested_version, "requested_version")
    query = _text_setting(get("query", ""), "query")
    raw_sections = get("section_ids", [])
    if raw_sections is None:
        raw_sections = []
    if not isinstance(raw_sections, list) or len(raw_sections) > 32:
        raise _ConfigError("invalid_section_ids")
    section_ids = [_text_setting(item, "section_id") for item in raw_sections]
    return {
        "executable": executable,
        "package": package,
        "ecosystem": ecosystem,
        "selection": selection,
        "requested_version": requested_version,
        "query": query,
        "section_ids": section_ids,
        "context_max_bytes": _bounded_int(
            get("context_max_bytes"),
            "context_max_bytes",
            DEFAULT_CONTEXT_MAX_BYTES,
            256,
            MAX_CONTEXT_BYTES,
        ),
        "freshness_mode": _text_setting(
            get("freshness_mode", "require_check"), "freshness_mode"
        )
        or "require_check",
        "deadline_ms": _bounded_int(
            get("deadline_ms"), "deadline_ms", DEFAULT_DEADLINE_MS, 100, MAX_DEADLINE_MS
        ),
    }


def _safe_env() -> dict[str, str]:
    """Return a fixed, credential/proxy-free child environment."""
    return dict(_SAFE_ENV)


def _set_child_cpu_limit(deadline_ms: int) -> None:
    """Best-effort POSIX CPU ceiling; Hermes also supplies the wall-clock ceiling."""
    try:
        import resource

        seconds = max(1, math.ceil(deadline_ms / 1000))
        resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds + 1))
    except Exception:
        # Wall-clock timeout remains authoritative on platforms without RLIMIT_CPU.
        return


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _run_child(settings: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Run the trusted executable, returning UTF-8 stdout or a bounded error marker."""
    request = {
        "package": settings["package"],
        "ecosystem": settings["ecosystem"],
        "selection": settings["selection"],
        "requested_version": settings["requested_version"],
        "query": settings["query"],
        "section_ids": settings["section_ids"],
        "context_max_bytes": settings["context_max_bytes"],
        "freshness_mode": settings["freshness_mode"],
        "deadline_ms": settings["deadline_ms"],
    }
    payload = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(payload) > MAX_REQUEST_BYTES:
        return None, "request_oversize"

    timeout_s = settings["deadline_ms"] / 1000
    try:
        process = subprocess.Popen(
            [settings["executable"]],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_safe_env(),
            # Do not inherit a caller-selected project/event cwd. The target and executable
            # are the only trusted routing inputs for this child.
            cwd=str(Path(settings["executable"]).parent),
            shell=False,
            close_fds=True,
            start_new_session=(os.name == "posix"),
            preexec_fn=(lambda: _set_child_cpu_limit(settings["deadline_ms"]))
            if os.name == "posix"
            else None,
        )
    except (OSError, ValueError):
        return None, "spawn_failed"

    try:
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write(payload)
        process.stdin.close()
        output = bytearray()
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout_s
        eof = False
        while not eof:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop_process(process)
                return None, "timeout"
            events = selector.select(min(remaining, 0.05))
            if not events:
                if process.poll() is not None:
                    # A final read observes EOF and prevents losing buffered JSON.
                    continue
                continue
            for key, _ in events:
                chunk = os.read(key.fd, 8192)
                if not chunk:
                    eof = True
                    break
                if len(output) + len(chunk) > MAX_CHILD_OUTPUT_BYTES:
                    _stop_process(process)
                    return None, "output_limit"
                output.extend(chunk)
        selector.close()
        remaining = max(0.01, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            _stop_process(process)
            return None, "timeout"
        try:
            decoded = output.decode("utf-8")
        except UnicodeDecodeError:
            return None, "invalid_utf8"
        if process.returncode != 0:
            # The preflight contract can emit a structured receipt alongside a non-zero
            # exit (for example, a no-match or oversized error). Preserve that receipt;
            # the caller adds the process failure as an explicit marker.
            return decoded, f"preflight_exit_{process.returncode}"
        return decoded, None
    except (OSError, ValueError):
        _stop_process(process)
        return None, "child_io_failed"
    finally:
        try:
            if process.stdout is not None:
                process.stdout.close()
        except OSError:
            pass


def _clean_inline(value: Any, limit: int = 256) -> str:
    text = str(value or "unknown").replace("\r", " ").replace("\n", " ").strip()
    return text[:limit] or "unknown"


def _truncate_utf8(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    suffix = "\n[documentation context truncated by Hermes hook]"
    room = max(0, limit - len(suffix.encode("utf-8")))
    return raw[:room].decode("utf-8", "ignore") + suffix


def _receipt_json(receipt: Any, *, error: str | None = None) -> str:
    if not isinstance(receipt, dict):
        receipt = {}
    try:
        encoded = json.dumps(receipt, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        encoded = "{}"
    if len(encoded.encode("utf-8")) <= MAX_RECEIPT_BYTES:
        return encoded
    source = receipt.get("source") if isinstance(receipt.get("source"), dict) else {}
    compact = {
        "schema": receipt.get("schema", SCHEMA),
        "source": {
            "kind": _clean_inline(source.get("kind", "unavailable")),
            "url": _clean_inline(source.get("url", ""), 1024),
        },
        "error": error or "receipt_oversize",
        "receipt_truncated": True,
    }
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"))


def _format_context(
    result: Mapping[str, Any],
    settings: Mapping[str, Any],
    process_error: str | None = None,
) -> str:
    raw_receipt = result.get("receipt")
    receipt: dict[str, Any] = raw_receipt if isinstance(raw_receipt, dict) else {}
    freshness = receipt.get("freshness")
    stale = isinstance(freshness, dict) and (
        freshness.get("stale") is True or freshness.get("state") == "stale"
    )
    found = (
        result.get("found") is True
        and isinstance(raw_receipt, dict)
        and not result.get("error")
    )
    error = _clean_inline(result.get("error")) if result.get("error") else None
    if not isinstance(raw_receipt, dict) and not error:
        error = "invalid_receipt"
    if process_error and not error:
        error = process_error
    status = "stale" if stale else ("ok" if found else "missing")
    raw_source = receipt.get("source")
    source: dict[str, Any] = raw_source if isinstance(raw_source, dict) else {}
    source_url = source.get("url") if isinstance(source, dict) else None
    source_kind = source.get("kind") if isinstance(source, dict) else None
    source_label = _clean_inline(source_url or source_kind or "unavailable", 1024)
    receipt_text = _receipt_json(receipt, error=error)
    lines = [
        f"DOCS_PREFLIGHT_STATUS={status}",
        f"DOCS_PREFLIGHT_SOURCE={source_label}",
        f"DOCS_PREFLIGHT_RECEIPT_JSON={receipt_text}",
    ]
    if error:
        lines.append(f"DOCS_PREFLIGHT_ERROR={error}")
    if process_error and process_error != error:
        lines.append(f"DOCS_PREFLIGHT_PROCESS_ERROR={_clean_inline(process_error)}")
    if status != "ok" and not error:
        lines.append(f"DOCS_PREFLIGHT_ERROR={status}_result")
    context = result.get("context") if isinstance(result.get("context"), str) else ""
    prefix = "\n".join(lines)
    if context:
        available = max(
            0,
            min(
                settings["context_max_bytes"],
                MAX_RETURN_BYTES - len(prefix.encode("utf-8")) - 32,
            ),
        )
        if available:
            prefix += "\n\nUNTRUSTED DOCUMENTATION DATA\n" + _truncate_utf8(
                context, available
            )
    return _truncate_utf8(prefix, MAX_RETURN_BYTES)


def _missing_context(error: str) -> str:
    receipt = {
        "schema": SCHEMA,
        "source": {"kind": "unavailable", "url": ""},
        "error": error,
    }
    return (
        "DOCS_PREFLIGHT_STATUS=missing\n"
        "DOCS_PREFLIGHT_SOURCE=unavailable\n"
        f"DOCS_PREFLIGHT_RECEIPT_JSON={_receipt_json(receipt, error=error)}\n"
        f"DOCS_PREFLIGHT_ERROR={_clean_inline(error)}"
    )


def _make_callback(settings: dict[str, Any]) -> Callable[..., dict[str, str] | None]:
    lock = threading.Lock()
    last_turn_id: str | None = None

    def pre_llm_call(**payload: Any) -> dict[str, str] | None:
        nonlocal last_turn_id
        turn_id = payload.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            return {"context": _missing_context("missing_turn_identity")}
        with lock:
            if turn_id == last_turn_id:
                return None
            last_turn_id = turn_id
            raw, error = _run_child(settings)
            if error and not raw:
                return {"context": _missing_context(error)}
            try:
                result = json.loads(raw or "")
            except (TypeError, ValueError):
                return {"context": _missing_context(error or "invalid_json")}
            if not isinstance(result, dict):
                return {"context": _missing_context("invalid_result")}
            if error:
                result.setdefault("error", error)
            return {"context": _format_context(result, settings, process_error=error)}

    return pre_llm_call


def register(ctx: Any) -> None:
    """Register one fail-open ``pre_llm_call`` hook with the installed Hermes host."""
    config_error = "invalid_configuration"
    try:
        settings = _settings_from_context(ctx)
    except _ConfigError as exc:
        settings = None
        config_error = str(exc)
    if settings is None:

        def config_callback(**_payload: Any) -> dict[str, str]:
            return {"context": _missing_context(config_error)}

        callback: Callable[..., dict[str, str] | None] = config_callback
    else:
        callback = _make_callback(settings)
    ctx.register_hook("pre_llm_call", callback)


__all__ = ["MAX_RETURN_BYTES", "register"]
