"""Tests for MeshCoreRunner's event-to-IncomingMessage translation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from meshcore.events import Event, EventType

from zorkbot.config import BotConfig
from zorkbot.runner import apply_settings, MeshCoreRunner

PUBKEY_PREFIX = "aabbccddeeff"
PLAYER_NAME = "Alice"


def _make_meshcore(contact=None):
    mc = MagicMock()
    mc.get_contact_by_name = MagicMock(return_value=contact)
    mc.get_contact_by_key_prefix = MagicMock(return_value=None)
    return mc


def _make_runner(meshcore):
    bot = MagicMock()
    bot.config.channel.index = 1
    bot.dispatch_channel = AsyncMock()
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
