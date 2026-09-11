from __future__ import annotations

import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from universal_docs_mcp import product_cli

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"


def _cache_call(path: Path, monkeypatch):
    monkeypatch.setenv("UNIVERSAL_DOCS_CACHE_DIR", str(path))
    return product_cli._doctor_cache(Path("/unused"))


def test_cache_classification_is_topology_only_and_noncreating(tmp_path, monkeypatch):
    missing = tmp_path / "missing"
    info, status = _cache_call(missing, monkeypatch)
    assert status == "pass" and info["root_status"] == "missing"
    assert not missing.exists()

    empty = tmp_path / "empty"
    empty.mkdir()
    info, status = _cache_call(empty, monkeypatch)
    assert status == "pass" and info["entries"]["total"] == 0

    (empty / "cache.db").write_bytes(b"source body must not be read")
    (empty / "directory").mkdir()
    (empty / "link").symlink_to(empty / "cache.db")
    if hasattr(os, "mkfifo"):
        os.mkfifo(empty / "fifo")
    info, status = _cache_call(empty, monkeypatch)
    assert status == "pass"
    assert info["entries"] == {
        "validity": "topology_only", "total": 4, "regular": 1,
        "symlink": 1, "directory": 1, "special": 1,
        "unreadable": 0, "oversized_names": 0, "names_truncated": False,
    }
    assert info["owner"] == "DocsCache"
    assert info["provenance"] == "fixture_override"
    assert "source body must not be read" not in json.dumps(info)


def test_cache_root_types_fail_closed_without_writes(tmp_path, monkeypatch):
    regular = tmp_path / "regular"
    regular.write_bytes(b"not a cache")
    assert _cache_call(regular, monkeypatch)[1] == "fail"
    link = tmp_path / "root-link"
    link.symlink_to(regular)
    assert _cache_call(link, monkeypatch)[1] == "fail"
    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "root-fifo"
        os.mkfifo(fifo)
        assert _cache_call(fifo, monkeypatch)[1] == "fail"


def test_default_identity_is_owner_derived_and_fixture_is_labeled(tmp_path, monkeypatch):
    monkeypatch.delenv("UNIVERSAL_DOCS_CACHE_DIR", raising=False)
    monkeypatch.setattr(product_cli, "DEFAULT_CACHE_DIR", tmp_path / "default")
    info, status = product_cli._doctor_cache(tmp_path)
    assert status == "pass" and info["root_status"] == "missing"
    assert info["scope"] == "default_user_installation"
    assert info["provenance"] == "production_default"
    assert info["path_semantics"] == "~/.cache/universal-docs-mcp"
    assert str(tmp_path) not in json.dumps(info)


