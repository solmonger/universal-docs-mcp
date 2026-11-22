"""Deterministic, fail-closed Python dependency-change planning."""

from __future__ import annotations

import ast
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

from packaging.version import InvalidVersion, Version

from .lockfile import Pin, read_pins
from .preflight import PreflightRequest

PLAN_SCHEMA = "universal-docs.plan/v1"
_QUERY = "migration upgrade breaking changes quick start"
_MAX_SOURCE_FILES = 8
_MAX_ALIAS_ENTRIES = 24
_MAX_ALIAS_ENTRY_LENGTH = 128
_MAX_TASK_RAW_LENGTH = 4096


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


#: Manifests this planner recognises but does not read. Their presence makes an
#: abstention "unsupported_manifest" (a project is here, we cannot read it)
#: instead of "no_manifest_found" (there is nothing here to read at all).
_FOREIGN_MANIFESTS = frozenset(
    {
        "package.json",
        "cargo.toml",
        "pipfile",
        "pipfile.lock",
        "poetry.lock",
        "pdm.lock",
        "setup.py",
        "setup.cfg",
        "environment.yml",
        "environment.yaml",
        "go.mod",
        "gemfile",
        "pom.xml",
        "build.gradle",
        "composer.json",
        "mix.exs",
        "pubspec.yaml",
        "package.swift",
        "deno.json",
    }
)


def _foreign_manifest(entry: Path) -> bool:
    """True when *entry* is a manifest file this planner does not support."""
    name = entry.name.lower()
    return (name in _FOREIGN_MANIFESTS or name.endswith(".csproj")) and entry.is_file()


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


def _read_source_scoped(root: Path, relative: Path) -> bytes:
    if (
        os.open not in os.supports_dir_fd
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
    ):
        raise OSError("descriptor_relative_traversal_unavailable")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags = flags | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(root, directory_flags)
    fds = [root_fd]
    try:
        current = root_fd
        for part in relative.parts[:-1]:
            current = os.open(part, directory_flags, dir_fd=current)
            fds.append(current)
        fd = os.open(
            relative.parts[-1],
            flags | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=current,
        )
        fds.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 32 * 1024:
            raise OSError("source_not_regular_or_oversized")
        chunks: list[bytes] = []
        size = 0
        while size <= 32 * 1024:
            chunk = os.read(fd, min(8192, 32 * 1024 + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        after = os.fstat(fd)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_dev != before.st_dev
            or after.st_ino != before.st_ino
            or after.st_size != before.st_size
            or size > 32 * 1024
        ):
            raise OSError("source_changed")
        return b"".join(chunks)
    finally:
        for fd in reversed(fds):
            try:
                os.close(fd)
            except OSError:
                pass


def _attribute_root(node: ast.AST) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _bounded_source_paths(
    source_paths: Iterable[str | Path],
) -> tuple[str | Path, ...] | None:
    """Consume no more than nine caller items before enforcing the eight-file cap."""
    try:
        iterator = iter(source_paths)
        paths: list[str | Path] = []
        for _ in range(_MAX_SOURCE_FILES + 1):
            try:
                paths.append(next(iterator))
            except StopIteration:
                break
    except Exception:
        return None
    return tuple(paths)


def _normalize_task(task: object) -> str | None:
    if not isinstance(task, str) or len(task) > _MAX_TASK_RAW_LENGTH:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in task):
        return None
    normalized = " ".join(task.split())
    try:
        if not normalized or len(normalized.encode("utf-8")) > 512:
            return None
    except UnicodeEncodeError:
        return None
    return normalized


def _task_terms_appended(query: str, task: str | None) -> tuple[str, bool]:
    """Append normalized task words to a bounded query; report contribution."""
    contributed = False
    if task:
        for term in task.split():
            if len(query) + 1 + len(term) > 512:
                break
            query += " " + term
            contributed = True
    return query, contributed


