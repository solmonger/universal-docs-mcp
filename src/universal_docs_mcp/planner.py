"""Deterministic, fail-closed Python dependency-change planning."""

from __future__ import annotations

import ast
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from packaging.version import InvalidVersion, Version

from .lockfile import Pin, read_pins
from .preflight import PreflightRequest

PLAN_SCHEMA = "universal-docs.plan/v1"
_QUERY = "migration upgrade breaking changes quick start"


def canonicalize_python_name(name: str) -> str:
    """Return a PEP 503 normalized distribution name."""
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class DependencyChange:
    package: str
    previous_version: str | None
    target_version: str
    resolution_source: str
    reason: str


@dataclass(frozen=True)
class Plan:
    """The singular model-facing plan contract."""

    status: str
    reason: str
    package: str | None
    ecosystem: str
    previous_version: str | None
    target_version: str | None
    resolution_source: str
    query: str | None = None
    selection: dict[str, Any] = field(default_factory=lambda: {"mode": "generic"})

    def as_dict(self) -> dict[str, Any]:
        query = self.query if self.status == "selected" else None
        payload = {
            "schema": PLAN_SCHEMA,
            "status": self.status,
            "reason": self.reason,
            "package": self.package,
            "ecosystem": self.ecosystem,
            "previous_version": self.previous_version,
            "target_version": self.target_version,
            "resolution_source": self.resolution_source,
            "query": query
            if query
            else (_QUERY if self.status == "selected" else None),
            "section_ids": [],
            "context_max_bytes": 4096,
            "freshness_mode": "require_check",
        }
        if self.selection.get("mode") == "source_backed":
            payload["selection"] = self.selection
        return payload


def _source(path: Path) -> str:
    return path.name


def _abstain(reason: str, *, package: str | None, source: str) -> Plan:
    return Plan("abstained", reason, package, "python", None, None, source)


def _supported_python_manifest(path: Path) -> bool:
    name = path.name.lower()
    return (
        name.startswith("requirements") and name.endswith(".txt")
    ) or name == "pyproject.toml"


def _index(pins: Iterable[Pin]) -> tuple[dict[str, Pin] | None, str | None]:
    result: dict[str, Pin] = {}
    for pin in pins:
        key = canonicalize_python_name(pin.name)
        if key in result:
            if result[key].pinned != pin.pinned or result[key].spec != pin.spec:
                return None, "conflicting_dependency_pins"
            return None, "duplicate_dependency"
        result[key] = pin
    return result, None


def _read_manifest(path: Path, root: Path) -> tuple[dict[str, Pin] | None, str | None]:
    if not _supported_python_manifest(path):
        return None, "unsupported_manifest"
    try:
        pins = read_pins(path, root=root)
        return _index(pins)
    except (OSError, UnicodeDecodeError, ValueError):
        return None, "malformed_manifest"


def _is_exact_registry(pin: Pin | None) -> bool:
    if (
        pin is None
        or not pin.registry_lookup
        or pin.spec_redacted
        or pin.pinned is None
    ):
        return False
    try:
        Version(pin.pinned)
    except InvalidVersion:
        return False
    return True


def _candidate(
    key: str, before: dict[str, Pin], after: dict[str, Pin]
) -> DependencyChange | Plan:
    old = before.get(key)
    new = after.get(key)
    display = key
    if new is None:
        assert old is not None
        return Plan(
            "abstained", "dependency_removed", display, "python", None, None, old.source
        )
    if not _is_exact_registry(new):
        reason = (
            "non_registry_reference"
            if new.spec_redacted or not new.registry_lookup
            else "target_version_unresolved"
        )
        return Plan("abstained", reason, display, "python", None, None, new.source)
    if old is not None and not _is_exact_registry(old):
        return Plan(
            "abstained",
            "previous_version_unresolved",
            display,
            "python",
            None,
            None,
            new.source,
        )
    if old is not None and old.pinned == new.pinned:
        return Plan(
            "abstained",
            "dependency_unchanged",
            display,
            "python",
            old.pinned,
            None,
            new.source,
        )
    target_version = new.pinned
    assert target_version is not None
    return DependencyChange(
        display,
        old.pinned if old else None,
        target_version,
        new.source,
        "exact_dependency_added" if old is None else "exact_dependency_changed",
    )


