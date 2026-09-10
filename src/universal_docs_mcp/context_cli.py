"""Neutral single-process context delivery CLI for agent adapters.

Usage is intentionally narrow: ``universal-docs-context --request-file
/absolute/trusted/request.json``.  The request is parsed by the existing
preflight parser and the existing async runner is called directly in this
process; this module does not inspect prompts, manifests, or model settings.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import BinaryIO, NoReturn

from .command_hook import AdapterConfigError, _read_regular
from .context_delivery import CONTEXT_SCHEMA, ContextDelivery, deliver_result
from .preflight import MAX_INPUT_BYTES, DocsCache, _run_cli, parse_request

MAX_OUTPUT_BYTES = 16 * 1024


def _unavailable(error: str) -> ContextDelivery:
    return ContextDelivery(status="unavailable", context="", error=error)


class _NoExitArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError(message)


def _request_file(path: Path) -> bytes:
    return _read_regular(
        path,
        too_large="request_file_too_large",
        not_regular="request_file_not_regular",
    )


def _write_delivery(delivery: ContextDelivery, stdout: BinaryIO) -> None:
    payload = delivery.as_dict()
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError, OverflowError):
        encoded = json.dumps(
            {
                "schema": CONTEXT_SCHEMA,
                "status": "unavailable",
                "context": "",
                "error": "internal_error",
            },
            separators=(",", ":"),
        ).encode("utf-8")
    if len(encoded) > MAX_OUTPUT_BYTES:
        encoded = json.dumps(
            {
                "schema": CONTEXT_SCHEMA,
                "status": "unavailable",
                "context": "",
                "error": "response_too_large",
            },
            separators=(",", ":"),
        ).encode("utf-8")
        delivery = _unavailable("response_too_large")
    stdout.write(encoded + b"\n")
    stdout.flush()


def _build_parser() -> argparse.ArgumentParser:
    parser = _NoExitArgumentParser(
        description=__doc__,
        add_help=False,
    )
    parser.add_argument("--request-file", type=Path)
    return parser


def main(argv: list[str] | None = None, *, stdout: BinaryIO | None = None) -> int:
    """Emit exactly one bounded JSON line and return the delivery status."""

    output = stdout or sys.stdout.buffer
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except (SystemExit, ValueError):
        _write_delivery(_unavailable("invalid_arguments"), output)
        return 1
    if args.request_file is None:
        _write_delivery(_unavailable("request_file_required"), output)
        return 1

    try:
        raw = _request_file(args.request_file)
    except AdapterConfigError as exc:
        code = str(exc)
        if code not in {
            "path_must_be_absolute",
            "file_unavailable",
            "request_file_too_large",
            "request_file_not_regular",
        }:
            code = "request_file_unavailable"
        _write_delivery(_unavailable(code), output)
        return 1
    if len(raw) > MAX_INPUT_BYTES:
        _write_delivery(_unavailable("request_file_too_large"), output)
        return 1
    try:
        request = parse_request(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError):
        _write_delivery(_unavailable("invalid_request"), output)
        return 1

    cache = DocsCache()
    try:
        try:
            result = asyncio.run(_run_cli(request, cache))
        except asyncio.TimeoutError:
            _write_delivery(_unavailable("preflight_unavailable"), output)
            return 1
        except (TypeError, ValueError, RecursionError, OverflowError, OSError):
            _write_delivery(_unavailable("preflight_unavailable"), output)
            return 1
        delivery = deliver_result(request, result)
        _write_delivery(delivery, output)
        return 0 if delivery.status in {"prepared", "prepared_stale"} else 1
    finally:
        cache.close()


if __name__ == "__main__":
    raise SystemExit(main())