def _source_signal(
    package: str,
    version: str,
    task: str | None,
    source_paths: Iterable[str | Path],
    root: Path,
) -> tuple[str, dict[str, Any]] | str:
    paths = (
        source_paths
        if isinstance(source_paths, tuple)
        else _bounded_source_paths(source_paths)
    )
    if paths is None:
        return "task_signal_invalid"
    if task is not None:
        task = _normalize_task(task)
        if task is None:
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
    trees: list[ast.AST] = []
    total = 0
    tracked: set[str] = set()
    try:
        for raw_path in paths:
            if not isinstance(raw_path, (str, Path)):
                return "task_signal_invalid"
            relative = Path(raw_path)
            if (
                relative.is_absolute()
                or not relative.parts
                or relative.suffix != ".py"
                or any(p in ("", ".", "..") for p in relative.parts)
            ):
                return "task_signal_invalid"
            raw = _read_source_scoped(root, relative)
            total += len(raw)
            if total > 128 * 1024:
                return "task_signal_invalid"
            trees.append(ast.parse(raw.decode("utf-8"), filename="<source>"))
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError, RecursionError):
        return "task_signal_invalid"
    for tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (
                (
                    isinstance(node.func, ast.Name)
                    and node.func.id in {"__import__", "import_module"}
                )
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "import_module"
                )
            ):
                return "task_signal_ambiguous"
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module.split(".", 1)[0] == import_name:
                    if any(a.name == "*" for a in node.names):
                        return "task_signal_ambiguous"
                    for a in node.names:
                        local = a.asname or a.name
                        value = f"{node.module}.{a.name}"
                        if local in aliases and aliases[local] != value:
                            return "task_signal_ambiguous"
                        if local in imported_names and aliases.get(local) != value:
                            return "task_signal_ambiguous"
                        imported_names.add(local)
                        aliases[local] = value
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".", 1)[0] == import_name:
                        local = a.asname or import_name
                        if local in aliases and aliases[local] != import_name:
                            return "task_signal_ambiguous"
                        aliases[local] = import_name
                    else:
                        local = a.asname or (
                            a.name.split(".", 1)[0] if "." in a.name else a.name
                        )
                        if local in aliases or local in imported_names:
                            return "task_signal_ambiguous"
    tracked = set(aliases) | imported_names
    for tree in trees:
        for node in ast.walk(tree):
            bound: set[str] = set()
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                if isinstance(node, ast.AugAssign):
                    targets = [node.target]
                for target in targets:
                    bound.update(
                        n.id for n in ast.walk(target) if isinstance(n, ast.Name)
                    )
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bound.add(node.name)
                bound.update(
                    a.arg
                    for a in node.args.posonlyargs
                    + node.args.args
                    + node.args.kwonlyargs
                )
                if node.args.vararg:
                    bound.add(node.args.vararg.arg)
                if node.args.kwarg:
                    bound.add(node.args.kwarg.arg)
            elif isinstance(node, ast.ClassDef):
                bound.add(node.name)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                bound.update(
                    n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)
                )
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars:
                        bound.update(
                            n.id
                            for n in ast.walk(item.optional_vars)
                            if isinstance(n, ast.Name)
                        )
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bound.add(node.name)
            elif isinstance(node, ast.NamedExpr):
                bound.add(node.target.id)
            elif isinstance(node, ast.Lambda):
                bound.update(
                    a.arg
                    for a in node.args.posonlyargs
                    + node.args.args
                    + node.args.kwonlyargs
                )
                if node.args.vararg:
                    bound.add(node.args.vararg.arg)
                if node.args.kwarg:
                    bound.add(node.args.kwarg.arg)
            elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
                bound.add(node.name)
            elif isinstance(node, ast.MatchMapping) and node.rest:
                bound.add(node.rest)
            elif isinstance(node, ast.Delete):
                bound.update(n.id for n in ast.walk(node) if isinstance(n, ast.Name))
            if bound & tracked:
                return "task_signal_ambiguous"
            if isinstance(node, ast.Call) and (
                (_attribute_root(node.func) in tracked)
                or (isinstance(node.func, ast.Name) and node.func.id in tracked)
            ):
                func = node.func
                while isinstance(func, ast.Attribute):
                    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", func.attr):
                        # Attributes and the called name are both useful terms.
                        symbols.add(func.attr)
                    func = func.value
                if isinstance(node.func, ast.Name):
                    symbols.add(node.func.id)
                for keyword in node.keywords:
                    if keyword.arg and re.fullmatch(
                        r"[A-Za-z_][A-Za-z0-9_]{0,63}", keyword.arg
                    ):
                        symbols.add(keyword.arg)
            if isinstance(node, ast.Attribute) and _attribute_root(node) in tracked:
                attribute = node
                while isinstance(attribute, ast.Attribute):
                    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", attribute.attr):
                        symbols.add(attribute.attr)
                    attribute = attribute.value
    symbols.update(
        v.rsplit(".", 1)[-1] for v in aliases.values() if v and v != import_name
    )
    if not aliases:
        return "task_signal_not_found"
    selected = sorted(
        s for s in symbols if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", s)
    )[:24]
    if not selected:
        return "task_signal_not_found"
    base = " ".join(
        [
            import_name,
            version,
            "migration",
            "upgrade",
            "breaking",
            "changes",
            "quick",
            "start",
        ]
    )
    query = base
    for term in selected:
        if len(query) + 1 + len(term) <= 512:
            query += " " + term
    query, task_contributed = _task_terms_appended(query, task)
    bounded_aliases = [
        f"{key}->{value}"
        for key, value in sorted(aliases.items())
        if key != import_name and len(f"{key}->{value}") <= _MAX_ALIAS_ENTRY_LENGTH
    ][:_MAX_ALIAS_ENTRIES]
    return query, {
        "mode": "source_backed",
        "source_files_inspected": len(paths),
        "symbols": selected,
        "aliases": bounded_aliases,
        "task_contributed": task_contributed,
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
    if task is None and not source_paths:
        return selected
    if selected.status != "selected":
        return selected
    bounded_paths = _bounded_source_paths(source_paths)
    if bounded_paths is None:
        return _abstain("task_signal_invalid", package=selected.package, source=source)
    signal = _source_signal(
        selected.package or package or "",
        selected.target_version or "",
        task,
        bounded_paths,
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


def select_current_package(
    project_root: Path,
    *,
    task: str | None = None,
    source_paths: Iterable[str | Path] | None = None,
) -> Plan:
    """Select one exact registry pin from trusted current project evidence."""
    root = Path(project_root)
    manifests = [
        p for p in (root / "pyproject.toml", root / "requirements.txt") if p.is_file()
    ]
    if len(manifests) != 1:
        if manifests:
            return _abstain("ambiguous_manifest", package=None, source="")
        # Neither canonical name exists. Accept a single bounded
        # requirements-family manifest (the same set lockfile.read_pins
        # supports); more than one candidate abstains rather than picking
        # arbitrarily. When there is no candidate at all, the reason says which
        # silence this is: a project manifest we do not read, or nothing here
        # that declares dependencies.
        try:
            entries = list(root.iterdir())
        except OSError:
            # The root itself could not be inspected (missing directory,
            # permissions) — which is not an unsupported manifest.
            return _abstain("unreadable_project_root", package=None, source="")
        family = sorted(
            (
                p
                for p in entries
                if p.is_file()
                and not p.is_symlink()
                and p.name.lower().startswith("requirements")
                and p.name.lower().endswith(".txt")
            ),
            key=lambda p: p.name,
        )
        if len(family) > 1:
            return _abstain("ambiguous_manifest", package=None, source="")
        if not family:
            if any(_foreign_manifest(p) for p in entries):
                bundle = select_current_packages(
                    root, task=task, active_files=source_paths or ()
                )
                if bundle.status == "selected" and len(bundle.plans) == 1:
                    return _plan_from_bundle(bundle.plans[0])
                return _abstain("unsupported_manifest", package=None, source="")
            return _abstain("no_manifest_found", package=None, source="")
        manifests = family
    manifest = manifests[0]
    index, error = _read_manifest(manifest, root)
    if error or index is None:
        return _abstain(
            error or "malformed_manifest", package=None, source=_source(manifest)
        )
    candidates = {k: p for k, p in index.items() if _is_exact_registry(p)}
    if not candidates:
        bundle = select_current_packages(
            root, task=task, active_files=source_paths or ()
        )
        if bundle.status == "selected" and len(bundle.plans) == 1:
            return _plan_from_bundle(bundle.plans[0])
        return _abstain(
            "no_exact_registry_candidate", package=None, source=_source(manifest)
        )
    paths = source_paths
    if paths is None:
        # Bound discovery while walking; never materialize an unbounded rglob.
        discovered: list[str] = []
        visited_directories = 0
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            visited_directories += 1
            if visited_directories > 64:
                break
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name not in {".git", ".venv", "venv", "node_modules", "__pycache__"}
                and not (Path(directory) / name).is_symlink()
            )
            for name in sorted(filenames):
                path = Path(directory) / name
                if path.suffix == ".py" and not path.is_symlink():
                    discovered.append(str(path.relative_to(root)))
                    if len(discovered) >= _MAX_SOURCE_FILES:
                        break
            if len(discovered) >= _MAX_SOURCE_FILES:
                break
        paths = tuple(discovered)
    else:
        paths = tuple(paths)
    normalized = _normalize_task(task) if task is not None else None
    if task is not None and normalized is None:
        return _abstain("task_signal_invalid", package=None, source=_source(manifest))
    named_candidates: list[str] = []
    if normalized:
        named_candidates = [
            key
            for key in candidates
            if re.search(
                r"(?<![A-Za-z0-9])" + re.escape(key) + r"(?![A-Za-z0-9])",
                normalized,
                re.I,
            )
        ]
    if len(named_candidates) == 1:
        candidate_keys = named_candidates
    elif len(candidates) > 64:
        return _abstain("too_many_candidates", package=None, source=_source(manifest))
    else:
        candidate_keys = sorted(candidates)
    proven = []
    for key in candidate_keys:
        pin = candidates[key]
        signal = _source_signal(key, pin.pinned or "", None, paths, root)
        if not isinstance(signal, str) and signal[1].get("mode") == "source_backed":
            proven.append(key)
    selected = None
    if len(proven) == 1:
        selected = proven[0]
    elif normalized:
        named = [
            k
            for k in (proven or named_candidates)
            if re.search(
                r"(?<![A-Za-z0-9])" + re.escape(k) + r"(?![A-Za-z0-9])",
                normalized,
                re.I,
            )
        ]
        if len(named) == 1:
            selected = named[0]
    if selected is None:
        return _abstain(
            "ambiguous_candidates"
            if len(proven) > 1 or len(candidates) > 1
            else "task_package_not_locally_proven",
            package=None,
            source=_source(manifest),
        )
    pin = candidates[selected]
    signal = _source_signal(selected, pin.pinned or "", normalized, paths, root)
    if isinstance(signal, str):
        if (
            signal in {"task_signal_not_found", "task_signal_source_required"}
            and normalized
            and len(named_candidates) == 1
            and selected == named_candidates[0]
        ):
            # The spec allows an unambiguous task-named locally pinned exact
            # version as the research signal when the bounded source scan
            # proves nothing. Hard fail-closed reasons (ambiguity, invalid
            # input) never relax here.
            base = f"{selected.replace('-', '_')} {pin.pinned or ''} {_QUERY}".strip()
            query, task_contributed = _task_terms_appended(base, normalized)
            return Plan(
                "selected",
                "current_exact_pin_named",
                selected,
                "python",
                None,
                pin.pinned,
                pin.source,
                query,
                {
                    "mode": "task_named",
                    "source_files_inspected": len(paths),
                    "task_contributed": task_contributed,
                },
            )
        return _abstain(signal, package=selected, source=_source(manifest))
    query, selection = signal
    return Plan(
        "selected",
        "current_exact_pin",
        selected,
        "python",
        None,
        pin.pinned,
        pin.source,
        query,
        selection,
    )


# Friendly singular alias for callers that model one before/after operation.
plan_dependency_change = plan_dependency_changes


def to_preflight_request(
    plan: Plan,
    *,
    freshness_mode: Literal[
        "require_check", "allow_cache", "allow_stale"
    ] = "require_check",
    context_max_bytes: int = 4096,
) -> PreflightRequest:
    """Convert a selected plan to the existing strict request model."""
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
            "context_max_bytes": context_max_bytes,
            "freshness_mode": freshness_mode,
        }
    )


