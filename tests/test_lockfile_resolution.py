"""Lockfile and installed-version resolution stays read-only and fail-closed."""

import json

from universal_docs_mcp.lockfile import (
    parse_package_lock_json,
    parse_pnpm_lock_yaml,
    parse_poetry_lock,
    parse_uv_lock,
    read_installed_pins,
    resolve_project_dependencies,
)


def test_lockfile_parsers_extract_and_redact_public_versions():
    uv = parse_uv_lock(
        """[[package]]\nname="demo"\nversion="1.2.3"\nsource={ registry="https://pypi.org/simple" }\n[[package]]\nname="private"\nversion="2.0.0"\nsource={ registry="https://private.example/simple" }\n""",
        "uv.lock",
    )
    assert [(p.name, p.pinned, p.registry_lookup) for p in uv] == [
        ("demo", "1.2.3", True),
        ("private", None, False),
    ]
    poetry = parse_poetry_lock(
        """[[package]]\nname="demo"\nversion="1.2.3"\n[[package]]\nname="gitdep"\nversion="2.0.0"\nsource={type="git", url="https://private.example/x"}\n""",
        "poetry.lock",
    )
    assert [(p.name, p.pinned, p.registry_lookup) for p in poetry] == [
        ("demo", "1.2.3", True),
        ("gitdep", None, False),
    ]
    npm = parse_package_lock_json(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {},
                    "node_modules/demo": {"version": "1.2.3"},
                    "node_modules/@types/node": {
                        "version": "20.1.0",
                        "resolved": "https://registry.npmjs.org/@types/node/-/node-20.1.0.tgz",
                    },
                    "node_modules/local": {"version": "1.0.0", "link": True},
                },
            }
        ),
        "package-lock.json",
    )
    assert [(p.name, p.pinned, p.registry_lookup) for p in npm] == [
        ("demo", "1.2.3", True),
        ("@types/node", "20.1.0", True),
        ("local", None, False),
    ]


def test_pnpm_v6_and_v9_parse_exact_registry_versions():
    v6 = parse_pnpm_lock_yaml(
        "lockfileVersion: 6.0\npackages:\n  /demo@1.2.3:\n    resolution: {integrity: sha512-x}\n  /@types/node@20.1.0:\n    resolution: {integrity: sha512-y}\n  /gitdep@github.com/x:\n    resolution: {tarball: https://example.test/x}\n",
        "pnpm-lock.yaml",
    )
    assert [(p.name, p.pinned, p.registry_lookup) for p in v6] == [
        ("demo", "1.2.3", True),
        ("@types/node", "20.1.0", True),
        ("gitdep", None, False),
    ]
    v9 = parse_pnpm_lock_yaml(
        "lockfileVersion: '9.0'\nimporters:\n  .:\n    dependencies:\n      demo:\n        version: 1.2.3\npackages:\n  demo@1.2.3:\n    resolution: {integrity: sha512-x}\nsnapshots:\n  demo@1.2.3: {}\n",
        "pnpm-lock.yaml",
    )
    assert [(p.name, p.pinned) for p in v9] == [("demo", "1.2.3")]


def test_installed_metadata_and_resolver_use_exact_versions(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies=["demo>=1.0"]\n')
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname="demo"\nversion="1.2.3"\nsource={ registry="https://pypi.org/simple" }\n'
    )
    metadata = tmp_path / ".venv/lib/python3.12/site-packages/demo-1.2.3.dist-info"
    metadata.mkdir(parents=True)
    (metadata / "METADATA").write_text("Name: demo\nVersion: 1.2.4\n\n")
    assert read_installed_pins(tmp_path)[0].pinned == "1.2.4"
    dep = resolve_project_dependencies(tmp_path)[0]
    assert (dep.name, dep.resolved_version, dep.resolution_source, dep.direct) == (
        "demo",
        "1.2.3",
        "uv.lock",
        True,
    )


def test_empty_and_conflicting_direct_pins(tmp_path):
    assert resolve_project_dependencies(tmp_path) == []
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies=["demo==1.2.3"]\n'
    )
    (tmp_path / "requirements.txt").write_text("demo==1.2.4\n")
    dep = resolve_project_dependencies(tmp_path)[0]
    assert dep.resolved_version is None


