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