PLAN_BUNDLE_SCHEMA = "universal-docs.plan-bundle/v1"
_MAX_DISCOVERY_FILES = 12
_MAX_DISCOVERY_BYTES = 256 * 1024
_SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".rs"}


@dataclass(frozen=True)
class PlanBundle:
    status: str
    reason: str
    plans: tuple[Plan, ...]
    resolution_root_has_manifest: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": PLAN_BUNDLE_SCHEMA,
            "status": self.status,
            "reason": self.reason,
            "plans": [plan.as_dict() for plan in self.plans],
            "resolution_root_has_manifest": self.resolution_root_has_manifest,
        }


def _normalized_package(name: str, ecosystem: str) -> str:
    if ecosystem == "python":
        return canonicalize_python_name(name)
    if ecosystem == "rust":
        return name.replace("_", "-").lower()
    return name.lower()


def _discover_multi_sources(
    root: Path, active_files: Iterable[str | Path]
) -> tuple[list[tuple[Path, bool]], int]:
    selected: list[tuple[Path, bool]] = []
    total = 0
    seen: set[Path] = set()
    try:
        iterator = iter(active_files)
        for _ in range(13):
            try:
                raw = next(iterator)
            except StopIteration:
                break
            rel = Path(raw) if isinstance(raw, (str, Path)) else Path("..")
            if (
                rel.is_absolute()
                or not rel.parts
                or any(p in ("", ".", "..") for p in rel.parts)
                or rel.suffix.lower() not in _SOURCE_SUFFIXES
            ):
                continue
            try:
                data = _read_source_scoped(root, rel)
            except (OSError, ValueError):
                continue
            if (
                rel not in seen
                and len(selected) < _MAX_DISCOVERY_FILES
                and total + len(data) <= _MAX_DISCOVERY_BYTES
            ):
                selected.append((rel, True))
                seen.add(rel)
                total += len(data)
    except Exception:
        pass
    discovered: list[tuple[int, str, Path]] = []
    dirs = 0
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirs += 1
        if dirs > 64:
            break
        dirnames[:] = [
            d
            for d in dirnames
            if d
            not in {
                ".git",
                ".venv",
                "venv",
                "node_modules",
                "target",
                "dist",
                "build",
                "__pycache__",
            }
            and not (Path(directory) / d).is_symlink()
        ]
        for name in filenames:
            path = Path(directory) / name
            if path.suffix.lower() not in _SOURCE_SUFFIXES or path.is_symlink():
                continue
            rel = path.relative_to(root)
            if rel in seen:
                continue
            try:
                discovered.append(
                    (-path.stat(follow_symlinks=False).st_mtime_ns, rel.as_posix(), rel)
                )
            except OSError:
                continue
    for _, _, rel in sorted(discovered):
        if len(selected) >= _MAX_DISCOVERY_FILES or total >= _MAX_DISCOVERY_BYTES:
            break
        try:
            data = _read_source_scoped(root, rel)
        except (OSError, ValueError):
            continue
        if total + len(data) > _MAX_DISCOVERY_BYTES:
            continue
        selected.append((rel, False))
        seen.add(rel)
        total += len(data)
    return selected, total


