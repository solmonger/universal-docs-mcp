"""One bounded command-hook adapter shared by Claude Code and Codex.

The harness event is only a trigger.  A trusted, fixed preflight configuration
provides the executable and request; event fields are never used as commands,
paths, or package targets.
"""

from __future__ import annotations

import argparse
import json
import os
import selectors
import signal
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Literal, TextIO

from .context_delivery import MAX_PACKET_BYTES as MAX_CONTEXT_PACKET_BYTES
from .context_delivery import build_context_packet
from .preflight import PreflightRequest

SCHEMA = "universal-docs.preflight/v1"
HARNESS_EVENT = "UserPromptSubmit"
MAX_EVENT_BYTES = 64 * 1024
MAX_FILE_BYTES = 64 * 1024
MAX_PREFLIGHT_REQUEST_BYTES = 64 * 1024
MAX_PREFLIGHT_STDOUT_BYTES = 128 * 1024
MAX_PREFLIGHT_STDERR_BYTES = 16 * 1024
MAX_PACKET_BYTES = MAX_CONTEXT_PACKET_BYTES
MAX_HOOK_OUTPUT_BYTES = 16 * 1024
MIN_HOOK_TIMEOUT_MS = 50
MAX_HOOK_TIMEOUT_MS = 45_000

Harness = Literal["claude", "codex"]


class AdapterConfigError(ValueError):
    """A trusted adapter config or executable failed closed."""


@dataclass(frozen=True)
class HookConfig:
    """Fixed command and preflight request selected outside the hook event."""

    command: tuple[str, ...]
    request: PreflightRequest
    timeout_ms: int = 30_000

    def __post_init__(self) -> None:
        if not self.command or not all(isinstance(arg, str) for arg in self.command):
            raise AdapterConfigError("command_invalid")
        if (
            len(self.command) > 16
            or sum(len(arg.encode()) for arg in self.command) > 16_384
        ):
            raise AdapterConfigError("command_too_large")
        executable = Path(self.command[0])
        if not executable.is_absolute():
            raise AdapterConfigError("executable_must_be_absolute")
        try:
            info = executable.lstat()
        except OSError as exc:
            raise AdapterConfigError("executable_unavailable") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise AdapterConfigError("executable_not_regular")
        if not os.access(executable, os.X_OK):
            raise AdapterConfigError("executable_not_executable")
        if (
            isinstance(self.timeout_ms, bool)
            or not MIN_HOOK_TIMEOUT_MS <= self.timeout_ms <= MAX_HOOK_TIMEOUT_MS
        ):
            raise AdapterConfigError("timeout_invalid")


def _reject_constant(_: str) -> Any:
    raise ValueError("non_finite_json")


