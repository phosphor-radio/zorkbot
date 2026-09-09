"""Tests for MeshCoreRunner's event-to-IncomingMessage translation."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from meshcore.events import Event, EventType

import zorkbot.runner as runner_module
from zorkbot.channels import ChannelConfig
from zorkbot.config import BotConfig
from zorkbot.runner import apply_settings, flush_pending_messages, MeshCoreRunner

PUBKEY_PREFIX = "aabbccddeeff"
PLAYER_NAME = "Alice"


def _make_meshcore(contact=None, queued_messages=()):
    mc = MagicMock()
    mc.get_contact_by_name = MagicMock(return_value=contact)
    mc.get_contact_by_key_prefix = MagicMock(return_value=None)
    mc.ensure_contacts = AsyncMock()
    mc.start_auto_message_fetching = AsyncMock()
    # get_msg() drains the device's offline backlog one message at a time,
    # then reports NO_MORE_MSGS.
    mc.commands.get_msg = AsyncMock(
        side_effect=[*queued_messages, Event(EventType.NO_MORE_MSGS, {})]
    )
    return mc


def _make_runner(meshcore):
    bot = MagicMock()
    bot.config.channel.index = 1
    bot.config.announce_on_start = False
    bot.config.bots_enabled = False
    bot.config.bots_channel = None
    bot.dispatch_channel = AsyncMock()
    bot.dispatch_bots_channel = AsyncMock()
    bot.advertiser = MagicMock()
    return MeshCoreRunner(bot, meshcore)


@pytest.mark.asyncio
async def test_channel_msg_resolves_pubkey_prefix_from_contact_name():
    """CHANNEL_MSG_RECV carries no pubkey_prefix on the wire — only the
    "Name: text" convention identifies the sender. The runner must resolve
    identity via the contact table, or every channel command sees
    pubkey_prefix=None (see commands/start.py's "Cannot identify you")."""
    contact = {"adv_name": PLAYER_NAME, "public_key": PUBKEY_PREFIX + "0" * 52}
    meshcore = _make_meshcore(contact=contact)
    runner = _make_runner(meshcore)

    event = Event(
        EventType.CHANNEL_MSG_RECV,
        {"channel_idx": 1, "text": f"{PLAYER_NAME}: !start"},
    )
    await runner._on_channel_msg(event)

    meshcore.get_contact_by_name.assert_called_once_with(PLAYER_NAME)
    message = runner.bot.dispatch_channel.call_args[0][0]
    assert message.pubkey_prefix == PUBKEY_PREFIX
    assert message.sender_name == PLAYER_NAME
    assert message.text == "!start"


@pytest.mark.asyncio
async def test_channel_msg_unknown_sender_has_no_pubkey_prefix():
    """A sender name the bot has no contact for (no advert received yet)
    cannot be identified — this is the expected "not a contact" case, not
    a bug — commands/start.py surfaces a dedicated message for it."""
    meshcore = _make_meshcore(contact=None)
    runner = _make_runner(meshcore)

    event = Event(
        EventType.CHANNEL_MSG_RECV,
        {"channel_idx": 1, "text": "Stranger: !start"},
    )
    await runner._on_channel_msg(event)

    message = runner.bot.dispatch_channel.call_args[0][0]
    assert message.pubkey_prefix is None


@pytest.mark.asyncio
async def test_channel_rx_counted_only_when_the_bot_answers():
    """#zork and #bots carry conversation the bot has no part in. Counting
    all of it as "messages received" reads as bot load that never existed —
    only what the bot actually answered belongs in the stat."""
    meshcore = _make_meshcore(contact=None)
    runner = _make_runner(meshcore)
    runner.bot.dispatch_channel = AsyncMock(return_value=False)

    event = Event(
        EventType.CHANNEL_MSG_RECV,
        {"channel_idx": 1, "text": "Stranger: anyone around?"},
    )
    await runner._on_channel_msg(event)

    runner.bot.event_sink.message_rx.assert_not_called()
    # The radio view still shows it — that window is what is on the air.
    runner.bot.message_windows.record_rx.assert_called_once()

    runner.bot.dispatch_channel = AsyncMock(return_value=True)
    await runner._on_channel_msg(
        Event(EventType.CHANNEL_MSG_RECV, {"channel_idx": 1, "text": "Alice: !start"})
    )

    runner.bot.event_sink.message_rx.assert_called_once()
    kwargs = runner.bot.event_sink.message_rx.call_args.kwargs
    assert kwargs["transport"] == "channel"
    assert kwargs["channel_idx"] == 1
    assert kwargs["chars"] == len("!start")


@pytest.mark.asyncio
async def test_bots_channel_rx_counted_only_when_the_bot_answers():
    meshcore = _make_meshcore(contact=None)
    runner = _make_runner(meshcore)
    runner.bot.dispatch_bots_channel = AsyncMock(return_value=False)

    event = Event(EventType.CHANNEL_MSG_RECV, {"channel_idx": 2, "text": "Other: !bots"})
    await runner._on_bots_channel_msg(event)

    runner.bot.event_sink.message_rx.assert_not_called()

    runner.bot.dispatch_bots_channel = AsyncMock(return_value=True)
    await runner._on_bots_channel_msg(event)

    runner.bot.event_sink.message_rx.assert_called_once()


@pytest.mark.asyncio
async def test_dm_rx_always_counted():
    """A DM is addressed to the bot even when it answers nothing, so every
    one counts — the gate is for shared channels only."""
    meshcore = _make_meshcore(contact=None)
    runner = _make_runner(meshcore)
    runner.bot.dispatch_dm = AsyncMock(return_value=False)

    event = Event(
        EventType.CONTACT_MSG_RECV,
        {"pubkey_prefix": PUBKEY_PREFIX, "text": "hello?"},
    )
    await runner._on_dm_msg(event)

    runner.bot.event_sink.message_rx.assert_called_once()
    kwargs = runner.bot.event_sink.message_rx.call_args.kwargs
    assert kwargs["transport"] == "dm"
    assert kwargs["channel_idx"] is None
    assert kwargs["pubkey_prefix"] == PUBKEY_PREFIX


def _make_settings_meshcore(current_autoadd_config: int):
    mc = MagicMock()
    mc.commands.set_name = AsyncMock(return_value=Event(EventType.OK, {}))
    mc.commands.set_channel = AsyncMock(return_value=Event(EventType.OK, {}))
    mc.commands.get_autoadd_config = AsyncMock(
        return_value=Event(EventType.AUTOADD_CONFIG, {"config": current_autoadd_config})
    )
    mc.commands.set_autoadd_config = AsyncMock(return_value=Event(EventType.OK, {}))
    return mc


@pytest.mark.asyncio
async def test_apply_settings_enables_contact_overwrite_when_disabled():
    """The radio's 100-slot contact table silently drops new player adverts
    once full unless overwrite-oldest is enabled — off by default in
    firmware. zorkbot has no reason to prefer dropping new players over
    evicting stale contacts, so this must be turned on unconditionally at
    startup."""
    meshcore = _make_settings_meshcore(current_autoadd_config=0x00)

    await apply_settings(meshcore, BotConfig())

    meshcore.commands.set_autoadd_config.assert_awaited_once_with(0x01)


@pytest.mark.asyncio
async def test_apply_settings_preserves_other_autoadd_bits():
    """set_autoadd_config replaces the whole config byte on the device, so
    enabling overwrite-oldest must OR into the existing value rather than
    stomp any operator-configured auto-add-type restriction bits."""
    meshcore = _make_settings_meshcore(current_autoadd_config=0x04)  # AUTO_ADD_REPEATER

    await apply_settings(meshcore, BotConfig())

    meshcore.commands.set_autoadd_config.assert_awaited_once_with(0x05)


@pytest.mark.asyncio
async def test_apply_settings_skips_write_when_already_enabled():
    meshcore = _make_settings_meshcore(current_autoadd_config=0x01)

    await apply_settings(meshcore, BotConfig())

    meshcore.commands.set_autoadd_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_enables_live_contact_updates():
    """The library only refreshes its local contact cache once by default
    (on ensure_contacts()). An advert received after that marks the cache
    dirty but is never re-fetched unless auto_update_contacts is on, so a
    player who advertises after the bot has started stays invisible to
    get_contact_by_name/get_contact_by_key_prefix until the process
    restarts and takes a fresh snapshot. start() must turn this on so newly
    advertised players are recognized without a restart."""
    meshcore = _make_meshcore()
    runner = _make_runner(meshcore)

    await runner.start()

    assert meshcore.auto_update_contacts is True
    meshcore.ensure_contacts.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_does_not_subscribe_bots_channel_when_disabled():
    meshcore = _make_meshcore()
    runner = _make_runner(meshcore)

    await runner.start()

    assert runner._bots_channel_sub is None


@pytest.mark.asyncio
async def test_start_subscribes_bots_channel_when_enabled():
    meshcore = _make_meshcore()
    runner = _make_runner(meshcore)
    runner.bot.config.bots_enabled = True
    runner.bot.config.bots_channel = ChannelConfig(index=3, name="#bots")

    await runner.start()

    assert runner._bots_channel_sub is not None
    event = Event(
        EventType.CHANNEL_MSG_RECV,
        {"channel_idx": 3, "text": "Stranger: !bots"},
    )
    await runner._on_bots_channel_msg(event)
    message = runner.bot.dispatch_bots_channel.call_args[0][0]
    assert message.text == "!bots"
    assert message.channel_idx == 3


@pytest.mark.asyncio
async def test_apply_settings_configures_bots_channel_when_enabled():
    meshcore = _make_settings_meshcore(current_autoadd_config=0x01)
    config = BotConfig()
    config.bots_enabled = True
    config.bots_channel = ChannelConfig(index=3, name="#bots")

    await apply_settings(meshcore, config)

    meshcore.commands.set_channel.assert_any_await(3, "#bots", None)


@pytest.mark.asyncio
async def test_apply_settings_skips_bots_channel_when_disabled():
    meshcore = _make_settings_meshcore(current_autoadd_config=0x01)

    await apply_settings(meshcore, BotConfig())

    meshcore.commands.set_channel.assert_awaited_once()


def _queued_channel_msg(text: str) -> Event:
    return Event(EventType.CHANNEL_MSG_RECV, {"channel_idx": 1, "text": text})


@pytest.mark.asyncio
async def test_start_flushes_backlog_before_subscribing():
    """The radio holds every message received while no client was attached
    and replays the lot on connect. If the handlers are subscribed first,
    the bot answers commands sent hours ago the instant it starts up, so
    start() must drain the queue while nothing is listening."""
    queued = [_queued_channel_msg(f"{PLAYER_NAME}: !start") for _ in range(3)]
    meshcore = _make_meshcore(queued_messages=queued)
    runner = _make_runner(meshcore)

    await runner.start()

    # Three backlog messages plus the NO_MORE_MSGS that ends the drain.
    assert meshcore.commands.get_msg.await_count == 4
    runner.bot.dispatch_channel.assert_not_awaited()
    assert runner._channel_sub is not None
    # Reported to the admin UI's /api/status.
    runner.bot.set_startup_flushed_messages.assert_called_once_with(3)


@pytest.mark.asyncio
async def test_flush_stops_on_error():
    """A get_msg() timeout or device error comes back as ERROR, not
    NO_MORE_MSGS — treating it as anything but the end of the queue would
    spin startup forever against an unresponsive radio."""
    meshcore = MagicMock()
    meshcore.commands.get_msg = AsyncMock(
        side_effect=[
            _queued_channel_msg("Alice: hello"),
            Event(EventType.ERROR, {"reason": "timeout"}),
            Event(EventType.NO_MORE_MSGS, {}),
        ]
    )

    flushed = await flush_pending_messages(meshcore)

    assert flushed == 1
    assert meshcore.commands.get_msg.await_count == 2


@pytest.mark.asyncio
async def test_flush_gives_up_after_cap():
    """A device that never reports NO_MORE_MSGS (or a mesh busy enough to
    refill the queue as fast as it drains) must not wedge startup."""
    meshcore = MagicMock()
    meshcore.commands.get_msg = AsyncMock(
        return_value=_queued_channel_msg("Alice: hello")
    )

    flushed = await flush_pending_messages(meshcore)

    assert flushed == runner_module._MAX_FLUSH_MESSAGES
    assert meshcore.commands.get_msg.await_count == runner_module._MAX_FLUSH_MESSAGES


# ----------------------------------------------------------------------
# Send gate: spacing, the post-channel-receive guard, adverts, overflow
# ----------------------------------------------------------------------


def _make_gate_runner(
    spacing: float = 0.0,
    guard: float = 0.0,
    max_depth: int = 64,
    dm_ack: bool = True,
    contact: dict | None = None,
):
    """A runner whose config carries real numbers, for exercising the gate."""
    mc = MagicMock()
    mc.commands.send_msg = AsyncMock(return_value="ok")
    mc.commands.send_msg_with_retry = AsyncMock(return_value="ok")
    mc.commands.send_chan_msg = AsyncMock(return_value="ok")
    mc.commands.send_advert = AsyncMock(return_value="ok")
    mc.get_contact_by_key_prefix = MagicMock(return_value=contact)
    bot = MagicMock()
    bot.config.send_spacing_seconds = spacing
    bot.config.channel_rx_guard_seconds = guard
    bot.config.max_send_queue_depth = max_depth
    bot.config.dm_ack_enabled = dm_ack
    bot.config.dm_ack_max_attempts = 3
    bot.config.dm_ack_max_flood_attempts = 2
    bot.config.dm_ack_flood_after = 2
    bot.config.dm_ack_timeout_seconds = 0.0
    bot.event_sink.message_tx = MagicMock()
    return MeshCoreRunner(bot, mc), mc


@pytest.mark.asyncio
async def test_spacing_is_shared_across_dm_and_channel() -> None:
    """One gate, one clock: a DM does not get to ignore the gap left by a
    channel message, or the other way round."""
    runner, mc = _make_gate_runner(spacing=0.05)
    loop = asyncio.get_running_loop()
    stamps: list[float] = []

    async def stamp(*args, **kwargs):
        stamps.append(loop.time())
        return "ok"

    mc.commands.send_msg_with_retry = AsyncMock(side_effect=stamp)
    mc.commands.send_chan_msg = AsyncMock(side_effect=stamp)

    await asyncio.gather(
        runner._send_dm(PUBKEY_PREFIX, "dm one"),
        runner._send_chan_msg(1, "chan one"),
        runner._send_dm(PUBKEY_PREFIX, "dm two"),
    )

    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(gaps) == 2
    assert all(gap >= 0.05 for gap in gaps), gaps


@pytest.mark.asyncio
async def test_adverts_wait_their_turn_on_the_send_gate() -> None:
    """An advert is RF too - and a flood advert reaches further than any
    message - so it must not transmit on top of one."""
    from zorkbot.advertiser import Advertiser

    runner, mc = _make_gate_runner(spacing=0.05)
    loop = asyncio.get_running_loop()
    stamps: list[tuple[str, float]] = []

    async def msg(*args, **kwargs):
        stamps.append(("msg", loop.time()))
        return "ok"

    async def advert(*args, **kwargs):
        stamps.append(("advert", loop.time()))
        return "ok"

    mc.commands.send_msg_with_retry = AsyncMock(side_effect=msg)
    mc.commands.send_advert = AsyncMock(side_effect=advert)

    advertiser = Advertiser(enabled=True, cooldown_seconds=0)
    advertiser.set_transmit(runner.send_advert)

    await asyncio.gather(
        runner._send_dm(PUBKEY_PREFIX, "one"),
        advertiser.send_if_due(mc),
        runner._send_dm(PUBKEY_PREFIX, "two"),
    )

    assert any(kind == "advert" for kind, _ in stamps), stamps
    times = [t for _, t in stamps]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(gap >= 0.05 for gap in gaps), stamps


@pytest.mark.asyncio
async def test_advert_falls_back_to_the_radio_when_no_gate_is_wired() -> None:
    """Simulate mode has no runner to inject a gate, and must still work."""
    from zorkbot.advertiser import Advertiser

    mc = MagicMock()
    mc.commands.send_advert = AsyncMock(return_value="ok")
    advertiser = Advertiser(enabled=True, cooldown_seconds=0)

    await advertiser.send_if_due(mc)

    mc.commands.send_advert.assert_awaited_once()


@pytest.mark.asyncio
async def test_channel_receive_holds_off_the_next_transmission() -> None:
    """The first reply packet is the one with no spacing in front of it, so
    the guard is measured from the inbound flood instead."""
    runner, _ = _make_gate_runner(spacing=0.0, guard=0.2)
    loop = asyncio.get_running_loop()

    runner._note_channel_rx()
    started = loop.time()
    await runner._send_chan_msg(1, "reply")

    assert loop.time() - started >= 0.2


@pytest.mark.asyncio
async def test_dm_traffic_is_not_guarded_when_no_channel_message_arrived() -> None:
    """A DM is addressed, not flooded, so nothing repeats it across the mesh
    and there is nothing to wait out."""
    runner, _ = _make_gate_runner(spacing=0.0, guard=0.5)
    loop = asyncio.get_running_loop()

    started = loop.time()
    await runner._send_dm(PUBKEY_PREFIX, "reply")

    assert loop.time() - started < 0.5


@pytest.mark.asyncio
async def test_zero_guard_disables_the_hold_off() -> None:
    runner, _ = _make_gate_runner(spacing=0.0, guard=0.0)
    loop = asyncio.get_running_loop()

    runner._note_channel_rx()
    started = loop.time()
    await runner._send_chan_msg(1, "reply")

    assert loop.time() - started < 0.1


@pytest.mark.asyncio
async def test_overflow_drop_does_not_leak_the_send_coroutine() -> None:
    """The dropped packet's coroutine is built by the caller. Left unawaited
    it warns at collection time - noise in exactly the log being read to
    understand the overflow."""
    import gc
    import warnings

    runner, _ = _make_gate_runner(max_depth=0)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = await runner._send_dm(PUBKEY_PREFIX, "dropped")
        gc.collect()

    assert result is False
    assert not [w for w in caught if "never awaited" in str(w.message)], [
        str(w.message) for w in caught
    ]


@pytest.mark.asyncio
async def test_guard_and_spacing_are_independent_deadlines() -> None:
    """Spacing 5, guard 2, last TX at t=0, channel message at t=1.

    Both deadlines are enforced and neither overrides the other: spacing
    lands at t=5, the guard at t=3, so the packet goes at t=5 - the guard
    satisfied on the way past rather than ignored.
    """
    runner, _ = _make_gate_runner(spacing=0.5, guard=0.2)
    loop = asyncio.get_running_loop()

    now = loop.time()
    runner._last_send_at = now
    runner._last_channel_rx_at = now + 0.1

    started = loop.time()
    await runner._wait_for_quiet_air()
    waited = loop.time() - started

    assert 0.5 <= waited < 0.7, waited


@pytest.mark.asyncio
async def test_guard_governs_alone_when_nothing_was_just_transmitted() -> None:
    """Same guard, but with an idle transmitter there is no spacing deadline
    to wait on, so the reply goes out as soon as the flood has settled."""
    runner, _ = _make_gate_runner(spacing=0.5, guard=0.2)
    loop = asyncio.get_running_loop()

    runner._last_send_at = None
    runner._last_channel_rx_at = loop.time()

    started = loop.time()
    await runner._wait_for_quiet_air()
    waited = loop.time() - started

    assert 0.2 <= waited < 0.4, waited


@pytest.mark.asyncio
async def test_a_channel_message_arriving_mid_wait_re_arms_the_guard() -> None:
    """The deadline is re-checked after sleeping, so a flood that starts
    while a packet is already queued still gets its quiet period."""
    runner, _ = _make_gate_runner(spacing=0.0, guard=0.2)
    loop = asyncio.get_running_loop()

    runner._note_channel_rx()

    async def second_message() -> None:
        await asyncio.sleep(0.15)
        runner._note_channel_rx()

    started = loop.time()
    await asyncio.gather(runner._wait_for_quiet_air(), second_message())
    waited = loop.time() - started

    # Without the re-check this returns at ~0.2; the second message pushes
    # the deadline out to ~0.35.
    assert waited >= 0.34, waited


# ----------------------------------------------------------------------
# DM delivery ACKs: player DMs are ACK-waited and retried, watcher
# fan-out is not (docs/specs/dm-ack-retry.md)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_player_dm_waits_for_the_ack() -> None:
    """send_msg alone only reports that the radio accepted the frame."""
    runner, mc = _make_gate_runner()

    delivered = await runner._send_dm(PUBKEY_PREFIX, "you are in a maze")

    assert delivered is True
    mc.commands.send_msg_with_retry.assert_awaited_once()
    mc.commands.send_msg.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_wait_is_floored_at_the_send_spacing() -> None:
    """The ACK wait is the gap in front of the next retransmission, so a
    retry cannot outpace the spacing every other packet obeys."""
    runner, mc = _make_gate_runner(spacing=2.0)

    await runner._send_dm(PUBKEY_PREFIX, "hello")

    kwargs = mc.commands.send_msg_with_retry.await_args.kwargs
    assert kwargs["min_timeout"] == 2.0
    assert kwargs["max_attempts"] == 3
    assert kwargs["max_flood_attempts"] == 2
    assert kwargs["flood_after"] == 2


@pytest.mark.asyncio
async def test_dm_destination_prefers_the_known_contact() -> None:
    """A 12-char prefix cannot express a path, so the library would assume
    flood and never re-route. The contact carries the full key."""
    contact = {"public_key": "aa" * 32, "out_path_len": 2}
    runner, mc = _make_gate_runner(contact=contact)

    await runner._send_dm(PUBKEY_PREFIX, "hello")

    assert mc.commands.send_msg_with_retry.await_args.args[0] is contact


@pytest.mark.asyncio
async def test_dm_destination_falls_back_to_the_prefix() -> None:
    """An unknown contact must not fail the send - it just means today's
    conservative assume-flood behaviour."""
    runner, mc = _make_gate_runner(contact=None)

    await runner._send_dm(PUBKEY_PREFIX, "hello")

    assert mc.commands.send_msg_with_retry.await_args.args[0] == PUBKEY_PREFIX


@pytest.mark.asyncio
async def test_acknowledged_dm_is_recorded_as_delivered() -> None:
    runner, _ = _make_gate_runner()

    await runner._send_dm(PUBKEY_PREFIX, "hello")

    assert runner.bot.event_sink.message_tx.call_args.kwargs["acked"] is True


@pytest.mark.asyncio
async def test_unacknowledged_dm_is_reported_and_recorded(caplog) -> None:
    """send_msg_with_retry returns None when no ACK ever arrived."""
    runner, mc = _make_gate_runner()
    mc.commands.send_msg_with_retry = AsyncMock(return_value=None)

    with caplog.at_level("WARNING"):
        delivered = await runner._send_dm(PUBKEY_PREFIX, "hello")

    assert delivered is False
    assert runner.bot.event_sink.message_tx.call_args.kwargs["acked"] is False
    assert "not acknowledged" in caplog.text


@pytest.mark.asyncio
async def test_overflow_drop_is_not_recorded_as_a_delivery_failure() -> None:
    """A packet dropped before it reached the air was never transmitted,
    which is a different fact from transmitted-and-not-acknowledged."""
    runner, _ = _make_gate_runner(max_depth=0)

    await runner._send_dm(PUBKEY_PREFIX, "dropped")

    kwargs = runner.bot.event_sink.message_tx.call_args.kwargs
    assert kwargs["dropped"] is True
    assert kwargs.get("acked") is None


@pytest.mark.asyncio
async def test_dm_ack_disabled_restores_the_plain_send() -> None:
    runner, mc = _make_gate_runner(dm_ack=False)

    delivered = await runner._send_dm(PUBKEY_PREFIX, "hello")

    assert delivered is True
    mc.commands.send_msg.assert_awaited_once()
    mc.commands.send_msg_with_retry.assert_not_awaited()
    assert runner.bot.event_sink.message_tx.call_args.kwargs["acked"] is None


@pytest.mark.asyncio
async def test_watcher_dm_is_never_ack_waited() -> None:
    """Watcher output is a courtesy stream about someone else's game, and
    fan-out is where ACK-waiting would cost the most airtime."""
    runner, mc = _make_gate_runner()

    await runner._send_watcher_dm(PUBKEY_PREFIX, "[Alice] > north")

    mc.commands.send_msg.assert_awaited_once()
    mc.commands.send_msg_with_retry.assert_not_awaited()
    assert runner.bot.event_sink.message_tx.call_args.kwargs["acked"] is None


@pytest.mark.asyncio
async def test_watcher_dm_still_goes_through_the_send_gate() -> None:
    """Not ACK-waited is not the same as not spaced."""
    runner, mc = _make_gate_runner(spacing=0.05)
    loop = asyncio.get_running_loop()
    stamps: list[float] = []

    async def stamp(*args, **kwargs):
        stamps.append(loop.time())
        return "ok"

    mc.commands.send_msg = AsyncMock(side_effect=stamp)

    await asyncio.gather(
        runner._send_watcher_dm(PUBKEY_PREFIX, "one"),
        runner._send_watcher_dm(PUBKEY_PREFIX, "two"),
    )

    assert stamps[1] - stamps[0] >= 0.05, stamps


@pytest.mark.asyncio
async def test_a_slow_retry_does_not_let_the_next_packet_interleave() -> None:
    """The lock is held across the ACK wait on purpose: the radio is
    half-duplex, so transmitting into the ACK window is transmitting on top
    of the ACK the bot is waiting to hear."""
    runner, mc = _make_gate_runner(spacing=0.0)
    events: list[str] = []

    async def slow_retry(*args, **kwargs):
        events.append("start")
        await asyncio.sleep(0.1)
        events.append("end")
        return "ok"

    mc.commands.send_msg_with_retry = AsyncMock(side_effect=slow_retry)

    await asyncio.gather(
        runner._send_dm(PUBKEY_PREFIX, "one"),
        runner._send_dm(PUBKEY_PREFIX, "two"),
    )

    assert events == ["start", "end", "start", "end"], events


@pytest.mark.asyncio
async def test_channel_sends_and_adverts_record_no_delivery(caplog) -> None:
    """Neither has a per-recipient ACK to wait for."""
    runner, _ = _make_gate_runner()

    await runner._send_chan_msg(1, "hello channel")
    assert runner.bot.event_sink.message_tx.call_args.kwargs["acked"] is None

    runner.bot.event_sink.message_tx.reset_mock()
    with caplog.at_level("WARNING"):
        await runner.send_advert(flood=True)

    runner.bot.event_sink.message_tx.assert_not_called()
    assert "not acknowledged" not in caplog.text


@pytest.mark.asyncio
async def test_start_injects_both_senders() -> None:
    """The bot needs to be able to tell player traffic from fan-out."""
    mc = _make_meshcore()
    bot = MagicMock()
    bot.config = BotConfig(channel=ChannelConfig(index=1, name="zork"))
    bot.set_send_dm = MagicMock()
    bot.set_send_watcher_dm = MagicMock()

    runner = MeshCoreRunner(bot, mc)
    await runner.start()

    assert bot.set_send_dm.call_args.args[0] == runner._send_dm_packets
    assert bot.set_send_watcher_dm.call_args.args[0] == runner._send_watcher_dm


@pytest.mark.asyncio
async def test_dm_reply_closure_reports_delivery() -> None:
    """The bot's reply path is what carries the ACK result back up to
    Context.reply_many, which is what decides to abandon a response."""
    runner, mc = _make_gate_runner()
    mc.commands.send_msg_with_retry = AsyncMock(return_value=None)
    runner.bot.dispatch_dm = AsyncMock()

    await runner._on_dm_msg(
        Event(EventType.CONTACT_MSG_RECV, {"pubkey_prefix": PUBKEY_PREFIX, "text": "look"})
    )

    reply = runner.bot.dispatch_dm.await_args.args[1]
    assert await reply("a packet") is False

    mc.commands.send_msg_with_retry = AsyncMock(return_value="ok")
    assert await reply("another") is True


@pytest.mark.asyncio
async def test_dm_reply_closure_without_a_sender_is_not_a_failure() -> None:
    """An unidentifiable sender means nothing was measured. False would
    truncate the rest of a response over a link that never failed."""
    runner, _ = _make_gate_runner()
    runner.bot.dispatch_dm = AsyncMock()

    await runner._on_dm_msg(Event(EventType.CONTACT_MSG_RECV, {"text": "look"}))

    reply = runner.bot.dispatch_dm.await_args.args[1]
    assert await reply("a packet") is True


@pytest.mark.asyncio
async def test_channel_reply_closure_never_reports_failure() -> None:
    """A broadcast has no per-recipient ACK, so nothing is measured."""
    runner, _ = _make_gate_runner()
    runner.bot.config.channel.index = 1
    runner.bot.dispatch_channel = AsyncMock()

    await runner._on_channel_msg(
        Event(EventType.CHANNEL_MSG_RECV, {"channel_idx": 1, "text": "!help"})
    )

    reply = runner.bot.dispatch_channel.await_args.args[1]
    assert await reply("a packet") is True


@pytest.mark.asyncio
async def test_transmissions_are_logged_at_debug(caplog) -> None:
    """The only per-packet record of an outgoing message in the log."""
    runner, _ = _make_gate_runner()

    with caplog.at_level("DEBUG", logger="zorkbot.runner"):
        await runner._send_dm(PUBKEY_PREFIX, "hello")
        await runner._send_watcher_dm(PUBKEY_PREFIX, "[Alice] > north")
        await runner._send_chan_msg(1, "hello channel")

    lines = [r.message for r in caplog.records if r.message.startswith("tx ")]
    assert len(lines) == 3, lines
    assert "player=aabbccdd" in lines[0] and "acked=True" in lines[0]
    # Watcher fan-out and channel traffic are transmitted but not measured.
    assert "acked=None" in lines[1]
    assert "channel=1" in lines[2] and "acked=None" in lines[2]


@pytest.mark.asyncio
async def test_transmissions_are_not_logged_at_info(caplog) -> None:
    """A line per packet per recipient is not what an INFO log is for."""
    runner, _ = _make_gate_runner()

    with caplog.at_level("INFO", logger="zorkbot.runner"):
        await runner._send_dm(PUBKEY_PREFIX, "hello")

    assert not [r for r in caplog.records if r.message.startswith("tx ")]


@pytest.mark.asyncio
async def test_adverts_are_not_double_logged(caplog) -> None:
    """The advertiser logs those itself, at INFO."""
    runner, _ = _make_gate_runner()

    with caplog.at_level("DEBUG", logger="zorkbot.runner"):
        await runner.send_advert(flood=True)

    assert not [r for r in caplog.records if r.message.startswith("tx ")]


@pytest.mark.asyncio
async def test_dropped_packet_is_not_logged_as_transmitted(caplog) -> None:
    runner, _ = _make_gate_runner(max_depth=0)

    with caplog.at_level("DEBUG", logger="zorkbot.runner"):
        await runner._send_dm(PUBKEY_PREFIX, "dropped")

    assert not [r for r in caplog.records if r.message.startswith("tx ")]
    assert "send queue overflow" in caplog.text
