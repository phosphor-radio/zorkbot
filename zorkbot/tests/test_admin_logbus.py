"""Tests for the in-memory process log tail and its SSE endpoint."""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from zorkbot.admin.logbus import LogBus, parse_level


def _logger(name: str) -> logging.Logger:
    log = logging.getLogger(name)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    for handler in list(log.handlers):
        log.removeHandler(handler)
    return log


def _record(logger_name: str = "zorkbot.test", level: int = logging.INFO, msg: str = "hi") -> logging.LogRecord:
    return logging.LogRecord(logger_name, level, __file__, 1, msg, None, None)


def test_buffer_replays_recent_records() -> None:
    bus = LogBus(buffer_size=10)
    bus.emit(_record(msg="first"))
    bus.emit(_record(msg="second"))

    queue, backlog = _subscribe_off_loop(bus)
    assert [item["message"] for item in backlog] == ["first", "second"]


def test_buffer_is_bounded() -> None:
    bus = LogBus(buffer_size=3)
    for n in range(10):
        bus.emit(_record(msg=f"line {n}"))

    _, backlog = _subscribe_off_loop(bus)
    assert [item["message"] for item in backlog] == ["line 7", "line 8", "line 9"]
    assert bus.buffer_size == 3


def test_backlog_is_filtered_by_level() -> None:
    bus = LogBus(buffer_size=10)
    bus.emit(_record(level=logging.DEBUG, msg="noisy"))
    bus.emit(_record(level=logging.ERROR, msg="broken"))

    _, backlog = _subscribe_off_loop(bus, min_level=logging.WARNING)
    assert [item["message"] for item in backlog] == ["broken"]


def test_records_carry_increasing_sequence_ids() -> None:
    bus = LogBus(buffer_size=10)
    bus.emit(_record(msg="a"))
    bus.emit(_record(msg="b"))

    _, backlog = _subscribe_off_loop(bus)
    assert [item["seq"] for item in backlog] == [1, 2]


def test_attach_and_detach_toggle_capture() -> None:
    log = _logger("zorkbot.attachtest")
    bus = LogBus(buffer_size=10)

    bus.attach(log)
    log.info("captured")
    bus.detach()
    log.info("not captured")

    _, backlog = _subscribe_off_loop(bus)
    assert [item["message"] for item in backlog] == ["captured"]
    assert bus not in log.handlers


def test_message_formatting_includes_args_and_traceback() -> None:
    log = _logger("zorkbot.fmt")
    bus = LogBus(buffer_size=10)
    bus.attach(log)
    try:
        log.warning("queue at %d of %d", 7, 10)
        try:
            raise ValueError("boom")
        except ValueError:
            log.exception("it broke")
    finally:
        bus.detach()

    _, backlog = _subscribe_off_loop(bus)
    assert backlog[0]["message"] == "queue at 7 of 10"
    assert backlog[0]["level"] == "WARNING"
    assert backlog[1]["message"].startswith("it broke\n")
    assert "ValueError: boom" in backlog[1]["message"]


def test_bad_format_args_do_not_raise_or_drop_the_record() -> None:
    bus = LogBus(buffer_size=10)
    # %d against a string: the caller's log line is broken, but a logging
    # handler must not raise into it.
    bus.emit(logging.LogRecord("zorkbot.bad", logging.INFO, __file__, 1, "n=%d", ("nope",), None))

    _, backlog = _subscribe_off_loop(bus)
    assert len(backlog) == 1
    assert "unformattable" in backlog[0]["message"]


def test_long_messages_are_truncated() -> None:
    bus = LogBus(buffer_size=10)
    bus.emit(_record(msg="x" * 50_000))

    _, backlog = _subscribe_off_loop(bus)
    assert backlog[0]["message"].endswith("… [truncated]")
    assert len(backlog[0]["message"]) < 9_000


def test_max_streams_cap() -> None:
    bus = LogBus(buffer_size=10, max_streams=2)
    q1, _ = _subscribe_off_loop(bus)
    _subscribe_off_loop(bus)
    assert bus.can_subscribe() is False
    bus.unsubscribe(q1)
    assert bus.can_subscribe() is True


@pytest.mark.asyncio
async def test_live_fan_out_to_subscribers() -> None:
    bus = LogBus(buffer_size=10)
    q1, _ = bus.subscribe()
    q2, _ = bus.subscribe(min_level=logging.ERROR)

    bus.emit(_record(level=logging.INFO, msg="chatter"))
    bus.emit(_record(level=logging.ERROR, msg="fault"))

    item = await asyncio.wait_for(q1.get(), timeout=1)
    assert item["message"] == "chatter"
    item = await asyncio.wait_for(q1.get(), timeout=1)
    assert item["message"] == "fault"

    # The ERROR-only subscriber never sees the INFO line.
    item = await asyncio.wait_for(q2.get(), timeout=1)
    assert item["message"] == "fault"
    assert q2.empty()


@pytest.mark.asyncio
async def test_records_from_a_worker_thread_reach_the_loop() -> None:
    """store.py logs from `asyncio.to_thread` workers — those must arrive too."""
    bus = LogBus(buffer_size=10)
    queue, _ = bus.subscribe()

    await asyncio.to_thread(bus.emit, _record(msg="from a worker"))

    item = await asyncio.wait_for(queue.get(), timeout=1)
    assert item["message"] == "from a worker"


