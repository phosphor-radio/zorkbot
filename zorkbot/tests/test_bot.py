"""Tests for ZorkBot dispatch logic."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import respx
import httpx

from zorkbot.advertiser import Advertiser
from zorkbot.bot import ZorkBot
from zorkbot.config import AdminConfig, BotConfig
from zorkbot.context import IncomingMessage
from zorkbot.game_client import GameClient

PLAYER_ID = "aabbccddeeff"
PLAYER_NAME = "Alice"


def _make_meshcore(contact=None):
    mc = MagicMock()
    mc.get_contact_by_key_prefix = MagicMock(return_value=contact)
    return mc


def _make_bot(config=None, game=None, meshcore=None):
    config = config or BotConfig()
    meshcore = meshcore or _make_meshcore(contact={"adv_name": PLAYER_NAME})
    advertiser = Advertiser()
    advertiser.send_if_due = AsyncMock()
    bot = ZorkBot(config, game, advertiser, meshcore)
    bot.set_send_dm(AsyncMock())
    return bot


def _dm_message(text: str, pubkey_prefix: str = PLAYER_ID) -> IncomingMessage:
    return IncomingMessage(
        text=text,
        sender_name=PLAYER_NAME,
        pubkey_prefix=pubkey_prefix,
        is_dm=True,
    )


def _channel_message(
    text: str,
    pubkey_prefix: str = PLAYER_ID,
    channel_idx: int = 1,
) -> IncomingMessage:
    return IncomingMessage(
        text=text,
        sender_name=PLAYER_NAME,
        pubkey_prefix=pubkey_prefix,
        is_dm=False,
        channel_idx=channel_idx,
    )


@pytest.mark.asyncio
async def test_help_command_on_channel() -> None:
    config = BotConfig()
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = _make_bot(config=config, game=game)
        await bot.dispatch_channel(
            _channel_message("!help", channel_idx=config.channel.index),
            reply,
        )
        await bot.drain()

    assert any("!start" in r for r in replies), f"Got: {replies}"


@pytest.mark.asyncio
async def test_help_command_via_dm() -> None:
    config = BotConfig()
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = _make_bot(config=config, game=game)
        await bot.dispatch_dm(_dm_message("!help"), reply)
        await bot.drain()

    assert any("!start" in r for r in replies)


@pytest.mark.asyncio
async def test_author_command() -> None:
    config = BotConfig()
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = _make_bot(config=config, game=game)
        await bot.dispatch_dm(_dm_message("!author"), reply)
        await bot.drain()

    assert any("phosphor_radio" in r for r in replies)


@pytest.mark.asyncio
@respx.mock
async def test_start_session_via_dm() -> None:
    config = BotConfig(rate_limit_seconds=0.0)
    respx.post("http://game:8080/sessions").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = _make_bot(config=config, game=game)
        await bot.dispatch_dm(_dm_message("!start"), reply)
        await bot.drain()

    assert any("Session #1" in r for r in replies), f"Got: {replies}"


@pytest.mark.asyncio
@respx.mock
async def test_game_command_in_dm() -> None:
    config = BotConfig(rate_limit_seconds=0.0)  # disable rate limiting in tests
    respx.post("http://game:8080/sessions").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.post(f"http://game:8080/sessions/{PLAYER_ID}/command").mock(
        return_value=httpx.Response(200, json={"ok": True, "output": "Taken.\n"})
    )
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = _make_bot(config=config, game=game)
        # Start session first.
        await bot.dispatch_dm(_dm_message("!start"), reply)
        await bot.drain()
        replies.clear()
        # Now send a bare game command.
        await bot.dispatch_dm(_dm_message("take lamp"), reply)
        await bot.drain()

    assert replies == ["Taken."]


@pytest.mark.asyncio
async def test_game_command_without_session_prompts_start() -> None:
    config = BotConfig()
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = _make_bot(config=config, game=game)
        await bot.dispatch_dm(_dm_message("take lamp"), reply)
        await bot.drain()

    assert any("!start" in r for r in replies)


@pytest.mark.asyncio
async def test_channel_message_ignores_other_channels() -> None:
    config = BotConfig()
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = _make_bot(config=config, game=game)
        await bot.dispatch_channel(
            _channel_message("!help", channel_idx=99),
            reply,
        )
        await bot.drain()

    assert replies == []


@pytest.mark.asyncio
async def test_rate_limiting() -> None:
    config = BotConfig(rate_limit_seconds=60.0)
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = _make_bot(config=config, game=game)
        await bot.dispatch_dm(_dm_message("!help"), reply)
        await bot.drain()
        await bot.dispatch_dm(_dm_message("!help"), reply)
        await bot.drain()

    assert any("Slow down" in r for r in replies)


@pytest.mark.asyncio
async def test_start_on_channel_without_contact_redirects() -> None:
    config = BotConfig()
    # Simulate player not in contacts.
    mc = _make_meshcore(contact=None)
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = _make_bot(config=config, game=game, meshcore=mc)
        await bot.dispatch_channel(
            _channel_message("!start", channel_idx=config.channel.index),
            reply,
        )
        await bot.drain()

    assert any("contacts" in r.lower() for r in replies), f"Got: {replies}"


@pytest.mark.asyncio
async def test_admin_end_requires_admin_pubkey() -> None:
    config = BotConfig()
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = _make_bot(config=config, game=game)
        # Pre-seed a session to try to force-end.
        bot._state.add_session("112233445566", "Bob")
        await bot.dispatch_dm(_dm_message("!end 1"), reply)
        await bot.drain()

    assert any("not authorized" in r.lower() for r in replies)
