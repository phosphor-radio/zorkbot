"""Tests for the admin UI's SQLite store and batched event sink."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from zorkbot.admin.bus import SessionBus
from zorkbot.admin.events import SqliteEventSink
from zorkbot.admin.store import Store
from zorkbot.session_state import SessionRecord


@pytest.fixture
async def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store(Path(tmp) / "admin.db", event_queue_size=8)
        await s.start()
        yield s
        s.close()


@pytest.mark.asyncio
async def test_schema_created(store) -> None:
    row = await store.query_one("SELECT value FROM schema_meta WHERE key = 'version'")
    assert row is not None


@pytest.mark.asyncio
async def test_run_many_is_transactional(store) -> None:
    await store.run_many(
        [
            (
                "INSERT INTO players(pubkey_prefix, name, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?)",
                ("aabbccddeeff", "Alice", 1, 1),
            ),
        ]
    )
    row = await store.query_one("SELECT name FROM players WHERE pubkey_prefix = ?", ("aabbccddeeff",))
    assert row["name"] == "Alice"


@pytest.mark.asyncio
async def test_sink_writes_session_lifecycle(store) -> None:
    bus = SessionBus()
    sink = SqliteEventSink(store, bus, bot_run_id="run1", queue_size=64)
    sink.start()

    # Deliberately no player_seen() call first: session_started() must be
    # self-sufficient (it upserts the player row itself) rather than relying
    # on another call site's ordering — the CLI simulator drives the bot
    # directly and never calls player_seen at all, which used to trip the
    # sessions.pubkey_prefix -> players FK constraint.
    record = SessionRecord(num=1, player_id="aabbccddeeff", player_name="Alice")
    sink.session_started(record)
    sink.watchers_changed(record)
    sink.session_ended(record, "player_end")

    await sink.stop()

    row = await store.query_one(
        "SELECT * FROM sessions WHERE bot_run_id = ? AND session_num = ?", ("run1", 1)
    )
    assert row is not None
    assert row["pubkey_prefix"] == "aabbccddeeff"
    assert row["end_reason"] == "player_end"
    assert row["ended_at"] is not None

    player = await store.query_one(
        "SELECT * FROM players WHERE pubkey_prefix = ?", ("aabbccddeeff",)
    )
    assert player["name"] == "Alice"


@pytest.mark.asyncio
async def test_sink_drops_on_queue_overflow_without_raising(store) -> None:
    bus = SessionBus()
    sink = SqliteEventSink(store, bus, bot_run_id="run1", queue_size=1)
    # Never started — queue never drains — so this must fill and drop, not block.
    for _ in range(10):
        sink.command(
            pubkey_prefix="aabbccddeeff",
            command="look",
            transport="dm",
            channel_idx=None,
            accepted=True,
        )
    assert sink._drop_count >= 1


@pytest.mark.asyncio
async def test_retention_prunes_old_rows(store) -> None:
    old = 1  # 1970-ish, guaranteed older than any retention window
    await store.run(
        "INSERT INTO commands(at, pubkey_prefix, command, transport, channel_idx, accepted, reject_reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (old, "aabbccddeeff", "look", "dm", None, 1, None),
    )
    store._retention_days = 1
    await store.prune_retention()
    rows = await store.query("SELECT * FROM commands")
    assert rows == []


def test_bucket_and_sort_allowlists_reject_unknown_values() -> None:
    with pytest.raises(ValueError):
        Store.bucket_seconds("fortnight")
    with pytest.raises(ValueError):
        Store.player_sort_column("; DROP TABLE players;--")


# ----------------------------------------------------------------------
# Schema migration (v1 -> v2: messages.acked)
# ----------------------------------------------------------------------

# The v1 messages table, exactly as it shipped before delivery ACKs.
_V1_MESSAGES = """
CREATE TABLE messages (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  at            INTEGER NOT NULL,
  direction     TEXT    NOT NULL,
  transport     TEXT    NOT NULL,
  channel_idx   INTEGER,
  pubkey_prefix TEXT,
  chars         INTEGER NOT NULL,
  dropped       INTEGER NOT NULL DEFAULT 0
);
"""


def _make_v1_db(path: Path) -> None:
    import sqlite3

    conn = sqlite3.connect(str(path))
    conn.executescript(_V1_MESSAGES)
    conn.execute(
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO schema_meta(key, value) VALUES ('version', '1')")
    conn.execute(
        "INSERT INTO messages(at, direction, transport, chars) VALUES (1, 'tx', 'dm', 10)"
    )
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_v1_database_gains_the_acked_column() -> None:
    """CREATE TABLE IF NOT EXISTS leaves an existing table alone, so without
    a migration every insert would fail on the unknown column."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "admin.db"
        _make_v1_db(path)

        s = Store(path)
        await s.start()
        try:
            columns = {
                row["name"] for row in await s.query("PRAGMA table_info(messages)")
            }
            assert "acked" in columns

            # The pre-existing row reads as "never measured", not as a failure.
            row = await s.query_one("SELECT acked FROM messages WHERE id = 1")
            assert row["acked"] is None

            version = await s.query_one(
                "SELECT value FROM schema_meta WHERE key = 'version'"
            )
            assert version["value"] == "2"
        finally:
            s.close()


