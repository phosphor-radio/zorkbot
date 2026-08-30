"""Tests for MeshCoreRunner's event-to-IncomingMessage translation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from meshcore.events import Event, EventType

from zorkbot.runner import MeshCoreRunner

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
