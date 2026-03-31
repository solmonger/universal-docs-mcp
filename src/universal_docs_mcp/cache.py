"""SQLite cache for documentation lookups.

TTL-based caching to avoid hammering registries on repeated queries.
"""

import json
import sqlite3
import time
from pathlib import Path
from typing import Optional


DEFAULT_CACHE_DIR = Path.home() / ".cache" / "universal-docs-mcp"
DEFAULT_TTL = 86400  # 24 hours


class DocsCache:
    def __init__(self, cache_dir: Optional[Path] = None, ttl: int = DEFAULT_TTL):
        self.cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl
        self.db_path = self.cache_dir / "cache.db"
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def _init_db(self):
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS docs_cache (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                fetched_at REAL NOT NULL
            )
        """)
        conn.commit()

    def get(self, key: str) -> Optional[dict]:
        row = self._get_conn().execute(
            "SELECT value, fetched_at FROM docs_cache WHERE key = ?", (key,)
        ).fetchone()

        if row is None:
            return None

        value, fetched_at = row
        if time.time() - fetched_at > self.ttl:
            return None  # expired

        return json.loads(value)

    def set(self, key: str, value: dict):
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO docs_cache (key, value, fetched_at) VALUES (?, ?, ?)",
            (key, json.dumps(value), time.time()),
        )
        conn.commit()

    def clear(self):
        conn = self._get_conn()
        conn.execute("DELETE FROM docs_cache")
        conn.commit()

    def stats(self) -> dict:
        conn = self._get_conn()
        total = conn.execute("SELECT COUNT(*) FROM docs_cache").fetchone()[0]
        valid = conn.execute(
            "SELECT COUNT(*) FROM docs_cache WHERE fetched_at > ?",
            (time.time() - self.ttl,),
        ).fetchone()[0]
        return {"total": total, "valid": valid, "expired": total - valid}

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __del__(self):
        self.close()
