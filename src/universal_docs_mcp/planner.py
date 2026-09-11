"""Deterministic, fail-closed Python dependency-change planning."""

from __future__ import annotations

import re
from dataclasses import dataclass
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": PLAN_SCHEMA,
            "status": self.status,
            "reason": self.reason,
            "package": self.package,
            "ecosystem": self.ecosystem,
            "previous_version": self.previous_version,
            "target_version": self.target_version,
            "resolution_source": self.resolution_source,
            "query": _QUERY if self.status == "selected" else None,
            "section_ids": [],
            "context_max_bytes": 4096,
            "freshness_mode": "require_check",
        }


def _source(path: Path) -> str:
    return path.name


def _abstain(reason: str, *, package: str | None, source: str) -> Plan:
    return Plan("abstained", reason, package, "python", None, None, source)


def _supported_python_manifest(path: Path) -> bool:
    name = path.name.lower()
    return (name.startswith("requirements") and name.endswith(".txt")) or name == "pyproject.toml"


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
    if pin is None or not pin.registry_lookup or pin.spec_redacted or pin.pinned is None:
        return False
    try:
        Version(pin.pinned)
    except InvalidVersion:
        return False
    return True


def _candidate(key: str, before: dict[str, Pin], after: dict[str, Pin]) -> DependencyChange | Plan:
    old = before.get(key)
    new = after.get(key)
    display = key
    if new is None:
        assert old is not None
        return Plan("abstained", "dependency_removed", display, "python", None, None, old.source)
    if not _is_exact_registry(new):
        reason = "non_registry_reference" if new.spec_redacted or not new.registry_lookup else "target_version_unresolved"
        return Plan("abstained", reason, display, "python", None, None, new.source)
    if old is not None and not _is_exact_registry(old):
        return Plan("abstained", "previous_version_unresolved", display, "python", None, None, new.source)
    if old is not None and old.pinned == new.pinned:
        return Plan("abstained", "dependency_unchanged", display, "python", old.pinned, None, new.source)
    target_version = new.pinned
    assert target_version is not None
    return DependencyChange(
        display,
        old.pinned if old else None,
        target_version,
        new.source,
        "exact_dependency_added" if old is None else "exact_dependency_changed",
    )


def _select(candidates: list[DependencyChange | Plan], package: str | None, source: str) -> Plan:
    if package is not None:
        wanted = canonicalize_python_name(package)
        matches = [item for item in candidates if canonicalize_python_name(item.package or "") == wanted]
        if not matches:
            return _abstain("unknown_package", package=wanted, source=source)
        item = matches[0]
    elif len(candidates) != 1:
        return _abstain("multiple_dependency_changes", package=None, source=source)
    else:
        item = candidates[0]
    if isinstance(item, Plan):
        return item
    return Plan("selected", item.reason, item.package, "python", item.previous_version, item.target_version, item.resolution_source)


def plan_dependency_changes(
    before: str | Path,
    after: str | Path,
    *,
    project_root: Path,
    package: str | None = None,
) -> Plan:
    """Plan from two explicit trusted manifest paths, without filesystem discovery."""
    before_path, after_path = Path(before), Path(after)
    source = _source(after_path)
    if not _supported_python_manifest(before_path) or not _supported_python_manifest(after_path):
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
        return _select(changes, package, source)
    if len(actionable) == 1:
        return _select(actionable, None, source)
    if len(actionable) > 1:
        return _abstain("multiple_dependency_changes", package=None, source=source)
    # Prefer a concrete fail-closed reason over a generic no-op.
    for item in changes:
        if isinstance(item, Plan) and item.reason not in {"dependency_unchanged"}:
            return item
    return _abstain("dependency_unchanged", package=None, source=source)


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
            "query": _QUERY,
            "section_ids": [],
            "context_max_bytes": 4096,
            "freshness_mode": "require_check",
        }
    )
