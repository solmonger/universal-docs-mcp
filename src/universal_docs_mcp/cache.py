"""Bounded, resilient SQLite cache for documentation lookups."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

DEFAULT_CACHE_DIR = Path.home() / ".cache" / "universal-docs-mcp"
DEFAULT_TTL = 86400  # 24 hours
DEFAULT_MAX_VALUE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_ENTRIES = 256
DEFAULT_MAX_TOTAL_BYTES = 32 * 1024 * 1024
SQLITE_BUSY_TIMEOUT_MS = 200
WAL_AUTOCHECKPOINT_PAGES = 1_000


class DocsCache:
    """A TTL cache that fails closed when its filesystem or database is unavailable.

    Cache failures are deliberately treated as misses.  Callers can continue
    retrieval uncached, while :attr:`available` exposes whether persistence is
    currently working.
    """

    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        ttl: int = DEFAULT_TTL,
        max_value_bytes: int = DEFAULT_MAX_VALUE_BYTES,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    ):
        # Set lifecycle state before any operation that can fail.  In
        # particular, __del__ must remain safe if startup cannot create a dir.
        self._conn: Optional[sqlite3.Connection] = None
        self._available: Optional[bool] = None

        configured_dir = os.environ.get("UNIVERSAL_DOCS_CACHE_DIR")
        self.cache_dir = (
            Path(cache_dir)
            if cache_dir is not None
            else Path(configured_dir or DEFAULT_CACHE_DIR)
        )
        self.db_path = self.cache_dir / "cache.db"
        self.ttl = ttl
        self.max_value_bytes = max(0, int(max_value_bytes))
        self.max_entries = max(0, int(max_entries))
        self.max_total_bytes = max(0, int(max_total_bytes))

    def _get_conn(self) -> sqlite3.Connection:
        """Open and initialize the database on first use."""
        if self._conn is not None:
            return self._conn

        conn: Optional[sqlite3.Connection] = None
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
            )
            conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute(f"PRAGMA wal_autocheckpoint = {WAL_AUTOCHECKPOINT_PAGES}")
            self._init_schema(conn)
            self._conn = conn
            self._available = True
            return conn
        except Exception:
            self._available = False
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            raise

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS docs_cache (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                fetched_at REAL NOT NULL,
                value_bytes INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(docs_cache)").fetchall()
        }
        if "value_bytes" not in columns:
            # Migrate databases created by the original three-column schema.
            conn.execute(
                "ALTER TABLE docs_cache "
                "ADD COLUMN value_bytes INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute(
            """
            UPDATE docs_cache
            SET value_bytes = length(CAST(value AS BLOB))
            WHERE value_bytes IS NULL OR value_bytes = 0
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_docs_cache_fetched_at
            ON docs_cache (fetched_at)
            """
        )
        conn.commit()

    def _mark_failed(self) -> None:
        self._available = False
        conn = self._conn
        self._conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    @staticmethod
    def _checkpoint(conn: sqlite3.Connection) -> None:
        # PASSIVE never waits for readers.  wal_autocheckpoint above handles
        # normal growth; this opportunistic checkpoint keeps small caches tidy.
        try:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass

    @staticmethod
    def _encode(value: dict) -> tuple[str, int]:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        return encoded, len(encoded.encode("utf-8"))

    def get(self, key: str) -> Optional[dict]:
        try:
            row = (
                self._get_conn()
                .execute(
                    "SELECT value, fetched_at FROM docs_cache WHERE key = ? AND length(CAST(value AS BLOB)) <= ?",
                    (key, self.max_value_bytes),
                )
                .fetchone()
            )
        except Exception:
            self._mark_failed()
            return None

        if row is None:
            return None

        value, fetched_at = row
        try:
            if time.time() - float(fetched_at) > self.ttl:
                return None
            decoded = json.loads(value)
        except (TypeError, ValueError, RecursionError):
            # A damaged row is an ordinary cache miss, never a retrieval error.
            return None
        except Exception:
            self._mark_failed()
            return None

        return decoded if isinstance(decoded, dict) else None

    def _prune_expired(self, conn: sqlite3.Connection) -> None:
        cutoff = time.time() - self.ttl
        conn.execute("DELETE FROM docs_cache WHERE fetched_at <= ?", (cutoff,))

    def _prune_to_quota(self, conn: sqlite3.Connection) -> None:
        """Remove oldest rows until both quotas hold."""
        while True:
            count, total_bytes = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(value_bytes), 0) FROM docs_cache"
            ).fetchone()
            if count <= self.max_entries and total_bytes <= self.max_total_bytes:
                return

            oldest = conn.execute(
                """
                SELECT key
                FROM docs_cache
                ORDER BY fetched_at ASC, rowid ASC
                LIMIT 1
                """
            ).fetchone()
            if oldest is None:
                return
            conn.execute("DELETE FROM docs_cache WHERE key = ?", (oldest[0],))

    def set(self, key: str, value: dict) -> bool:
        """Store a value, returning False when it cannot be cached."""
        try:
            encoded, value_bytes = self._encode(value)
        except (TypeError, ValueError, OverflowError):
            return False

        if (
            self.max_entries < 1
            or value_bytes > self.max_value_bytes
            or value_bytes > self.max_total_bytes
        ):
            return False

        conn: Optional[sqlite3.Connection] = None
        try:
            conn = self._get_conn()
            conn.execute("BEGIN IMMEDIATE")
            self._prune_expired(conn)
            now = time.time()
            conn.execute(
                """
                INSERT INTO docs_cache (key, value, fetched_at, value_bytes)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    fetched_at = excluded.fetched_at,
                    value_bytes = excluded.value_bytes
                """,
                (key, encoded, now, value_bytes),
            )
            self._prune_to_quota(conn)
            conn.commit()
            self._checkpoint(conn)
            return True
        except Exception:
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            self._mark_failed()
            return False

    def clear(self) -> bool:
        try:
            conn = self._get_conn()
            conn.execute("DELETE FROM docs_cache")
            conn.commit()
            self._checkpoint(conn)
            return True
        except Exception:
            self._mark_failed()
            return False

    def stats(self) -> dict:
        """Return the legacy exact three-counter shape, even when unavailable."""
        try:
            conn = self._get_conn()
            total = conn.execute("SELECT COUNT(*) FROM docs_cache").fetchone()[0]
            valid = conn.execute(
                "SELECT COUNT(*) FROM docs_cache WHERE fetched_at > ?",
                (time.time() - self.ttl,),
            ).fetchone()[0]
            return {"total": total, "valid": valid, "expired": total - valid}
        except Exception:
            self._mark_failed()
            return {"total": 0, "valid": 0, "expired": 0}

    @property
    def available(self) -> bool:
        """Whether the cache can currently persist data."""
        try:
            self._get_conn()
        except Exception:
            return False
        return self._available is True

    @property
    def is_available(self) -> bool:
        """Compatibility alias for callers that prefer an ``is_*`` name."""
        return self.available

    def stats_with_availability(self) -> dict:
        """Return stats plus availability without changing :meth:`stats`."""
        result = self.stats()
        result["available"] = self.available
        return result

    def close(self) -> None:
        conn = self._conn
        self._conn = None
        self._available = None
        if conn is not None:
            try:
                self._checkpoint(conn)
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
