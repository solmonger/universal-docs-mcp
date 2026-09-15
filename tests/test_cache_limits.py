import json
import sqlite3

from universal_docs_mcp.cache import (
    DEFAULT_MAX_ENTRIES,
    DEFAULT_MAX_TOTAL_BYTES,
    DEFAULT_MAX_VALUE_BYTES,
    DocsCache,
)


def test_cache_directory_env_is_lazy_and_configurable(monkeypatch, tmp_path):
    cache_dir = tmp_path / "env-cache"
    monkeypatch.setenv("UNIVERSAL_DOCS_CACHE_DIR", str(cache_dir))

    cache = DocsCache()
    assert not cache_dir.exists()

    assert cache.set("demo", {"content": "docs"}) is True
    assert cache_dir.joinpath("cache.db").exists()
    cache.close()


def test_individual_json_value_limit_rejects_oversize_values(tmp_path):
    cache = DocsCache(
        tmp_path,
        max_value_bytes=32,
        max_entries=4,
        max_total_bytes=128,
    )

    assert cache.set("small", {"content": "ok"}) is True
    assert cache.set("large", {"content": "x" * 100}) is False
    assert cache.get("large") is None
    assert cache.get("small") == {"content": "ok"}
    assert cache.stats() == {"total": 1, "valid": 1, "expired": 0}
    cache.close()


def test_max_entries_prunes_oldest_entry(tmp_path):
    cache = DocsCache(tmp_path, max_entries=2, max_total_bytes=1024)

    assert cache.set("first", {"value": 1}) is True
    assert cache.set("second", {"value": 2}) is True
    assert cache.set("third", {"value": 3}) is True

    assert cache.get("first") is None
    assert cache.get("second") == {"value": 2}
    assert cache.get("third") == {"value": 3}
    assert cache.stats() == {"total": 2, "valid": 2, "expired": 0}
    cache.close()


def test_total_logical_value_bytes_prune_oldest_entry(tmp_path):
    cache = DocsCache(tmp_path, max_entries=10, max_value_bytes=128, max_total_bytes=20)

    assert cache.set("first", {"value": "12345"}) is True
    assert cache.set("second", {"value": "67890"}) is True

    assert cache.get("first") is None
    assert cache.get("second") == {"value": "67890"}
    assert cache.stats() == {"total": 1, "valid": 1, "expired": 0}
    cache.close()


def test_set_prunes_expired_entries_before_adding_new_value(tmp_path):
    cache = DocsCache(tmp_path, ttl=-1, max_entries=10, max_total_bytes=1024)

    assert cache.set("expired", {"value": "old"}) is True
    assert cache.set("current", {"value": "new"}) is True

    assert cache.stats() == {"total": 1, "valid": 0, "expired": 1}
    assert cache.get("expired") is None
    assert cache.get("current") is None
    cache.close()


def test_malformed_cached_json_is_a_miss(tmp_path):
    cache = DocsCache(tmp_path)
    assert cache.set("demo", {"content": "docs"}) is True
    cache.close()

    with sqlite3.connect(tmp_path / "cache.db") as conn:
        conn.execute(
            "UPDATE docs_cache SET value = ? WHERE key = ?", ("{malformed", "demo")
        )

    cache = DocsCache(tmp_path)
    assert cache.get("demo") is None
    cache.close()


def test_cache_failures_degrade_to_uncached_mode(tmp_path):
    blocked_path = tmp_path / "not-a-directory"
    blocked_path.write_text("not a directory")
    cache = DocsCache(blocked_path)

    assert cache.get("demo") is None
    assert cache.set("demo", {"content": "docs"}) is False
    assert cache.stats() == {"total": 0, "valid": 0, "expired": 0}
    assert cache.available is False
    assert cache.stats_with_availability() == {
        "total": 0,
        "valid": 0,
        "expired": 0,
        "available": False,
    }
    cache.close()


def test_close_is_safe_after_partial_initialization(tmp_path):
    blocked_path = tmp_path / "not-a-directory"
    blocked_path.write_text("not a directory")
    cache = DocsCache(blocked_path)
    cache.close()
    cache.close()


def test_sqlite_uses_wal_autocheckpoint_and_bounded_busy_timeout(tmp_path):
    cache = DocsCache(tmp_path)
    assert cache.set("demo", {"content": "docs"}) is True

    with sqlite3.connect(tmp_path / "cache.db") as conn:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        wal_autocheckpoint = conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]

    assert journal_mode.lower() == "wal"
    assert 0 < wal_autocheckpoint <= 1000
    assert 0 < busy_timeout <= 10_000
    cache.close()


def test_default_limits_are_bounded():
    assert DEFAULT_MAX_VALUE_BYTES == 2 * 1024 * 1024
    assert DEFAULT_MAX_ENTRIES == 256
    assert DEFAULT_MAX_TOTAL_BYTES == 32 * 1024 * 1024


async def test_tool_reports_cache_unavailability(monkeypatch, tmp_path):
    from universal_docs_mcp import server

    path = tmp_path / "blocked"
    path.write_text("not a directory")
    monkeypatch.setattr(server, "cache", DocsCache(path))
    result = await server.call_tool("cache_stats", {})
    payload = json.loads(result[0].text)
    assert payload["available"] is False
    assert payload["total"] is None


def test_busy_timeout_applies_to_real_cache_connection(tmp_path):
    cache = DocsCache(tmp_path)
    cache.set("demo", {})
    assert 0 < cache._get_conn().execute("PRAGMA busy_timeout").fetchone()[0] <= 250
    cache.close()


def test_legacy_oversize_cache_row_is_not_loaded(tmp_path):
    cache = DocsCache(tmp_path, max_value_bytes=32)
    cache.set("demo", {})
    conn = cache._get_conn()
    conn.execute(
        "UPDATE docs_cache SET value=?", (json.dumps({"content": "x" * 1000}),)
    )
    conn.commit()
    assert cache.get("demo") is None
    cache.close()


def test_stats_shape_remains_compatible(tmp_path):
    cache = DocsCache(tmp_path)
    assert set(cache.stats()) == {"total", "valid", "expired"}
    cache.close()