def _source_imports(path: Path, text: str) -> tuple[set[str], dict[str, set[str]]]:
    imports: set[str] = set()
    symbols: dict[str, set[str]] = {}
    suffix = path.suffix.lower()
    if suffix == ".py":
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError, RecursionError):
            return imports, symbols
        bindings: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for item in node.names:
                    name = item.name.split(".", 1)[0]
                    local = item.asname or name
                    imports.add(name)
                    bindings[local] = name
                    symbols.setdefault(name, set()).add(item.name.rsplit(".", 1)[-1])
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                name = node.module.split(".", 1)[0]
                imports.add(name)
                for item in node.names:
                    if item.name != "*":
                        symbols.setdefault(name, set()).add(item.name)
                        bindings[item.asname or item.name] = name
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                root_name = _attribute_root(node)
                package = bindings.get(root_name or "")
                if package:
                    attribute = node
                    while isinstance(attribute, ast.Attribute):
                        symbols.setdefault(package, set()).add(attribute.attr)
                        attribute = attribute.value
    elif suffix == ".rs":
        for match in re.finditer(r"\buse\s+([A-Za-z_][\w]*)(?:::(.*?))?;", text, re.S):
            name = match.group(1).replace("_", "-").lower()
            imports.add(name)
            symbols.setdefault(name, set()).update(
                re.findall(r"[A-Za-z_][\w]*", match.group(2) or "")
            )
    else:
        pattern = r"\bimport\s+(?:([\s\S]*?)\s+from\s+)?['\"]([^'\"]+)['\"]|\brequire\(\s*['\"]([^'\"]+)['\"]\s*\)"
        for match in re.finditer(pattern, text):
            raw = match.group(2) or match.group(3)
            if raw.startswith((".", "/")):
                continue
            parts = raw.split("/")
            name = (
                "/".join(parts[:2])
                if raw.startswith("@") and len(parts) > 1
                else parts[0]
            )
            imports.add(name)
            symbols.setdefault(name, set()).update(
                re.findall(r"[A-Za-z_$][\w$]*", match.group(1) or "")
            )
    return imports, symbols