def test_installed_metadata_enforces_symlinks_and_byte_bound(tmp_path):
    from universal_docs_mcp.lockfile import MAX_LOCKFILE_BYTES

    root = tmp_path
    site = root / ".venv/lib/python3.12/site-packages"
    item = site / "demo-1.0.dist-info"
    item.mkdir(parents=True)
    (item / "METADATA").write_text("Name: demo\nVersion: 1.0\n\n" + "x" * 70000)
    assert read_installed_pins(root, ecosystems=("python",))[0].name == "demo"
    (item / "METADATA").write_text("Name: demo\nVersion: 1.0.0\n\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    link_root = tmp_path / "linked"
    link_root.mkdir()
    (link_root / ".venv").symlink_to(outside, target_is_directory=True)
    assert not read_installed_pins(link_root, ecosystems=("python",))
    assert MAX_LOCKFILE_BYTES == 8 * 1024 * 1024


def test_installed_javascript_metadata_reads_scopes_and_skips_links(tmp_path):
    import json

    root = tmp_path
    modules = root / "node_modules"
    (modules / "demo").mkdir(parents=True)
    (modules / "demo/package.json").write_text(
        json.dumps({"name": "demo", "version": "1.2.3"})
    )
    (modules / "@types/node").mkdir(parents=True)
    (modules / "@types/node/package.json").write_text(
        json.dumps({"name": "@types/node", "version": "20.1.0"})
    )
    outside = tmp_path / "external"
    outside.mkdir()
    (outside / "package.json").write_text(
        json.dumps({"name": "linked", "version": "1.0.0"})
    )
    (modules / "linked").symlink_to(outside, target_is_directory=True)
    pins = read_installed_pins(root, ecosystems=("javascript",))
    assert {(p.name, p.pinned) for p in pins} == {
        ("demo", "1.2.3"),
        ("@types/node", "20.1.0"),
    }


def test_resolver_uses_installed_and_ecosystem_lockfiles(tmp_path):
    import json

    from universal_docs_mcp.lockfile import parse_cargo_lock

    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies=["demo>=1.0"]\n')
    metadata = tmp_path / ".venv/lib/python3.12/site-packages/demo-1.2.3.dist-info"
    metadata.mkdir(parents=True)
    (metadata / "METADATA").write_text("Name: demo\nVersion: 1.2.3\n\n")
    dep = resolve_project_dependencies(tmp_path)[0]
    assert (dep.resolved_version, dep.resolution_source, dep.direct) == (
        "1.2.3",
        "venv_metadata",
        True,
    )

    jsroot = tmp_path / "js"
    jsroot.mkdir()
    (jsroot / "package.json").write_text(
        json.dumps({"dependencies": {"next": "^15.1.0"}})
    )
    (jsroot / "package-lock.json").write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {"node_modules/next": {"version": "15.1.6"}},
            }
        )
    )
    dep = resolve_project_dependencies(jsroot)[0]
    assert (dep.resolved_version, dep.resolution_source, dep.direct) == (
        "15.1.6",
        "package-lock.json",
        True,
    )

    rustroot = tmp_path / "rust"
    rustroot.mkdir()
    (rustroot / "Cargo.toml").write_text('[dependencies]\nserde = "1.0"\n')
    (rustroot / "Cargo.lock").write_text(
        '[[package]]\nname="serde"\nversion="1.0.215"\nsource="registry+https://github.com/rust-lang/crates.io-index"\n'
    )
    dep = resolve_project_dependencies(rustroot)[0]
    assert (dep.resolved_version, dep.resolution_source, dep.direct) == (
        "1.0.215",
        "Cargo.lock",
        True,
    )
    assert (
        parse_cargo_lock((rustroot / "Cargo.lock").read_text(), "Cargo.lock")[0].pinned
        == "1.0.215"
    )


def test_resolver_handles_poetry_lock_without_manifest(tmp_path):
    (tmp_path / "poetry.lock").write_text('[[package]]\nname="demo"\nversion="1.2.3"\n')
    dep = resolve_project_dependencies(tmp_path)[0]
    assert (dep.name, dep.resolved_version, dep.direct) == ("demo", "1.2.3", False)


def test_invalid_root_is_safe_error(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="^unreadable_project_root$"):
        resolve_project_dependencies(tmp_path / "missing")


def test_pep440_versions_pep503_names_and_nested_node_modules(tmp_path):
    import json

    from universal_docs_mcp.lockfile import parse_package_lock_json, parse_uv_lock

    uv_pins = parse_uv_lock(
        '[[package]]\nname="packaging"\nversion="26.3"\nsource={registry="https://pypi.org/simple"}\n'
        '[[package]]\nname="pydantic-core"\nversion="2.27.1.post1"\nsource={registry="https://pypi.org/simple"}\n',
        "uv.lock",
    )
    assert {(p.name, p.pinned) for p in uv_pins} == {
        ("packaging", "26.3"),
        ("pydantic-core", "2.27.1.post1"),
    }

    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies=["pydantic_core>=2.0"]\n'
    )
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname="pydantic-core"\nversion="2.27.1"\nsource={registry="https://pypi.org/simple"}\n'
        '[[package]]\nname="starlette"\nversion="0.41.3"\nsource={registry="https://pypi.org/simple"}\n'
    )
    resolved = {d.name.lower(): d for d in resolve_project_dependencies(tmp_path)}
    assert resolved["pydantic_core"].resolved_version == "2.27.1"
    assert resolved["pydantic_core"].direct is True
    assert resolved["starlette"].resolved_version == "0.41.3"
    assert resolved["starlette"].direct is False

    npm_pins = parse_package_lock_json(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "node_modules/next": {"version": "15.1.6"},
                    "node_modules/next/node_modules/nested-pkg": {"version": "1.0.0"},
                },
            }
        ),
        "package-lock.json",
    )
    assert [(p.name, p.pinned) for p in npm_pins] == [("next", "15.1.6")]