def _json_object(
    raw: bytes, *, error: str, too_large: str = "config_file_too_large"
) -> dict[str, Any]:
    if len(raw) > MAX_FILE_BYTES:
        raise AdapterConfigError(too_large)
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise AdapterConfigError(error) from exc
    if not isinstance(value, dict):
        raise AdapterConfigError(error)
    return value


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _read_regular(path: Path, *, too_large: str, not_regular: str) -> bytes:
    if not path.is_absolute():
        raise AdapterConfigError("path_must_be_absolute")
    try:
        info = path.lstat()
    except OSError as exc:
        raise AdapterConfigError("file_unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise AdapterConfigError(not_regular)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise AdapterConfigError("file_unavailable") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise AdapterConfigError(not_regular)
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_FILE_BYTES:
            chunk = os.read(fd, min(16 * 1024, MAX_FILE_BYTES + 1 - total))
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise AdapterConfigError(too_large)
    except AdapterConfigError:
        raise
    except OSError as exc:
        raise AdapterConfigError("file_unavailable") from exc
    finally:
        os.close(fd)
    raise AssertionError("unreachable")


def _validated_request(value: Any) -> PreflightRequest:
    try:
        return PreflightRequest.model_validate(value)
    except (TypeError, ValueError) as exc:
        raise AdapterConfigError("request_invalid") from exc


def _timeout(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AdapterConfigError("timeout_invalid")
    if not MIN_HOOK_TIMEOUT_MS <= value <= MAX_HOOK_TIMEOUT_MS:
        raise AdapterConfigError("timeout_invalid")
    return value


def _command(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise AdapterConfigError("command_invalid")
    command = tuple(value)
    if not command or not all(isinstance(arg, str) and arg for arg in command):
        raise AdapterConfigError("command_invalid")
    return command


def load_request(path: Path) -> PreflightRequest:
    """Read one fixed request file, rejecting links and special files."""

    raw = _read_regular(
        path,
        too_large="request_file_too_large",
        not_regular="request_file_not_regular",
    )
    return _validated_request(
        _json_object(
            raw, error="request_file_invalid", too_large="request_file_too_large"
        )
    )


def load_config(path: Path) -> HookConfig:
    """Read one fixed adapter config without following the final symlink."""

    raw = _read_regular(
        path,
        too_large="config_file_too_large",
        not_regular="config_file_not_regular",
    )
    value = _json_object(raw, error="config_file_invalid")
    if set(value) - {"preflight_command", "request", "timeout_ms"}:
        raise AdapterConfigError("config_file_invalid")
    if "preflight_command" not in value or "request" not in value:
        raise AdapterConfigError("config_file_invalid")
    timeout_ms = _timeout(value.get("timeout_ms", 30_000))
    return HookConfig(
        command=_command(value["preflight_command"]),
        request=_validated_request(value["request"]),
        timeout_ms=timeout_ms,
    )


def _filtered_environment() -> dict[str, str]:
    """Copy only process basics; credentials and proxy settings never cross."""

    allowed = {
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "TERM",
        "TZ",
        "PYTHONIOENCODING",
    }
    result = {
        key: value
        for key, value in os.environ.items()
        if key in allowed or (key.startswith("LC_") and key.isascii())
    }
    result.setdefault("PATH", "/usr/bin:/bin")
    result.setdefault("PYTHONIOENCODING", "utf-8")
    return result


@dataclass(frozen=True)
class _ProcessResult:
    status: str
    returncode: int | None
    stdout: bytes
    stderr: bytes


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    """Terminate the whole new-session process group, including descendants."""

    try:
        if hasattr(os, "killpg"):
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=0.25)
    except subprocess.TimeoutExpired:
        try:
            if hasattr(os, "killpg"):
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            pass


class _PreflightInterrupted(Exception):
    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


@contextmanager
def _signal_cleanup(process: subprocess.Popen[bytes]):
    """Make parent signals stop an external preflight before exiting."""

    previous: dict[int, Any] = {}
    installed: list[int] = []

    def cleanup(signum: int, _frame: Any) -> None:
        _stop_process(process)
        raise _PreflightInterrupted(signum)

    try:
        for name in ("SIGINT", "SIGTERM", "SIGHUP"):
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            try:
                previous[signum] = signal.getsignal(signum)
                signal.signal(signum, cleanup)
                installed.append(signum)
            except (OSError, ValueError):
                continue
        yield
    finally:
        for signum in installed:
            try:
                signal.signal(signum, previous[signum])
            except (OSError, ValueError):
                pass


def _invoke_preflight(config: HookConfig) -> _ProcessResult:
    request_bytes = (
        json.dumps(
            config.request.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    if len(request_bytes) > MAX_PREFLIGHT_REQUEST_BYTES:
        return _ProcessResult("request_too_large", None, b"", b"")

    try:
        process = subprocess.Popen(
            list(config.command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_filtered_environment(),
            shell=False,
            close_fds=True,
            start_new_session=True,
        )
    except (OSError, ValueError):
        return _ProcessResult("preflight_unavailable", None, b"", b"")

    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr = bytearray()
    input_offset = 0
    status = "completed"
    stdin_fd = process.stdin.fileno()
    stdout_fd = process.stdout.fileno()
    stderr_fd = process.stderr.fileno()
    for fd in (stdin_fd, stdout_fd, stderr_fd):
        os.set_blocking(fd, False)
    selector.register(stdin_fd, selectors.EVENT_WRITE, "stdin")
    selector.register(stdout_fd, selectors.EVENT_READ, "stdout")
    selector.register(stderr_fd, selectors.EVENT_READ, "stderr")
    deadline = time.monotonic() + config.timeout_ms / 1000

    signal_cleanup = _signal_cleanup(process)
    signal_cleanup.__enter__()
    try:
        while selector.get_map() or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                status = "preflight_timeout"
                _stop_process(process)
                break
            for key, _mask in selector.select(min(0.05, remaining)):
                fd = key.fd
                kind = key.data
                if kind == "stdin":
                    try:
                        written = os.write(fd, request_bytes[input_offset:])
                    except (BlockingIOError, InterruptedError):
                        continue
                    except BrokenPipeError:
                        selector.unregister(fd)
                        process.stdin.close()
                        continue
                    input_offset += written
                    if input_offset == len(request_bytes):
                        selector.unregister(fd)
                        process.stdin.close()
                    continue
                try:
                    chunk = os.read(fd, 8192)
                except (BlockingIOError, InterruptedError):
                    continue
                if not chunk:
                    selector.unregister(fd)
                    continue
                target = stdout if kind == "stdout" else stderr
                limit = (
                    MAX_PREFLIGHT_STDOUT_BYTES
                    if kind == "stdout"
                    else MAX_PREFLIGHT_STDERR_BYTES
                )
                target.extend(chunk[: max(0, limit + 1 - len(target))])
                if len(target) > limit and kind == "stdout":
                    status = "preflight_stdout_too_large"
                    _stop_process(process)
                    break
            if status != "completed":
                break
    except _PreflightInterrupted:
        status = "preflight_interrupted"
    finally:
        signal_cleanup.__exit__(None, None, None)
        if status == "completed":
            _stop_process(process)
        selector.close()
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass
        if process.poll() is None:
            try:
                process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                pass

    return _ProcessResult(status, process.returncode, bytes(stdout), bytes(stderr))


def _parse_preflight_output(raw: bytes) -> dict[str, Any] | None:
    if len(raw) > MAX_PREFLIGHT_STDOUT_BYTES:
        return None
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        RecursionError,
    ):
        return None
    return value if isinstance(value, dict) else None


def _build_packet(request: PreflightRequest, result: dict[str, Any]) -> str:
    """Compatibility wrapper around the shared delivery validator/formatter."""

    return build_context_packet(request, result)


def _block(code: str) -> dict[str, str]:
    return {
        "decision": "block",
        "reason": (
            f"Universal Docs preflight blocked this prompt: {code}. "
            "No documentation context was injected."
        ),
    }


def format_claude_output(packet: str) -> dict[str, Any]:
    """Claude Code UserPromptSubmit output format."""

    return {
        "hookSpecificOutput": {
            "hookEventName": HARNESS_EVENT,
            "additionalContext": packet,
        }
    }


def format_codex_output(packet: str) -> dict[str, Any]:
    """Codex UserPromptSubmit output format."""

    return {
        "hookSpecificOutput": {
            "hookEventName": HARNESS_EVENT,
            "additionalContext": packet,
        }
    }


def _parse_event(raw: bytes) -> bool:
    if len(raw) > MAX_EVENT_BYTES:
        return False
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        RecursionError,
    ):
        return False
    return isinstance(value, dict)


def handle_event(
    raw_event: bytes, *, harness: Harness, config: HookConfig
) -> tuple[dict[str, Any], int]:
    """Handle one host event and return a host payload plus hook exit code."""

    if harness not in {"claude", "codex"} or not _parse_event(raw_event):
        return _block("invalid_hook_event"), 0
    process = _invoke_preflight(config)
    if process.status != "completed":
        return _block(process.status), 0
    if process.returncode != 0:
        return _block("preflight_nonzero"), 0
    result = _parse_preflight_output(process.stdout)
    if result is None:
        return _block("preflight_malformed"), 0
    try:
        packet = _build_packet(config.request, result)
        payload = (
            format_claude_output(packet)
            if harness == "claude"
            else format_codex_output(packet)
        )
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(encoded) > MAX_HOOK_OUTPUT_BYTES:
            raise ValueError("adapter_output_too_large")
        return payload, 0
    except ValueError as exc:
        code = (
            str(exc)
            if str(exc)
            in {
                "preflight_not_found",
                "preflight_empty_context",
                "preflight_invalid_receipt",
                "preflight_no_match",
                "adapter_output_too_large",
            }
            else "preflight_invalid_receipt"
        )
        return _block(code), 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness", choices=("claude", "codex"), required=True)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--config", type=Path)
    source.add_argument("--request-file", type=Path)
    parser.add_argument("--preflight-executable", type=Path)
    parser.add_argument("--package")
    parser.add_argument("--ecosystem", choices=("python", "javascript", "rust"))
    parser.add_argument("--selection", choices=("requested", "latest"))
    parser.add_argument("--requested-version")
    parser.add_argument("--query")
    parser.add_argument("--section-id", action="append", default=[])
    parser.add_argument("--context-max-bytes", type=int, default=12_000)
    parser.add_argument(
        "--freshness-mode",
        choices=("require_check", "allow_cache", "allow_stale"),
    )
    parser.add_argument("--deadline-ms", type=int, default=30_000)
    parser.add_argument("--timeout-ms", type=int)
    return parser


def _config_from_args(args: argparse.Namespace) -> HookConfig:
    if args.config is not None:
        if (
            args.preflight_executable is not None
            or any(
                value is not None
                for value in (
                    args.package,
                    args.ecosystem,
                    args.selection,
                    args.requested_version,
                    args.query,
                    args.freshness_mode,
                )
            )
            or args.section_id
            or args.timeout_ms is not None
        ):
            raise AdapterConfigError("config_argument_conflict")
        return load_config(args.config)
    if args.request_file is not None:
        if args.preflight_executable is None:
            raise AdapterConfigError("executable_required")
        if (
            any(
                value is not None
                for value in (
                    args.package,
                    args.ecosystem,
                    args.selection,
                    args.requested_version,
                    args.query,
                    args.freshness_mode,
                )
            )
            or args.section_id
            or args.timeout_ms is not None
        ):
            raise AdapterConfigError("request_argument_conflict")
        request = load_request(args.request_file)
    else:
        required = (args.package, args.ecosystem, args.selection, args.freshness_mode)
        if args.preflight_executable is None or any(
            value is None for value in required
        ):
            raise AdapterConfigError("explicit_target_incomplete")
        request_value = {
            "package": args.package,
            "ecosystem": args.ecosystem,
            "selection": args.selection,
            "requested_version": args.requested_version,
            "query": args.query,
            "section_ids": args.section_id,
            "context_max_bytes": args.context_max_bytes,
            "freshness_mode": args.freshness_mode,
            "deadline_ms": args.deadline_ms,
        }
        request = _validated_request(request_value)
    timeout = args.timeout_ms if args.timeout_ms is not None else request.deadline_ms
    return HookConfig(
        command=(str(args.preflight_executable),), request=request, timeout_ms=timeout
    )


def _write_payload(payload: dict[str, Any], stdout: TextIO) -> None:
    stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    stdout.flush()


def main(
    argv: list[str] | None = None,
    *,
    stdin: BinaryIO | None = None,
    stdout: TextIO | None = None,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    output = stdout or sys.stdout
    try:
        config = _config_from_args(args)
    except AdapterConfigError as exc:
        _write_payload(_block(str(exc)), output)
        return 0
    event_stream = stdin or sys.stdin.buffer
    raw_event = event_stream.read(MAX_EVENT_BYTES + 1)
    payload, exit_code = handle_event(raw_event, harness=args.harness, config=config)
    _write_payload(payload, output)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
