"""Multi-ecosystem proactive planner behavior (offline)."""

from __future__ import annotations

import json
from pathlib import Path

from universal_docs_mcp.planner import (
    Plan,
    select_current_package,
    select_current_packages,
    select_for_error,
    to_preflight_request,
    to_preflight_requests,
)


def _python_project(root: Path) -> Path:
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname="demo"\ndependencies=["pydantic>=2.0", "httpx>=0.27"]\n'
    )
    (root / "uv.lock").write_text(
        'version = 1\n[[package]]\nname = "pydantic"\nversion = "2.9.2"\nsource = { registry = "https://pypi.org/simple" }\n'
        '[[package]]\nname = "httpx"\nversion = "0.27.2"\nsource = { registry = "https://pypi.org/simple" }\n'
    )
    return root


def test_selects_multiple_locked_python_packages_and_splits_budget(tmp_path):
    root = _python_project(tmp_path / "python")
    (root / "app.py").write_text("import pydantic\nimport httpx\n")
    bundle = select_current_packages(root, task="fix pydantic and httpx")
    assert bundle.status == "selected"
    assert {p.package: p.target_version for p in bundle.plans} == {
        "pydantic": "2.9.2",
        "httpx": "0.27.2",
    }
    requests = to_preflight_requests(bundle)
    assert sum(r.context_max_bytes for r in requests) <= 6144
    assert all(r.freshness_mode == "allow_cache" for r in requests)
    assert (
        json.loads(json.dumps(bundle.as_dict()))["schema"]
        == "universal-docs.plan-bundle/v1"
    )


def test_selects_javascript_imports_and_symbols(tmp_path):
    root = tmp_path / "js"
    root.mkdir()
    (root / "package.json").write_text(
        '{"dependencies":{"zod":"^3.0.0","next":"^14.0.0"}}'
    )
    (root / "package-lock.json").write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"dependencies": {"zod": "^3.0.0", "next": "^14.0.0"}},
                    "node_modules/zod": {"version": "3.23.8"},
                    "node_modules/next": {"version": "14.2.5"},
                },
            }
        )
    )
    (root / "app.ts").write_text(
        'import { z } from "zod"; import { useRouter } from "next/navigation";'
    )
    bundle = select_current_packages(root, task="fix zod and next router")
    assert {p.package: p.target_version for p in bundle.plans} == {
        "zod": "3.23.8",
        "next": "14.2.5",
    }
    assert any("z" in p.selection["symbols"] for p in bundle.plans)
    assert any("useRouter" in p.selection["symbols"] for p in bundle.plans)


def test_selects_rust_import_with_lock_version(tmp_path):
    root = tmp_path / "rust"
    (root / "src").mkdir(parents=True)
    (root / "Cargo.toml").write_text(
        '[package]\nname="demo"\nversion="0.1.0"\n[dependencies]\nserde="1"\n'
    )
    (root / "Cargo.lock").write_text(
        'version = 3\n[[package]]\nname = "serde"\nversion = "1.0.210"\nsource = "registry+https://github.com/rust-lang/crates.io-index"\n'
    )
    (root / "src/main.rs").write_text("use serde::Deserialize;")
    bundle = select_current_packages(root)
    assert [(p.package, p.target_version) for p in bundle.plans] == [
        ("serde", "1.0.210")
    ]
    assert "Deserialize" in bundle.plans[0].selection["symbols"]


def test_recently_modified_file_beats_alphabetical_init_and_twelve_file_cap(tmp_path):
    root = _python_project(tmp_path / "recent")
    for index in range(14):
        (root / f"{index:02}.py").write_text("pass\n")
    (root / "00.py").write_text("import pydantic\npydantic.BaseModel\n")
    bundle = select_current_packages(root)
    assert bundle.status == "selected"
    assert bundle.plans[0].package == "pydantic"
    assert "BaseModel" in bundle.plans[0].selection["symbols"]