@pytest.mark.asyncio
async def test_migration_is_idempotent() -> None:
    """It runs on every open, and there is no down path."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "admin.db"
        _make_v1_db(path)

        for _ in range(3):
            s = Store(path)
            await s.start()
            s.close()

        s = Store(path)
        await s.start()
        try:
            columns = [
                row["name"] for row in await s.query("PRAGMA table_info(messages)")
            ]
            assert columns.count("acked") == 1
        finally:
            s.close()


@pytest.mark.asyncio
async def test_acked_round_trips(store) -> None:
    for acked in (None, 0, 1):
        await store.run(
            "INSERT INTO messages(at, direction, transport, chars, acked) "
            "VALUES (1, 'tx', 'dm', 10, ?)",
            (acked,),
        )
    rows = await store.query("SELECT acked FROM messages ORDER BY id")
    assert [row["acked"] for row in rows] == [None, 0, 1]


@pytest.mark.asyncio
async def test_sink_records_delivery_outcome(store) -> None:
    """acked distinguishes delivered / not delivered / not measured."""
    bus = SessionBus()
    sink = SqliteEventSink(store, bus, bot_run_id="run1", queue_size=64)
    sink.start()

    sink.message_tx(
        transport="dm", channel_idx=None, pubkey_prefix="aabbccddeeff", chars=10,
        acked=True,
    )
    sink.message_tx(
        transport="dm", channel_idx=None, pubkey_prefix="aabbccddeeff", chars=10,
        acked=False,
    )
    # Watcher fan-out and channel messages: transmitted, never measured.
    sink.message_tx(
        transport="dm", channel_idx=None, pubkey_prefix="112233445566", chars=10,
    )
    sink.message_tx(transport="channel", channel_idx=1, pubkey_prefix=None, chars=10)
    # Dropped before it ever reached the air, which is not a failed delivery.
    sink.message_tx(
        transport="dm", channel_idx=None, pubkey_prefix="aabbccddeeff", chars=10,
        dropped=True,
    )

    await sink.stop()

    rows = await store.query("SELECT acked, dropped FROM messages ORDER BY id")
    assert [row["acked"] for row in rows] == [1, 0, None, None, None]

    counts = await store.query_one(
        "SELECT SUM(acked = 1) AS delivered, SUM(acked = 0) AS failed "
        "FROM messages WHERE direction = 'tx' AND transport = 'dm' "
        "AND acked IS NOT NULL"
    )
    assert counts["delivered"] == 1
    assert counts["failed"] == 1
