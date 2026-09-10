"""Lockfile / manifest parsing so docs can be fetched at *pinned* versions.

An agent working inside a repo does not care about "latest stable"; it cares
about the exact version the project is pinned to. This module reads the common
manifests and reports, per dependency: the declared spec, an exact pin when the
spec is one, and (optionally, via the registries) the current latest stable so
staleness is visible.

Supported manifests:
  - requirements*.txt / constraints*.txt   (Python, PEP 508 lines)
  - pyproject.toml                         (Python, [project].dependencies)
  - package.json                           (JS/TS, dependencies + devDependencies)
  - Cargo.toml                             (Rust, [dependencies] / [dev-dependencies])
  - Cargo.lock                             (Rust, exact [[package]] versions)
"""

from __future__ import annotations

import json
import re
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from packaging.requirements import Requirement, InvalidRequirement

_SEMVER = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
MAX_MANIFEST_BYTES = 1024 * 1024
_EXACT = re.compile(r"^={1,3}\s*(?P<ver>[A-Za-z0-9][A-Za-z0-9.+-]*)$")


@dataclass
class Pin:
    name: str
    ecosystem: str
    spec: str
    pinned: Optional[str]  # exact version when determinable, else None
    source: str
    spec_redacted: bool = False
    registry_lookup: bool = True
    extras: list[str] = field(default_factory=list)
    marker: Optional[str] = None

    def __post_init__(self):
        # Omit whole non-version references, not just familiar token query keys.
        # URL paths, fragments, encoded userinfo and local paths can all be private.
        if not self.registry_lookup or not re.fullmatch(r"[0-9.*xX<>=!~^, |+\-a-zA-Z]*", self.spec) or (
            self.spec and not re.search(r"[0-9*]", self.spec)
        ):
            self.spec = "[redacted]"
            self.pinned = None
            self.spec_redacted = True
            self.registry_lookup = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ecosystem": self.ecosystem,
            "spec": self.spec,
            "pinned": self.pinned,
            "source": self.source,
            "spec_redacted": self.spec_redacted,
            "registry_lookup": self.registry_lookup,
            "extras": self.extras,
            "marker": self.marker,
        }


def _exact_from_spec(spec: str) -> Optional[str]:
    spec = (spec or "").strip().rstrip(",").strip()
    if not spec:
        return None
    m = _EXACT.match(spec)
    if m:
        return m.group("ver").strip()
    # bare version (Cargo.lock style / package.json exact)
    if re.fullmatch(r"[0-9]+(\.[0-9]+)*([+-][A-Za-z0-9.+-]+)?", spec):
        return spec
    return None


def _mapping(value) -> dict:
    if not isinstance(value, dict):
        raise ValueError("invalid_manifest_structure")
    return value


def _strings(value) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("invalid_dependency_list")
    return value


def _requirement(line: str, source: str) -> Pin:
    try:
        req = Requirement(line)
    except InvalidRequirement:
        raise ValueError("invalid_or_unsupported_requirement") from None
    spec = req.url if req.url is not None else str(req.specifier)
    return Pin(req.name, "python", spec, _exact_from_spec(spec), source,
               registry_lookup=req.url is None, extras=sorted(req.extras),
               marker=str(req.marker) if req.marker else None)


def parse_requirements(text: str, source: str) -> list[Pin]:
    pins = []
    for raw in text.splitlines():
        line = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
        if not line or line.startswith("#"):
            continue
        pins.append(_requirement(line, source))
    return pins


def parse_pyproject(text: str, source: str) -> list[Pin]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise ValueError("invalid_toml") from None
    deps = _mapping(data.get("project", {})).get("dependencies", [])
    return [_requirement(dep, source) for dep in _strings(deps)]


def parse_package_json(text: str, source: str) -> list[Pin]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError("invalid_json") from None
    data = _mapping(data)
    pins = []
    for group in ("dependencies", "devDependencies"):
        for name, spec in _mapping(data.get(group, {})).items():
            if not isinstance(spec, str):
                raise ValueError("invalid_dependency_spec")
            pins.append(
                Pin(
                    name=name,
                    ecosystem="javascript",
                    spec=str(spec),
                    pinned=spec if re.fullmatch(_SEMVER, spec) else None,
                    source=source,
                )
            )
    return pins


def parse_cargo_toml(text: str, source: str) -> list[Pin]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise ValueError("invalid_toml") from None
    pins = []
    for group in ("dependencies", "dev-dependencies"):
        for name, val in _mapping(data.get(group, {})).items():
            registry_lookup = True
            if isinstance(val, str):
                spec = val
            elif isinstance(val, dict):
                spec = val.get("version", "")
                name = val.get("package", name)
                registry_lookup = not any(key in val for key in ("git", "path", "registry", "workspace"))
            else:
                raise ValueError("invalid_dependency_spec")
            if not isinstance(spec, str) or not isinstance(name, str):
                raise ValueError("invalid_dependency_spec")
            pins.append(
                Pin(
                    name=name,
                    ecosystem="rust",
                    spec=spec,
                    pinned=spec[1:].strip() if re.fullmatch(r"=\s*" + _SEMVER, spec) else None,
                    source=source,
                    registry_lookup=registry_lookup,
                )
            )
    return pins


def parse_cargo_lock(text: str, source: str) -> list[Pin]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise ValueError("invalid_toml") from None
    pins = []
    packages = data.get("package", [])
    if not isinstance(packages, list):
        raise ValueError("invalid_manifest_structure")
    for pkg in packages:
        pkg = _mapping(pkg)
        name = pkg.get("name")
        version = pkg.get("version")
        if not isinstance(name, str) or not isinstance(version, str) or not re.fullmatch(_SEMVER, version):
            raise ValueError("invalid_locked_package")
        registry_lookup = pkg.get("source") == "registry+https://github.com/rust-lang/crates.io-index"
        pins.append(
            Pin(
                name=name,
                ecosystem="rust",
                spec=f"={version}",
                pinned=version,
                source=source,
                registry_lookup=registry_lookup,
            )
        )
    return pins


_PARSERS = {
    "requirements.txt": parse_requirements,
    "requirements": parse_requirements,
    "constraints.txt": parse_requirements,
    "pyproject.toml": parse_pyproject,
    "package.json": parse_package_json,
    "cargo.toml": parse_cargo_toml,
    "cargo.lock": parse_cargo_lock,
}


def manifest_kind(path: Path) -> Optional[str]:
    name = path.name.lower()
    if name in _PARSERS:
        return name
    if name.startswith("requirements") and name.endswith(".txt"):
        return "requirements"
    if name.startswith("constraints") and name.endswith(".txt"):
        return "constraints.txt"
    return None


def read_pins(path: str | Path) -> list[Pin]:
    p = Path(path)
    kind = manifest_kind(p)
    if kind is None:
        raise ValueError(
            f"unsupported manifest: {p.name} "
            "(expected requirements*.txt, pyproject.toml, package.json, Cargo.toml, Cargo.lock)"
        )
    parser = _PARSERS[kind]
    with p.open("rb") as stream:
        data = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(data) > MAX_MANIFEST_BYTES:
        raise ValueError("manifest_too_large")
    return parser(data.decode("utf-8"), kind)
