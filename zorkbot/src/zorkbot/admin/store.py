"""SQLite-backed event log and query layer for the admin UI.

All actual sqlite3 calls happen on a worker thread via `asyncio.to_thread`,
serialized by a `threading.Lock` around the one shared connection — never on
the event loop. Writes are non-blocking from the caller's perspective: they
land on a bounded in-process queue drained by a single batching writer task.
On overflow, events are dropped (never block, never fail the RF path) and a
rate-limited warning is logged.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_user (
  id                   INTEGER PRIMARY KEY CHECK (id = 1),
  username             TEXT    NOT NULL DEFAULT 'admin',
  password_hash        BLOB    NOT NULL,
  password_salt        BLOB    NOT NULL,
  kdf                  TEXT    NOT NULL DEFAULT 'scrypt',
  kdf_params           TEXT    NOT NULL,
  must_change_password INTEGER NOT NULL DEFAULT 1,
  password_changed_at  INTEGER,
  created_at           INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
  token_hash  BLOB    PRIMARY KEY,
  issued_at   INTEGER NOT NULL,
  expires_at  INTEGER NOT NULL,
  revoked_at  INTEGER,
  replaced_by BLOB
);
CREATE INDEX IF NOT EXISTS idx_refresh_expires ON refresh_tokens(expires_at);

CREATE TABLE IF NOT EXISTS players (
  pubkey_prefix TEXT    PRIMARY KEY,
  name          TEXT,
  first_seen_at INTEGER NOT NULL,
  last_seen_at  INTEGER NOT NULL,
  banned_at     INTEGER
);

CREATE TABLE IF NOT EXISTS sessions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_run_id    TEXT    NOT NULL,
  session_num   INTEGER NOT NULL,
  pubkey_prefix TEXT    NOT NULL REFERENCES players(pubkey_prefix),
  started_at    INTEGER NOT NULL,
  ended_at      INTEGER,
  end_reason    TEXT,
  peak_watchers INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_run_num ON sessions(bot_run_id, session_num);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_sessions_player  ON sessions(pubkey_prefix, started_at);

CREATE TABLE IF NOT EXISTS commands (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  at            INTEGER NOT NULL,
  pubkey_prefix TEXT,
  command       TEXT    NOT NULL,
  transport     TEXT    NOT NULL,
  channel_idx   INTEGER,
  accepted      INTEGER NOT NULL,
  reject_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_commands_at     ON commands(at);
CREATE INDEX IF NOT EXISTS idx_commands_player ON commands(pubkey_prefix, at);

CREATE TABLE IF NOT EXISTS messages (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  at            INTEGER NOT NULL,
  direction     TEXT    NOT NULL,
  transport     TEXT    NOT NULL,
  channel_idx   INTEGER,
  pubkey_prefix TEXT,
  chars         INTEGER NOT NULL,
  dropped       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_at     ON messages(at, direction);
CREATE INDEX IF NOT EXISTS idx_messages_player ON messages(pubkey_prefix, at);
"""

_VALID_BUCKETS = {"minute": 60, "hour": 3600, "day": 86400}
_VALID_PLAYER_SORTS = {
    "last_active": "last_seen_at",
    "first_active": "first_seen_at",
    "sessions": "sessions_started",
    "messages_rx": "messages_received_from",
    "messages_tx": "messages_sent_to",
}


class Store:
    def __init__(
        self,
        db_path: str | Path,
        *,
        event_queue_size: int = 1024,
        retention_days: int = 90,
    ) -> None:
        self._db_path = Path(db_path)
        self._retention_days = retention_days
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._drop_count = 0
        self._last_drop_log = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        await asyncio.to_thread(self._open)

    def _open(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('version', ?)",
            (str(_SCHEMA_VERSION),),
        )
        conn.commit()
        try:
            os.chmod(self._db_path, 0o600)
        except OSError:
            pass
        self._conn = conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        assert self._conn is not None, "Store not started"
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _execute_many_in_txn(self, statements: list[tuple[str, tuple]]) -> None:
        assert self._conn is not None, "Store not started"
        with self._lock:
            try:
                for sql, params in statements:
                    self._conn.execute(sql, params)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    async def run(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return await asyncio.to_thread(self._execute, sql, params)

    async def run_many(self, statements: list[tuple[str, tuple]]) -> None:
        await asyncio.to_thread(self._execute_many_in_txn, statements)

    async def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        def _q() -> list[sqlite3.Row]:
            assert self._conn is not None, "Store not started"
            self._conn.row_factory = sqlite3.Row
            with self._lock:
                return list(self._conn.execute(sql, params))

        return await asyncio.to_thread(_q)

    async def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        rows = await self.query(sql, params)
        return rows[0] if rows else None

    # ------------------------------------------------------------------
    # Retention
    # ------------------------------------------------------------------

    async def prune_retention(self) -> None:
        if self._retention_days <= 0:
            return
        cutoff = int(time.time()) - self._retention_days * 86400
        statements = [
            ("DELETE FROM commands WHERE at < ?", (cutoff,)),
            ("DELETE FROM messages WHERE at < ?", (cutoff,)),
            (
                "DELETE FROM sessions WHERE ended_at IS NOT NULL AND ended_at < ?",
                (cutoff,),
            ),
            (
                "DELETE FROM refresh_tokens WHERE expires_at < ? OR revoked_at IS NOT NULL AND revoked_at < ?",
                (int(time.time()), cutoff),
            ),
        ]
        await self.run_many(statements)
        await self.run("PRAGMA incremental_vacuum")
        logger.info("admin-ui retention prune complete (cutoff=%d)", cutoff)

    # ------------------------------------------------------------------
    # Bucket / sort allowlists (never interpolate raw user input into SQL)
    # ------------------------------------------------------------------

    @staticmethod
    def bucket_seconds(bucket: str) -> int:
        if bucket not in _VALID_BUCKETS:
            raise ValueError(f"invalid bucket: {bucket!r}")
        return _VALID_BUCKETS[bucket]

    @staticmethod
    def player_sort_column(sort: str) -> str:
        if sort not in _VALID_PLAYER_SORTS:
            raise ValueError(f"invalid sort: {sort!r}")
        return _VALID_PLAYER_SORTS[sort]