@pytest.mark.asyncio
async def test_slow_subscriber_gets_an_explicit_gap_marker() -> None:
    bus = LogBus(buffer_size=1000)
    queue, _ = bus.subscribe()

    # Overflow the 256-slot subscriber queue, then let it drain one slot so the
    # next record can land and report what was lost.
    for n in range(300):
        bus.emit(_record(msg=f"line {n}"))
        await asyncio.sleep(0)
    await queue.get()
    bus.emit(_record(msg="after the flood"))
    await asyncio.sleep(0)

    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    last = items[-1]
    assert last["message"] == "after the flood"
    assert last["dropped_before"] > 0


@pytest.mark.asyncio
async def test_unsubscribed_queue_stops_receiving() -> None:
    bus = LogBus(buffer_size=10)
    queue, _ = bus.subscribe()
    bus.unsubscribe(queue)

    bus.emit(_record(msg="after unsubscribe"))
    await asyncio.sleep(0)
    assert queue.empty()


def test_emit_without_a_loop_still_buffers() -> None:
    """Startup logs land before the server (and its loop) exist."""
    bus = LogBus(buffer_size=10)
    bus.emit(_record(msg="during startup"))
    _, backlog = _subscribe_off_loop(bus)
    assert [item["message"] for item in backlog] == ["during startup"]


@pytest.mark.parametrize("name", ["debug", "INFO", " warning ", "Critical"])
def test_parse_level_accepts_known_names(name: str) -> None:
    assert parse_level(name) is not None


@pytest.mark.parametrize("name", ["", "TRACE", "NOTSET", "42", "INFO; DROP TABLE"])
def test_parse_level_rejects_everything_else(name: str) -> None:
    assert parse_level(name) is None


def _subscribe_off_loop(bus: LogBus, *, min_level: int = logging.NOTSET):
    """Subscribe from a throwaway loop, for the synchronous buffer tests."""
    result: list = []

    def run() -> None:
        async def go():
            result.append(bus.subscribe(min_level=min_level))

        asyncio.run(go())

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    return result[0]


# ---------------------------------------------------------------------
# The SSE route's body generator, driven directly: httpx's ASGITransport
# buffers a response fully before returning it, so an open event-stream can
# only be exercised at this level.
# ---------------------------------------------------------------------


async def _open_stream(bus: LogBus, level: str = "INFO"):
    from types import SimpleNamespace

    from zorkbot.admin.routes.logs import stream_logs

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ctx=SimpleNamespace(logbus=bus))))
    return await stream_logs(request, level=level, _entry={})


async def _take(response, count: int) -> list[str]:
    frames = []
    async for frame in response.body_iterator:
        frames.append(frame)
        if len(frames) >= count:
            break
    return frames


def _parse(frame: str) -> tuple[str, dict]:
    import json

    event, data = "message", ""
    for line in frame.split("\n"):
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data += line[5:].strip()
    return event, json.loads(data)


@pytest.mark.asyncio
async def test_stream_opens_with_a_hello_then_replays_the_buffer() -> None:
    log = _logger("zorkbot.routetest")
    bus = LogBus(buffer_size=50)
    bus.attach(log)
    try:
        log.info("a quiet line")
        log.error("a loud one")
        response = await _open_stream(bus)
        assert response.media_type == "text/event-stream"
        frames = await _take(response, 3)
    finally:
        bus.detach()

    assert _parse(frames[0])[0] == "hello"
    assert _parse(frames[0])[1] == {"root_level": "DEBUG", "buffer_size": 50, "replayed": 2}
    assert [_parse(f)[1]["message"] for f in frames[1:]] == ["a quiet line", "a loud one"]


@pytest.mark.asyncio
async def test_stream_delivers_records_logged_after_connecting() -> None:
    log = _logger("zorkbot.routelive")
    bus = LogBus(buffer_size=50)
    bus.attach(log)
    try:
        response = await _open_stream(bus)
        frames = await _take(response, 1)  # the hello
        log.warning("something happened")
        await asyncio.sleep(0)
        frames = await _take(response, 1)
    finally:
        bus.detach()

    event, data = _parse(frames[0])
    assert event == "log"
    assert data["message"] == "something happened"
    assert data["level"] == "WARNING"


@pytest.mark.asyncio
async def test_stream_level_filters_both_replay_and_live_records() -> None:
    log = _logger("zorkbot.routefilter")
    bus = LogBus(buffer_size=50)
    bus.attach(log)
    try:
        log.info("chatter")
        log.error("fault")
        response = await _open_stream(bus, level="ERROR")
        frames = await _take(response, 2)
        log.info("more chatter")
        log.critical("worse fault")
        await asyncio.sleep(0)
        frames += await _take(response, 1)
    finally:
        bus.detach()

    assert [_parse(f)[1]["message"] for f in frames[1:]] == ["fault", "worse fault"]


@pytest.mark.asyncio
async def test_stream_rejects_an_unknown_level() -> None:
    from fastapi import HTTPException

    bus = LogBus(buffer_size=10)
    with pytest.raises(HTTPException) as excinfo:
        await _open_stream(bus, level="TRACE")
    assert excinfo.value.status_code == 400


@pytest.mark.asyncio
async def test_stream_is_refused_past_the_concurrency_cap() -> None:
    from fastapi import HTTPException

    bus = LogBus(buffer_size=10, max_streams=1)
    # The route subscribes when the response is built, not when its body is
    # first read, so one open stream already fills the cap.
    await _open_stream(bus)
    with pytest.raises(HTTPException) as excinfo:
        await _open_stream(bus)
    assert excinfo.value.status_code == 429


@pytest.mark.asyncio
async def test_closing_the_stream_frees_its_slot() -> None:
    bus = LogBus(buffer_size=10, max_streams=1)
    response = await _open_stream(bus)
    await _take(response, 1)
    assert bus.can_subscribe() is False
    await response.body_iterator.aclose()
    assert bus.can_subscribe() is True