def test_first_run_real_hook_and_preflight_own_cache_write(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    cache = home / ".cache" / "universal-docs-mcp"
    monkeypatch.delenv("UNIVERSAL_DOCS_CACHE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(product_cli, "DEFAULT_CACHE_DIR", cache)

    hook = tmp_path / "universal-docs-command-hook"
    hook.write_text(
        f"#!{sys.executable}\n"
        f"import os; os.environ['HOME'] = {str(home)!r}\n"
        f"import sys; sys.path.insert(0, {str(SOURCE_ROOT)!r})\n"
        "from universal_docs_mcp.command_hook import main\n"
        "raise SystemExit(main())\n"
    )
    preflight = tmp_path / "universal-docs-preflight"
    preflight.write_text(
        f"#!{sys.executable}\n"
        f"import os; os.environ['HOME'] = {str(home)!r}\n"
        f"import asyncio, json, sys; sys.path.insert(0, {str(SOURCE_ROOT)!r})\n"
        "from universal_docs_mcp import preflight\n"
        "from universal_docs_mcp.cache import DocsCache\n"
        "from universal_docs_mcp.docs_fetcher import FetchedDocument\n"
        "from universal_docs_mcp.registries import PackageInfo\n"
        "async def pkg(*a, **k): return PackageInfo('demo', 'python', '1.0.1', 'fixture')\n"
        "async def doc(*a, **k): return FetchedDocument('## Migration upgrade breaking changes quick start\\nSOURCE_BODY_MARKER', 'pypi_description', 'https://pypi.org/pypi/demo/1.0.1/json')\n"
        "raw=sys.stdin.buffer.read(); req=preflight.parse_request(raw)\n"
        "out=asyncio.run(preflight.run_preflight(req, cache=DocsCache(), fetch_package_fn=pkg, fetch_document_fn=doc))\n"
        "sys.stdout.buffer.write(preflight._bounded_json_bytes(out)+b'\\n')\n"
    )
    for path in (hook, preflight):
        path.chmod(0o700)
    monkeypatch.setenv("UNIVERSAL_DOCS_INIT_UNIVERSAL_DOCS_COMMAND_HOOK", str(hook))
    monkeypatch.setenv("UNIVERSAL_DOCS_INIT_UNIVERSAL_DOCS_PREFLIGHT", str(preflight))

    root = tmp_path / "project"
    (root / "before").mkdir(parents=True)
    (root / "after").mkdir()
    (root / "before/requirements.txt").write_text("demo==1.0.0\n")
    (root / "after/requirements.txt").write_text("demo==1.0.1\n")
    out = io.BytesIO()
    assert product_cli.main(["init", "--harness", "claude-code", "--project-root", str(root), "--before", "before/requirements.txt", "--after", "after/requirements.txt", "--apply"], stdout=out) == 0
    monkeypatch.setattr(product_cli, "_doctor_installation", lambda: {"status": "pass", "reason": "identity_match", "module_path": "module.py", "installed_version": "0.4.0rc2", "executable": "python"})
    assert not cache.exists()
    before, before_status = product_cli._doctor_cache(root)
    assert before_status == "pass" and before["root_status"] == "missing"
    out = io.BytesIO()
    assert product_cli.main(["doctor", "--project-root", str(root)], stdout=out) == 0
    receipt = json.loads(out.getvalue())
    assert receipt["source_probe"]["source_body_omitted"] is True
    assert "SOURCE_BODY_MARKER" not in json.dumps(receipt)
    assert cache.is_dir() and (cache / "cache.db").is_file()
    after, after_status = product_cli._doctor_cache(root)
    assert after_status == "pass" and after["root_status"] == "directory"
    assert after["entries"]["regular"] >= 1
    names_before_repeat = {p.name for p in cache.iterdir()}
    out = io.BytesIO()
    assert product_cli.main(["doctor", "--project-root", str(root)], stdout=out) == 0
    assert names_before_repeat == {p.name for p in cache.iterdir()}
    assert len(out.getvalue()) <= product_cli.MAX_OUTPUT_BYTES
    assert str(home) not in out.getvalue().decode()


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
@pytest.mark.parametrize("name", sorted(product_cli._DOCTOR_CACHE_OWNED_NAMES))
def test_each_owned_cache_entry_requires_regular_topology_and_never_touches_target(
    tmp_path, monkeypatch, kind, name
):
    cache = tmp_path / "cache"
    cache.mkdir()
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"untouched")
    target = tmp_path / "target"
    target.write_bytes(b"private cache body")
    entry = cache / name
    if kind == "symlink":
        entry.symlink_to(target)
    elif kind == "directory":
        entry.mkdir()
    else:
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO unavailable")
        os.mkfifo(entry)
    info, status = _cache_call(cache, monkeypatch)
    assert status == "fail"
    assert info["reason"] == product_cli._DOCTOR_CACHE_UNSAFE_REASON
    assert info["entries"]["validity"] == "topology_only"
    assert sentinel.read_bytes() == b"untouched"
    assert target.read_bytes() == b"private cache body"


@pytest.mark.parametrize("name", sorted(product_cli._DOCTOR_CACHE_OWNED_NAMES))
def test_owned_entry_stat_error_is_unknown_without_following_or_reading(tmp_path, monkeypatch, name):
    cache = tmp_path / "cache"
    cache.mkdir()

    class Entry:
        def __init__(self):
            self.name = name

        def stat(self, *, follow_symlinks):
            assert follow_symlinks is False
            raise PermissionError("secret path")

    class Scan:
        def __enter__(self):
            return iter((Entry(),))

        def __exit__(self, *args):
            return False

    monkeypatch.setenv("UNIVERSAL_DOCS_CACHE_DIR", str(cache))
    monkeypatch.setattr(product_cli.os, "scandir", lambda path: Scan())
    info, status = product_cli._doctor_cache(Path("/unused"))
    assert status == "unknown"
    assert info["reason"] == product_cli._DOCTOR_CACHE_ENTRY_ERROR_REASON
    assert "secret path" not in json.dumps(info)


def test_scandir_root_error_is_bounded_and_path_safe(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("UNIVERSAL_DOCS_CACHE_DIR", str(cache))
    monkeypatch.setattr(product_cli.os, "scandir", lambda path: (_ for _ in ()).throw(PermissionError("private")))
    info, status = product_cli._doctor_cache(Path("/unused"))
    assert status == "unknown"
    assert info["reason"] == "cache_entries_inaccessible"
    assert "private" not in json.dumps(info)


def test_oversized_encoded_name_is_counted_without_reading_entry(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()

    class Entry:
        name = "x" * 256

        def stat(self, *, follow_symlinks):
            return type("Stat", (), {"st_mode": stat.S_IFREG | 0o600})()

    class Scan:
        def __enter__(self):
            return iter((Entry(),))

        def __exit__(self, *args):
            return False

    monkeypatch.setenv("UNIVERSAL_DOCS_CACHE_DIR", str(cache))
    monkeypatch.setattr(product_cli.os, "scandir", lambda path: Scan())
    info, status = product_cli._doctor_cache(Path("/unused"))
    assert status == "pass"
    assert info["entries"]["oversized_names"] == 1


def test_cache_entry_enumeration_is_capped_at_256(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()

    class Entry:
        def __init__(self, number):
            self.name = f"unrelated-{number}"

        def stat(self, *, follow_symlinks):
            return type("Stat", (), {"st_mode": stat.S_IFREG | 0o600})()

    class Scan:
        def __enter__(self):
            return iter(Entry(number) for number in range(257))

        def __exit__(self, *args):
            return False

    monkeypatch.setenv("UNIVERSAL_DOCS_CACHE_DIR", str(cache))
    monkeypatch.setattr(product_cli.os, "scandir", lambda path: Scan())
    info, status = product_cli._doctor_cache(Path("/unused"))
    assert status == "pass"
    assert info["entries"]["total"] == 256
    assert info["entries"]["names_truncated"] is True
