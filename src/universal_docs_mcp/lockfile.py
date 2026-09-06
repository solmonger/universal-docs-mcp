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
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_REQ_LINE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?P<spec>[=<>!~]=?[^;#]*)?"
)
_EXACT = re.compile(r"^={1,3}\s*(?P<ver>[A-Za-z0-9][A-Za-z0-9.*+-]*)$")


@dataclass
class Pin:
    name: str
    ecosystem: str
    spec: str
    pinned: Optional[str]  # exact version when determinable, else None
    source: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ecosystem": self.ecosystem,
            "spec": self.spec,
            "pinned": self.pinned,
            "source": self.source,
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


def parse_requirements(text: str, source: str) -> list[Pin]:
    pins = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = _REQ_LINE.match(line)
        if not m:
            continue
        spec = (m.group("spec") or "").strip()
        pins.append(
            Pin(
                name=m.group("name"),
                ecosystem="python",
                spec=spec,
                pinned=_exact_from_spec(spec),
                source=source,
            )
        )
    return pins


def parse_pyproject(text: str, source: str) -> list[Pin]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    deps = data.get("project", {}).get("dependencies", []) or []
    pins = []
    for dep in deps:
        m = _REQ_LINE.match(dep.strip())
        if not m:
            continue
        spec = (m.group("spec") or "").strip()
        pins.append(
            Pin(
                name=m.group("name"),
                ecosystem="python",
                spec=spec,
                pinned=_exact_from_spec(spec),
                source=source,
            )
        )
    return pins


def parse_package_json(text: str, source: str) -> list[Pin]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    pins = []
    for group in ("dependencies", "devDependencies"):
        for name, spec in (data.get(group) or {}).items():
            pins.append(
                Pin(
                    name=name,
                    ecosystem="javascript",
                    spec=str(spec),
                    pinned=_exact_from_spec(str(spec)),
                    source=source,
                )
            )
    return pins


def parse_cargo_toml(text: str, source: str) -> list[Pin]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    pins = []
    for group in ("dependencies", "dev-dependencies"):
        for name, val in (data.get(group) or {}).items():
            if isinstance(val, str):
                spec = val
            elif isinstance(val, dict):
                spec = str(val.get("version", ""))
            else:
                continue
            pins.append(
                Pin(
                    name=name,
                    ecosystem="rust",
                    spec=spec,
                    pinned=_exact_from_spec(spec),
                    source=source,
                )
            )
    return pins


def parse_cargo_lock(text: str, source: str) -> list[Pin]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    pins = []
    for pkg in data.get("package", []) or []:
        name = pkg.get("name")
        version = pkg.get("version")
        if not name or not version:
            continue
        pins.append(
            Pin(
                name=name,
                ecosystem="rust",
                spec=f"={version}",
                pinned=version,
                source=source,
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
    return parser(p.read_text(encoding="utf-8"), str(p))
