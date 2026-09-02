"""Tests for the in-memory live-session SSE fan-out."""

from __future__ import annotations

import asyncio

import pytest

from zorkbot.admin.bus import SessionBus


@pytest.mark.asyncio
async def test_subscribe_replays_backlog() -> None:
    bus = SessionBus(buffer_size=10, max_streams=4)
    bus.publish(1, "output", {"text": "North of House"})
    bus.publish(1, "output", {"text": "You are facing the north side..."})

    queue, backlog = bus.subscribe(1)
    assert [item["data"]["text"] for item in backlog] == [
        "North of House",
        "You are facing the north side...",
    ]
    assert queue.empty()


@pytest.mark.asyncio
async def test_fan_out_to_multiple_subscribers() -> None:
    bus = SessionBus(buffer_size=10, max_streams=4)
    q1, _ = bus.subscribe(1)
    q2, _ = bus.subscribe(1)

    bus.publish(1, "command", {"text": "go north"})

    item1 = await asyncio.wait_for(q1.get(), timeout=1)
    item2 = await asyncio.wait_for(q2.get(), timeout=1)
    assert item1["data"]["text"] == "go north"
    assert item2["data"]["text"] == "go north"


@pytest.mark.asyncio
async def test_max_streams_cap() -> None:
    bus = SessionBus(buffer_size=10, max_streams=2)
    bus.subscribe(1)
    bus.subscribe(2)
    assert bus.can_subscribe() is False


@pytest.mark.asyncio
async def test_session_end_drops_channel() -> None:
    bus = SessionBus(buffer_size=10, max_streams=4)
    queue, _ = bus.subscribe(1)
    bus.publish(1, "session_end", {"reason": "player_end"})

    # Buffer for a since-ended session starts fresh.
    _, backlog = bus.subscribe(1)
    assert backlog == []


@pytest.mark.asyncio
async def test_unsubscribe_frees_a_stream_slot() -> None:
    bus = SessionBus(buffer_size=10, max_streams=1)
    queue, _ = bus.subscribe(1)
    assert bus.can_subscribe() is False
    bus.unsubscribe(1, queue)
    assert bus.can_subscribe() is True