def test_error_selector_uses_proven_package_and_symbol(tmp_path):
    root = _python_project(tmp_path / "error")
    bundle = select_for_error(
        root, error_text="ImportError: cannot import name 'validator' from 'pydantic'"
    )
    assert bundle.status == "selected"
    assert bundle.plans[0].package == "pydantic"
    assert "ImportError" in bundle.plans[0].query
    assert "validator" in bundle.plans[0].query


def test_error_selector_matches_javascript_node_modules_path(tmp_path):
    root = tmp_path / "js-error"
    root.mkdir()
    (root / "package.json").write_text('{"dependencies":{"zod":"^3.0.0"}}')
    (root / "package-lock.json").write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"dependencies": {"zod": "^3.0.0"}},
                    "node_modules/zod": {"version": "3.23.8"},
                },
            }
        )
    )
    bundle = select_for_error(
        root, error_text="TypeError at node_modules/zod/lib/index.js: invalid input"
    )
    assert bundle.status == "selected" and bundle.plans[0].package == "zod"


def test_error_selector_never_invents_unknown_package(tmp_path):
    root = _python_project(tmp_path / "unknown")
    bundle = select_for_error(root, error_text="ImportError: missing frobnicator")
    assert bundle.status == "abstained"


def test_singular_request_defaults_to_cache_but_can_require_freshness():
    plan = Plan(
        "selected", "current_exact_pin", "demo", "python", None, "1.2.3", "uv.lock"
    )
    assert to_preflight_request(plan).freshness_mode == "require_check"
    assert (
        to_preflight_request(plan, freshness_mode="require_check").freshness_mode
        == "require_check"
    )


def test_legacy_selector_uses_locked_python_and_javascript_resolution(tmp_path):
    python_root = _python_project(tmp_path / "singular-python")
    (python_root / "app.py").write_text("import pydantic\n")
    python_plan = select_current_package(
        python_root, task="fix pydantic", source_paths=("app.py",)
    )
    assert (python_plan.status, python_plan.package, python_plan.target_version) == (
        "selected",
        "pydantic",
        "2.9.2",
    )

    js_root = tmp_path / "singular-js"
    js_root.mkdir()
    (js_root / "package.json").write_text('{"dependencies":{"zod":"^3.0.0"}}')
    (js_root / "package-lock.json").write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"dependencies": {"zod": "^3.0.0"}},
                    "node_modules/zod": {"version": "3.23.8"},
                },
            }
        )
    )
    (js_root / "app.ts").write_text('import { z } from "zod";')
    js_plan = select_current_package(js_root, source_paths=("app.ts",))
    assert (js_plan.status, js_plan.package, js_plan.target_version) == (
        "selected",
        "zod",
        "3.23.8",
    )


def test_multiline_long_prompt_and_unquoted_import_error_and_scoped_npm(tmp_path):
    root = _python_project(tmp_path / "multiline")
    (root / "app.py").write_text("import pydantic\n")
    long_prompt = "Line 1: fix pydantic validator\n" + ("context " * 120)
    bundle = select_current_packages(root, task=long_prompt)
    assert bundle.status == "selected"
    assert bundle.plans[0].package == "pydantic"

    err_bundle = select_for_error(
        root,
        error_text="ImportError: cannot import name validator from pydantic.main",
    )
    assert err_bundle.status == "selected"
    assert err_bundle.plans[0].package == "pydantic"
    assert "validator" in (err_bundle.plans[0].query or "")

    scoped = tmp_path / "scoped"
    scoped.mkdir()
    (scoped / "package.json").write_text('{"dependencies":{"@types/node":"^20.0.0"}}')
    (scoped / "package-lock.json").write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "node_modules/@types/node": {"version": "20.11.0"},
                },
            }
        )
    )
    scoped_err = select_for_error(
        scoped, error_text="TypeError in node_modules/@types/node/fs.d.ts"
    )
    assert scoped_err.status == "selected"
    assert scoped_err.plans[0].package == "@types/node"