def _select(
    candidates: list[DependencyChange | Plan], package: str | None, source: str
) -> Plan:
    if package is not None:
        wanted = canonicalize_python_name(package)
        matches = [
            item
            for item in candidates
            if canonicalize_python_name(item.package or "") == wanted
        ]
        if not matches:
            return _abstain("unknown_package", package=wanted, source=source)
        item = matches[0]
    elif len(candidates) != 1:
        return _abstain("multiple_dependency_changes", package=None, source=source)
    else:
        item = candidates[0]
    if isinstance(item, Plan):
        return item
    return Plan(
        "selected",
        item.reason,
        item.package,
        "python",
        item.previous_version,
        item.target_version,
        item.resolution_source,
    )


def _source_signal(
    package: str,
    version: str,
    task: str | None,
    source_paths: Iterable[str | Path],
    root: Path,
) -> tuple[str, dict[str, Any]] | str:
    paths = tuple(source_paths)
    if task is not None:
        task = " ".join(task.split())
        if (
            not task
            or "\x00" in task
            or any(ord(c) < 32 for c in task)
            or len(task.encode()) > 512
        ):
            return "task_signal_invalid"
    if task is not None and not paths:
        return "task_signal_source_required"
    if not paths:
        return _QUERY, {"mode": "generic"}
    if len(paths) > 8:
        return "task_signal_invalid"
    import_name = package.replace("-", "_")
    aliases: dict[str, str] = {}
    imported_names: set[str] = set()
    symbols: set[str] = set()
    total = 0
    for raw_path in paths:
        if not isinstance(raw_path, (str, Path)):
            return "task_signal_invalid"
        relative = Path(raw_path)
        if (
            relative.is_absolute()
            or not relative.parts
            or any(p in ("", ".", "..") for p in relative.parts)
            or relative.suffix != ".py"
        ):
            return "task_signal_invalid"
        path = root.joinpath(relative)
        try:
            current = root
            for part in relative.parts[:-1]:
                current /= part
                info = current.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    return "task_signal_invalid"
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                return "task_signal_invalid"
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            opened = os.fstat(fd)
            if (
                stat.S_ISLNK(opened.st_mode)
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != info.st_dev
                or opened.st_ino != info.st_ino
                or opened.st_size != info.st_size
            ):
                os.close(fd)
                return "task_signal_invalid"
            try:
                raw = os.read(fd, 32 * 1024 + 1)
            finally:
                os.close(fd)
            if len(raw) > 32 * 1024:
                return "task_signal_invalid"
            total += len(raw)
            if total > 128 * 1024:
                return "task_signal_invalid"
            tree = ast.parse(raw.decode("utf-8"), filename="<source>")
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError, RecursionError):
            return "task_signal_invalid"
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                top = node.module.split(".", 1)[0]
                if top == import_name:
                    if any(alias.name == "*" for alias in node.names):
                        return "task_signal_ambiguous"
                    for alias in node.names:
                        local_name = alias.asname or alias.name
                        imported_names.add(local_name)
                        symbols.add(local_name)
                        if alias.asname:
                            if alias.asname in aliases:
                                return "task_signal_ambiguous"
                            aliases[alias.asname] = f"{import_name}.{alias.name}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".", 1)[0] == import_name:
                        local = alias.asname or import_name
                        if local in aliases:
                            return "task_signal_ambiguous"
                        aliases[local] = import_name
            elif isinstance(node, ast.Call):
                if (
                    isinstance(node.func, ast.Name)
                    and node.func.id in {"__import__", "import_module"}
                ) or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "import_module"
                ):
                    return "task_signal_ambiguous"
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                root_name = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else node.func.value.id
                    if isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    else None
                )
                if root_name in aliases and aliases[root_name] == import_name:
                    if isinstance(node.func, ast.Attribute):
                        symbols.add(node.func.attr)
                    symbols.update(
                        keyword.arg for keyword in node.keywords if keyword.arg
                    )
                elif root_name in imported_names:
                    symbols.update(
                        keyword.arg for keyword in node.keywords if keyword.arg
                    )
    if not symbols and not aliases:
        return "task_signal_not_found"
    selected = sorted(
        term for term in symbols if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", term)
    )[:24]
    alias_terms = sorted(
        f"{key}->{value}" for key, value in aliases.items() if key != import_name
    )
    query_parts = [
        import_name,
        version,
        "migration",
        "upgrade",
        "breaking",
        "changes",
        "quick",
        "start",
    ]
    if task:
        query_parts.extend(task.split())
    query_parts.extend(selected)
    query = " ".join(query_parts)[:512].rstrip()
    if not query:
        return "task_signal_not_found"
    return query, {
        "mode": "source_backed",
        "source_files_inspected": len(paths),
        "symbols": selected,
        "aliases": alias_terms,
        "task_contributed": bool(task),
    }


