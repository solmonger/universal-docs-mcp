"""The single product CLI dispatcher; Slice 04 owns ``plan``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import BinaryIO, NoReturn

from .planner import plan_dependency_changes

MAX_OUTPUT_BYTES = 16 * 1024


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
    return parser


def _safe_relative(value: str, root: Path) -> Path:
    path = Path(value)
    if path.is_absolute() or not value or "\x00" in value:
        raise ValueError("manifest_path_invalid")
    # Resolve only for validation; the scoped reader still enforces no symlinks.
    candidate = (root / path).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError("manifest_path_escape") from None
    return path


def _validate_root(root: Path) -> Path:
    try:
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


def main(argv: list[str] | None = None, *, stdout: BinaryIO | None = None) -> int:
    """Emit exactly one bounded plan JSON object and no diagnostics."""
    output = stdout or sys.stdout.buffer
    try:
        args = _parser().parse_args(argv)
        if args.command != "plan":
            raise ValueError("command_required")
        root = _validate_root(args.project_root)
        before = _safe_relative(args.before, root)
        after = _safe_relative(args.after, root)
        plan = plan_dependency_changes(
            before, after, project_root=root, package=args.package
        )
        payload = plan.as_dict()
    except (OSError, RuntimeError, TypeError, ValueError):
        payload = _error("invalid_plan_request")
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) + 1 > MAX_OUTPUT_BYTES:
        encoded = json.dumps(_error("response_too_large"), separators=(",", ":")).encode()
    output.write(encoded + b"\n")
    output.flush()
    return 0 if payload.get("status") == "selected" else 1


if __name__ == "__main__":
    raise SystemExit(main())