def _normalize_proactive_task(task: object) -> str | None:
    """Normalize real chat prompts (multi-line or long) without rejecting newlines."""
    if not isinstance(task, str) or "\x00" in task:
        return None
    cleaned = "".join(c if c in "\n\r\t" or ord(c) >= 32 else " " for c in task[:16384])
    normalized = " ".join(cleaned.split())
    encoded = normalized.encode("utf-8")
    if len(encoded) > 1024:
        normalized = encoded[:1024].decode("utf-8", errors="ignore").strip()
    return normalized


def select_current_packages(
    project_root: Path,
    *,
    task: str | None = None,
    active_files: Iterable[str | Path] = (),
    max_packages: int = 3,
) -> PlanBundle:
    from .lockfile import resolve_project_dependencies

    root = Path(project_root)
    try:
        deps = resolve_project_dependencies(root)
    except (OSError, ValueError):
        return PlanBundle("abstained", "unreadable_project_root", (), False)
    manifest_names = (
        "pyproject.toml",
        "requirements.txt",
        "package.json",
        "Cargo.toml",
    )
    has_manifest = bool(deps) or any((root / n).is_file() for n in manifest_names)
    if not has_manifest:
        reason = (
            "unsupported_manifest"
            if any((root / n).exists() for n in _FOREIGN_MANIFESTS)
            else "no_manifest_found"
        )
        return PlanBundle("abstained", reason, (), False)
    task_value = _normalize_proactive_task(task) if task is not None else ""
    if task is not None and task_value is None:
        return PlanBundle("abstained", "task_signal_invalid", (), True)
    sources, _ = _discover_multi_sources(root, active_files)
    task_lower = task_value.lower()
    scores: dict[tuple[str, str], int] = {}
    found_symbols: dict[tuple[str, str], set[str]] = {}
    imported_sources: dict[str, set[str]] = {}
    for rel, is_active in sources:
        try:
            raw = _read_source_scoped(root, rel).decode("utf-8")
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        imported, symbols = _source_imports(rel, raw)
        for dep in deps:
            key = _normalized_package(dep.name, dep.ecosystem)
            aliases = {key, dep.name.lower(), dep.name.lower().replace("-", "_")}
            if any(i.lower() in aliases for i in imported):
                imported_sources.setdefault(key, set()).add(
                    "active" if is_active else "discovered"
                )
                found_symbols.setdefault((dep.ecosystem, key), set()).update(
                    s for i, ss in symbols.items() if i.lower() in aliases for s in ss
                )
    for dep in deps:
        key = _normalized_package(dep.name, dep.ecosystem)
        named = bool(
            re.search(
                r"(?<![\w@])" + re.escape(dep.name) + r"(?![\w])", task_lower, re.I
            )
            or (
                dep.ecosystem == "python"
                and "-" in dep.name
                and re.search(
                    r"(?<![\w@])" + re.escape(dep.name.replace("-", "_")) + r"(?![\w])",
                    task_lower,
                    re.I,
                )
            )
        )
        provenance = imported_sources.get(key, set())
        if not dep.direct and not (named or "active" in provenance):
            continue
        score = 10 if named else 0
        score += 6 if "active" in provenance else 0
        score += 4 if "discovered" in provenance else 0
        symbols = found_symbols.get((dep.ecosystem, key), set())
        score += 2 * sum(
            1
            for symbol in symbols
            if re.search(r"\b" + re.escape(symbol) + r"\b", task_value, re.I)
        )
        if score and dep.resolved_version and dep.registry_lookup:
            scores[(dep.ecosystem, key)] = score
    limit = max(1, min(3, int(max_packages)))
    ranked = sorted(scores, key=lambda k: (-scores[k], k[1]))[:limit]
    if not ranked:
        reason = (
            "task_package_not_locally_proven"
            if task_value
            else "no_exact_registry_candidate"
        )
        return PlanBundle("abstained", reason, (), True)
    by_key = {(d.ecosystem, _normalized_package(d.name, d.ecosystem)): d for d in deps}
    plans = []
    for ecosystem, key in ranked:
        dep = by_key[(ecosystem, key)]
        display = dep.name
        query = " ".join(
            [
                display,
                dep.resolved_version or "",
                task_value or _QUERY,
                *sorted(found_symbols.get((ecosystem, key), set())),
            ]
        ).strip()[:512]
        plans.append(
            Plan(
                "selected",
                "current_exact_pin",
                display,
                ecosystem,
                None,
                dep.resolved_version,
                dep.resolution_source,
                query,
                {
                    "mode": "source_backed"
                    if key in imported_sources
                    else "task_named",
                    "symbols": sorted(found_symbols.get((ecosystem, key), set()))[:24],
                    "source_files_inspected": len(sources),
                },
            )
        )
    return PlanBundle("selected", "packages_selected", tuple(plans), True)