def plan_dependency_changes(
    before: str | Path,
    after: str | Path,
    *,
    project_root: Path,
    package: str | None = None,
    task: str | None = None,
    source_paths: Iterable[str | Path] = (),
) -> Plan:
    """Plan from two explicit trusted manifest paths, without filesystem discovery."""
    before_path, after_path = Path(before), Path(after)
    source = _source(after_path)
    if not _supported_python_manifest(before_path) or not _supported_python_manifest(
        after_path
    ):
        return _abstain("unsupported_manifest", package=package, source=source)
    before_index, before_error = _read_manifest(before_path, project_root)
    if before_error:
        return _abstain(before_error, package=package, source=source)
    after_index, after_error = _read_manifest(after_path, project_root)
    if after_error:
        return _abstain(after_error, package=package, source=source)
    assert before_index is not None and after_index is not None
    all_keys = sorted(set(before_index) | set(after_index))
    changes = [_candidate(key, before_index, after_index) for key in all_keys]
    actionable = [item for item in changes if isinstance(item, DependencyChange)]
    if package is not None:
        selected = _select(changes, package, source)
    elif len(actionable) == 1:
        selected = _select(actionable, None, source)
    elif len(actionable) > 1:
        selected = _abstain("multiple_dependency_changes", package=None, source=source)
    else:
        # Prefer a concrete fail-closed reason over a generic no-op.
        selected = next(
            (
                item
                for item in changes
                if isinstance(item, Plan) and item.reason != "dependency_unchanged"
            ),
            _abstain("dependency_unchanged", package=None, source=source),
        )
    source_paths = tuple(source_paths)
    if not task and not source_paths:
        return selected
    if selected.status != "selected":
        return selected
    signal = _source_signal(
        selected.package or package or "",
        selected.target_version or "",
        task,
        source_paths,
        project_root,
    )
    if isinstance(signal, str):
        return _abstain(signal, package=selected.package, source=source)
    query, selection = signal
    return Plan(
        selected.status,
        selected.reason,
        selected.package,
        selected.ecosystem,
        selected.previous_version,
        selected.target_version,
        selected.resolution_source,
        query,
        selection,
    )


# Friendly singular alias for callers that model one before/after operation.
plan_dependency_change = plan_dependency_changes


def to_preflight_request(plan: Plan) -> PreflightRequest:
    """Convert only a selected plan to the existing strict request model."""
    if plan.status != "selected" or plan.target_version is None or plan.package is None:
        raise ValueError("plan_must_be_selected")
    return PreflightRequest.model_validate(
        {
            "package": plan.package,
            "ecosystem": plan.ecosystem,
            "selection": "requested",
            "requested_version": plan.target_version,
            "query": plan.query or _QUERY,
            "section_ids": [],
            "context_max_bytes": 4096,
            "freshness_mode": "require_check",
        }
    )
