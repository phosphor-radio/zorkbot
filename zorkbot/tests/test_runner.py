"""Tests for MeshCoreRunner's event-to-IncomingMessage translation."""

from __future__ import annotations

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