def select_for_error(
    project_root: Path,
    *,
    error_text: str,
    active_files: Iterable[str | Path] = (),
    max_packages: int = 2,
) -> PlanBundle:
    from .lockfile import resolve_project_dependencies

    if not isinstance(error_text, str):
        return PlanBundle("abstained", "error_text_invalid", (), False)
    error_text = "".join(
        c for c in error_text[: 16 * 1024] if c in "\n\t" or ord(c) >= 32
    )
    try:
        deps = resolve_project_dependencies(Path(project_root))
    except (OSError, ValueError):
        return PlanBundle("abstained", "unreadable_project_root", (), False)
    mentions = set()
    for pattern in (
        r"no module named ['\"]?([A-Za-z0-9_.-]+)['\"]?",
        r"cannot import name ['\"]?([A-Za-z0-9_]+)['\"]? from ['\"]?([A-Za-z0-9_.-]+)['\"]?",
        r"cannot find module ['\"]?(@[^/'\"\s]+/[^/'\"\s]+|[^/'\"\s]+)['\"]?",
        r"(?:site-packages|node_modules)/(@[^/\s]+/[^/\s]+|[^/\s]+)",
        r"module ['\"]([A-Za-z0-9_.-]+)['\"] has no attribute",
    ):
        for match in re.finditer(pattern, error_text, re.I):
            value = (
                match.group(2)
                if match.lastindex and match.lastindex > 1 and match.group(2)
                else match.group(1)
            )
            cleaned = value.strip("'\"")
            if cleaned.startswith("@"):
                mentions.add(cleaned.lower())
            else:
                mentions.add(cleaned.split(".", 1)[0].lower())
    symbols = set(
        re.findall(
            r"has no attribute ['\"]?([\w]+)['\"]?|unexpected keyword argument ['\"]?([\w]+)['\"]?|cannot import name ['\"]?([\w]+)['\"]?",
            error_text,
            re.I,
        )
    )
    symbol_terms = {part for tup in symbols for part in tup if part}
    error_class = re.search(
        r"\b(ImportError|ModuleNotFoundError|AttributeError|TypeError|DeprecationWarning|ValidationError|ERR_MODULE_NOT_FOUND)\b|\b(error\[E\d+\])",
        error_text,
        re.I,
    )
    error_name = error_class.group(0) if error_class else "runtime error"
    matches = []
    for dep in deps:
        key = _normalized_package(dep.name, dep.ecosystem)
        aliases = {key, dep.name.lower(), dep.name.lower().replace("-", "_")}
        if (
            dep.resolved_version
            and dep.registry_lookup
            and any(m in aliases or m.replace("_", "-") in aliases for m in mentions)
        ):
            matches.append((dep, key))
    matches = matches[: max(1, min(2, int(max_packages)))]
    if not matches:
        return PlanBundle(
            "abstained",
            "task_package_not_locally_proven",
            (),
            any(
                (Path(project_root) / n).is_file()
                for n in (
                    "pyproject.toml",
                    "requirements.txt",
                    "package.json",
                    "Cargo.toml",
                )
            ),
        )
    plans = tuple(
        Plan(
            "selected",
            "error_package_matched",
            dep.name,
            dep.ecosystem,
            None,
            dep.resolved_version,
            dep.resolution_source,
            " ".join(
                [
                    dep.name,
                    dep.resolved_version or "",
                    error_name,
                    *sorted(symbol_terms),
                ]
            )[:512],
            {"mode": "error", "symbols": sorted(symbol_terms)[:24]},
        )
        for dep, _ in matches
    )
    return PlanBundle("selected", "error_package_matched", plans, True)


