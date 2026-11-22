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
    if profile_name == "frenchbot" or profile_name in disabled_profiles:
        raise ValueError("profile_disabled")
    cache_raw = ctx.get_config(
        "cache_dir", str(profile_home / "cache" / "universal-docs-preflight")
    )
    if not isinstance(cache_raw, str) or not Path(cache_raw).is_absolute():
        raise ValueError("invalid_configuration")
    cache_dir = Path(cache_raw).resolve(strict=False)
    if cache_dir.exists() and (cache_dir.is_symlink() or not cache_dir.is_dir()):
        raise ValueError("invalid_configuration")
    project_root_raw = ctx.get_config("project_root", "")
    project_root: Path | None = None
    if project_root_raw:
        # Optional: an explicit project for sessions whose own root has nothing
        # to read. An unusable value must not disable the hook — it is ignored,
        # and receipts keep showing the session-derived resolution.
        try:
            if (
                not isinstance(project_root_raw, str)
                or len(project_root_raw.encode()) > 4096
            ):
                raise ValueError("invalid_project_root")
            candidate = Path(project_root_raw).expanduser()
            if (
                not candidate.is_absolute()
                or candidate.is_symlink()
                or not candidate.is_dir()
            ):
                raise ValueError("invalid_project_root")
            project_root = candidate.resolve(strict=True)
        except (OSError, ValueError, TypeError):
            project_root = None
    settings = {
        "mode": mode,
        "executable": _regular_path(ctx.get_config("executable"), executable=True),
        "timeout_ms": timeout,
        "profile_name": profile_name,
        "cache_dir": cache_dir,
        "project_root": project_root,
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


#: Reasons that mean "this root has nothing to read": the only abstentions for
#: which an explicitly configured project root may stand in for the session's own.
_SESSION_ROOT_DRY_REASONS = frozenset({"no_manifest_found", "unreadable_project_root"})


def _native_callback(
    settings: dict[str, Any], payload: dict[str, Any]
) -> dict[str, str] | None:
    from universal_docs_mcp.planner import (
        select_current_packages,
        to_preflight_requests,
    )

    session = payload.get("session_id")
    if not isinstance(session, str) or not session:
        return _missing("missing_session_state")
    task = (
        payload.get("user_message")
        if isinstance(payload.get("user_message"), str)
        else None
    )
    if not task:
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
    if not _is_eligible_turn(task, root):
        return None
    receipt["resolution"] = {"cwd": str(cwd), "root": str(root), "source": resolution}
    bundle = select_current_packages(root, task=task, max_packages=3)
    fallback_root = settings.get("project_root")
    if (
        bundle.status != "selected"
        and bundle.reason in _SESSION_ROOT_DRY_REASONS
        and isinstance(fallback_root, Path)
        and fallback_root != root
    ):
        # The session's own root had nothing to read (e.g. desktop sessions
        # that start in the home directory). The operator named a project
        # explicitly, so it stands in — never a guess, and only for "nothing
        # here": every other abstention is respected as-is.
        fallback_bundle = select_current_packages(
            fallback_root, task=task, max_packages=3
        )
        if fallback_bundle.status == "selected":
            bundle = fallback_bundle
            root = fallback_root
            receipt["resolution"] = {
                "cwd": str(cwd),
                "root": str(root),
                "source": "configured_project_root",
            }
        else:
            receipt["fallback_attempted"] = {
                "root": str(fallback_root),
                "reason": fallback_bundle.reason,
            }
    receipt.update(
        {
            "status": bundle.status,
            "reason": bundle.reason,
            "package": bundle.plans[0].package if bundle.plans else None,
            "target_version": bundle.plans[0].target_version if bundle.plans else None,
            "packages": [],
        }
    )
    if bundle.status != "selected":
        _atomic_receipt(receipt_dir, receipt)
        return _missing(bundle.reason)
    request_paths: list[Path] = []
    try:
        requests = to_preflight_requests(
            bundle, total_budget_bytes=6144, freshness_mode="allow_cache"
        )
        contexts: list[str] = []
        packages: list[dict[str, Any]] = []
        local_stubs: list[Any] = []
        local_stub_evidence: list[dict[str, Any]] = []
        last_error: str | None = None
        for plan, request_model in zip(bundle.plans, requests, strict=True):
            request = request_model.model_dump(mode="json", exclude_none=True)
            request["deadline_ms"] = max(1000, min(settings["timeout_ms"] - 100, 9000))
            request_path = _request_file(receipt_dir / ".requests", request)
            request_paths.append(request_path)
            raw, error = _read_child(dict(settings, request_file=str(request_path)))
            if error:
                last_error = error
            framed = _missing(error) if error else _frame_context(raw)
            context = framed["context"]
            retrieved = (
                "DOCS_PREFLIGHT_STATUS=prepared\n" in context
                or "DOCS_PREFLIGHT_STATUS=prepared_stale\n" in context
            )
            cache_evidence = (
                _cache_evidence(settings["cache_dir"], plan, context)
                if retrieved
                else None
            )
            retrieved = retrieved and cache_evidence is not None
            stub_used = False
            if not retrieved:
                try:
                    from universal_docs_mcp.local_stubs import extract_local_stubs

                    stub = extract_local_stubs(
                        root,
                        plan.package,
                        plan.ecosystem,
                        plan.target_version,
                        symbols=plan.selection.get("symbols", ()),
                    )
                except (OSError, ValueError, TypeError):
                    stub = None
                if stub is not None:
                    local_stubs.append(stub)
                    local_stub_evidence.append(
                        {
                            "package": stub.package,
                            "version": stub.version,
                            "source": stub.source,
                            "version_binding": stub.version_binding,
                            "relative_path": stub.relative_path,
                            "content_bytes": stub.content_bytes,
                        }
                    )
                    retrieved = True
                    stub_used = True
            packages.append(
                {
                    "package": plan.package,
                    "target_version": plan.target_version,
                    "status": "retrieved" if retrieved else "failed",
                    "error": None
                    if retrieved
                    else (error or "cache_evidence_missing_or_mismatched"),
                    "cache_evidence": cache_evidence,
                    "local_stub_used": stub_used,
                }
            )
            if retrieved and cache_evidence is not None:
                contexts.append(
                    context.removeprefix(
                        "DOCS_PREFLIGHT_STATUS=prepared\n"
                    ).removeprefix("DOCS_PREFLIGHT_STATUS=prepared_stale\n")
                )
        if len(contexts) > 1 or (
            local_stubs and contexts and "Receipt: UDCTX:" in contexts[0]
        ):
            from universal_docs_mcp.context_delivery import build_bundle_packet

            context = build_bundle_packet(contexts, local_stubs=local_stubs)
        elif local_stubs:
            from universal_docs_mcp.context_delivery import build_bundle_packet

            context = build_bundle_packet([], local_stubs=local_stubs)
        else:
            context = contexts[0] if contexts else ""
        if local_stubs:
            receipt["local_stub_used"] = True
            receipt["local_stub_evidence"] = local_stub_evidence
        retrieved = bool(context) and bool(contexts or local_stubs)
        if len(context.encode("utf-8")) > MAX_CONTEXT_BYTES:
            retrieved = False
        receipt.update(
            {
                "status": "retrieved" if retrieved else "failed",
                "error": None
                if retrieved
                else (last_error or "cache_evidence_missing_or_mismatched"),
                "query": bundle.plans[0].query,
                "selection": bundle.plans[0].selection,
                "context_sha256": hashlib.sha256(context.encode()).hexdigest()
                if retrieved
                else None,
                "cache_evidence": packages[0]["cache_evidence"] if packages else None,
                "packages": packages,
            }
        )
        _atomic_receipt(receipt_dir, receipt)
        return (
            {"context": "DOCS_PREFLIGHT_STATUS=prepared\n" + context}
            if retrieved
            else _missing(receipt["error"])
        )
    except Exception:
        receipt.update({"status": "failed", "error": "preflight_failed"})
        _atomic_receipt(receipt_dir, receipt)
        return _missing("preflight_failed")
    finally:
        for request_path in request_paths:
            try:
                request_path.unlink()
            except FileNotFoundError:
                pass


_TECHNICAL_TERMS = re.compile(
    r"\b(code|coding|debug|implement|package|dependency|api|sdk|migration|library|import|upgrade|install|technical|fix|error|bug|test|pytest|build|lint|refactor|endpoint|schema|async|client|hook|mcp|plugin)\b",
    re.I,
)
_ERROR_PATTERN = re.compile(
    r"\b(ImportError|ModuleNotFoundError|AttributeError|TypeError|ValidationError|Cannot find module|error\[E0\d+)\b",
    re.I,
)


def _is_eligible_turn(task: str, root: Path | None = None) -> bool:
    if _TECHNICAL_TERMS.search(task) or _ERROR_PATTERN.search(task):
        return True
    if root is None:
        return False
    try:
        from universal_docs_mcp.lockfile import resolve_project_dependencies

        dependencies = resolve_project_dependencies(root)
    except (OSError, ValueError):
        return False
    lowered = task.lower()
    return any(
        re.search(r"(?<![\w@])" + re.escape(alias) + r"(?![\w])", lowered)
        for dependency in dependencies
        for alias in {
            dependency.name.lower(),
            dependency.name.lower().replace("-", "_"),
            dependency.name.lower().replace("_", "-"),
        }
    )


def _post_tool_callback(
    settings: dict[str, Any],
    payload: dict[str, Any],
    seen: set[tuple[str, str, str, str]],
) -> dict[str, str] | None:
    output = next(
        (
            payload.get(key)
            for key in ("output", "tool_output", "result")
            if isinstance(payload.get(key), str)
        ),
        "",
    )
    if (
        not output
        or len(output.encode("utf-8")) > 16 * 1024
        or not _ERROR_PATTERN.search(output)
    ):
        return None
    session = payload.get("session_id")
    if not isinstance(session, str) or not session:
        return None
    state = _session_state(session)
    if state is None:
        return None
    _cwd, root, _resolution = state
    from universal_docs_mcp.planner import select_for_error, to_preflight_requests

    bundle = select_for_error(root, error_text=output)
    if bundle.status != "selected":
        return None
    signature = hashlib.sha256(" ".join(output.split()).encode()).hexdigest()
    fresh = [
        plan
        for plan in bundle.plans
        if (session, plan.package, plan.target_version or "", signature) not in seen
    ]
    if not fresh:
        return None
    bundle = type(bundle)(
        bundle.status, bundle.reason, tuple(fresh), bundle.resolution_root_has_manifest
    )
    requests = to_preflight_requests(
        bundle, total_budget_bytes=6144, freshness_mode="allow_cache"
    )
    contexts: list[str] = []
    local_stubs: list[Any] = []
    receipt_packages: list[dict[str, Any]] = []
    for plan, request_model in zip(fresh, requests, strict=True):
        request = request_model.model_dump(mode="json", exclude_none=True)
        request["deadline_ms"] = max(1000, min(settings["timeout_ms"] - 100, 9000))
        request_path = _request_file(
            _hermes_home() / "receipts" / "universal-docs" / ".requests", request
        )
        try:
            raw, error = _read_child(dict(settings, request_file=str(request_path)))
        finally:
            request_path.unlink(missing_ok=True)
        framed = _missing(error) if error else _frame_context(raw)
        context = framed["context"]
        retrieved = (
            "DOCS_PREFLIGHT_STATUS=prepared\n" in context
            or "DOCS_PREFLIGHT_STATUS=prepared_stale\n" in context
        )
        evidence = (
            _cache_evidence(settings["cache_dir"], plan, context) if retrieved else None
        )
        stub_evidence = None
        if not (retrieved and evidence):
            try:
                from universal_docs_mcp.local_stubs import extract_local_stubs

                stub = extract_local_stubs(
                    root,
                    plan.package,
                    plan.ecosystem,
                    plan.target_version,
                    symbols=plan.selection.get("symbols", ()),
                )
            except (OSError, ValueError, TypeError):
                stub = None
            if stub is not None:
                local_stubs.append(stub)
                stub_evidence = {
                    "source": stub.source,
                    "version_binding": stub.version_binding,
                    "relative_path": stub.relative_path,
                    "content_bytes": stub.content_bytes,
                }
        if retrieved and evidence:
            contexts.append(context.split("\n", 1)[1])
            seen.add((session, plan.package, plan.target_version or "", signature))
        elif stub_evidence:
            seen.add((session, plan.package, plan.target_version or "", signature))
        receipt_packages.append(
            {
                "package": plan.package,
                "target_version": plan.target_version,
                "cache_evidence": evidence,
                "local_stub_evidence": stub_evidence,
                "status": "retrieved"
                if (retrieved and evidence) or stub_evidence
                else "failed",
            }
        )
    if not contexts and not local_stubs:
        return None
    if len(contexts) > 1 or (
        local_stubs and contexts and "Receipt: UDCTX:" in contexts[0]
    ):
        from universal_docs_mcp.context_delivery import build_bundle_packet

        context = build_bundle_packet(contexts, local_stubs=local_stubs)
    elif local_stubs:
        from universal_docs_mcp.context_delivery import build_bundle_packet

        context = build_bundle_packet([], local_stubs=local_stubs)
    else:
        context = contexts[0]
    if len(context.encode("utf-8")) > MAX_CONTEXT_BYTES:
        return None
    receipt = {
        "schema": "universal-docs.hermes-consumption/v1",
        "status": "retrieved",
        "reason": "post_tool_error",
        "session_id": session,
        "error_signature": signature,
        "packages": receipt_packages,
        "local_stub_used": bool(local_stubs),
    }
    _atomic_receipt(_hermes_home() / "receipts" / "universal-docs", receipt)
    return {"context": "DOCS_PREFLIGHT_STATUS=prepared\n" + context}


def register(ctx: Any) -> None:
    """Inject explicit prepared/missing state; Hermes pre_llm_call is fail-open."""
    try:
        settings = _settings(ctx)
    except (OSError, ValueError, TypeError):
        # Native mode is opt-in; malformed static settings must not activate it.
        return
    lock = threading.Lock()
    last_turn_id: str | None = None
    seen_errors: set[tuple[str, str, str, str]] = set()

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
    try:
        ctx.register_hook(
            "post_tool_call",
            lambda **payload: (
                _post_tool_callback(settings, payload, seen_errors)
                if settings["mode"] == "native"
                else None
            ),
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        # Older Hermes hook managers and narrow test doubles expose only the
        # pre-LLM event; keep their supported registration intact.
        pass