def to_preflight_requests(
    bundle: PlanBundle,
    *,
    total_budget_bytes: int = 6144,
    freshness_mode: Literal[
        "require_check", "allow_cache", "allow_stale"
    ] = "allow_cache",
) -> list[PreflightRequest]:
    if bundle.status != "selected" or not bundle.plans:
        return []
    if total_budget_bytes < 1500 * len(bundle.plans):
        raise ValueError("budget_too_small")
    weights = [max(1, len((p.query or "").split())) for p in bundle.plans]
    remaining = total_budget_bytes - 1500 * len(bundle.plans)
    total_weight = sum(weights)
    budgets = [1500 + remaining * w // total_weight for w in weights]
    budgets[-1] += total_budget_bytes - sum(budgets)
    return [
        PreflightRequest.model_validate(
            {
                "package": p.package,
                "ecosystem": p.ecosystem,
                "selection": "requested",
                "requested_version": p.target_version,
                "query": p.query or _QUERY,
                "section_ids": [],
                "context_max_bytes": budgets[i],
                "freshness_mode": freshness_mode,
            }
        )
        for i, p in enumerate(bundle.plans)
    ]


def _plan_from_bundle(plan: Plan) -> Plan:
    """Project a one-target bundle back onto the legacy singular contract."""
    return Plan(
        plan.status,
        plan.reason,
        plan.package,
        plan.ecosystem,
        plan.previous_version,
        plan.target_version,
        plan.resolution_source,
        plan.query,
        plan.selection,
    )
