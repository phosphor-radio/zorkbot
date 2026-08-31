"""Tests for ZorkBot dispatch logic."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import respx
import httpx

from zorkbot.advertiser import Advertiser
from zorkbot.bot import HELP_TEXT, ZorkBot
from zorkbot.commands.bots import REPLY_DELAY_SECONDS, REPLY_TEXT
from zorkbot.commands.rules import RULES_TEXT
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
        await bot.dispatch_channel(
            _channel_message("!author", channel_idx=config.channel.index), reply
        )
        await bot.drain()

    assert any("phosphor_radio" in r for r in replies)


@pytest.mark.asyncio
async def test_source_aliases_to_author() -> None:
    config = BotConfig()
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = _make_bot(config=config, game=game)
        await bot.dispatch_channel(
            _channel_message("!source", channel_idx=config.channel.index), reply
        )
        await bot.drain()

    assert any("phosphor_radio" in r for r in replies)


@pytest.mark.asyncio
async def test_author_not_supported_via_dm() -> None:
    config = BotConfig()
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = _make_bot(config=config, game=game)
        await bot.dispatch_dm(_dm_message("!author"), reply)
        await bot.drain()

    assert not any("phosphor_radio" in r for r in replies)
    assert any("Unknown command" in r for r in replies), f"Got: {replies}"


@pytest.mark.asyncio
async def test_uptime_command_on_channel() -> None:
    config = BotConfig()
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = _make_bot(config=config, game=game)
        await bot.dispatch_channel(
            _channel_message("!uptime", channel_idx=config.channel.index), reply
        )
        await bot.drain()

    assert any("Uptime" in r for r in replies), f"Got: {replies}"


@pytest.mark.asyncio
async def test_uptime_not_supported_via_dm() -> None:
    config = BotConfig()
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = _make_bot(config=config, game=game)
        await bot.dispatch_dm(_dm_message("!uptime"), reply)
        await bot.drain()

    assert not any("Uptime" in r for r in replies)
    assert any("Unknown command" in r for r in replies), f"Got: {replies}"


def test_help_text_mentions_author_not_source() -> None:
    assert "!author" in HELP_TEXT
    assert "!source" not in HELP_TEXT


def test_help_text_mentions_uptime() -> None:
    assert "!uptime" in HELP_TEXT


def test_dm_help_omits_author_and_uptime() -> None:
    from zorkbot.commands.zork import _HELP_PACKETS_DM, _HELP_PACKETS_IN_SESSION

    dm_text = "\n".join(_HELP_PACKETS_DM)
    in_session_text = "\n".join(_HELP_PACKETS_IN_SESSION)
    assert "!author" not in dm_text
    assert "!uptime" not in dm_text
    assert "!author" not in in_session_text
    assert "!uptime" not in in_session_text


@pytest.mark.asyncio
async def test_bots_command_replies_after_delay() -> None:
    config = BotConfig()
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    with patch("zorkbot.commands.bots.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        async with GameClient("http://game:8080") as game:
            bot = _make_bot(config=config, game=game)
            await bot.dispatch_dm(_dm_message("!bots"), reply)
            await bot.drain()
            # !bots replies via a fire-and-forget background task, not the
            # player's own command queue — drain() doesn't wait for it.
            await asyncio.gather(*bot._background_tasks)

    mock_sleep.assert_awaited_once_with(REPLY_DELAY_SECONDS)
    assert replies == [REPLY_TEXT]


@pytest.mark.asyncio
async def test_bots_command_on_channel() -> None:
    config = BotConfig()
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    with patch("zorkbot.commands.bots.asyncio.sleep", new=AsyncMock()):
        async with GameClient("http://game:8080") as game:
            bot = _make_bot(config=config, game=game)
            await bot.dispatch_channel(
                _channel_message("!bots", channel_idx=config.channel.index),
                reply,
            )
            await bot.drain()
            await asyncio.gather(*bot._background_tasks)

    assert replies == [REPLY_TEXT]


@pytest.mark.asyncio
@respx.mock
async def test_help_via_dm_in_active_session_includes_rules() -> None:
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
        replies.clear()

        await bot.dispatch_dm(_dm_message("!help"), reply)
        await bot.drain()

    assert any("!rules" in r for r in replies), f"Got: {replies}"


@pytest.mark.asyncio
async def test_help_via_dm_without_session_omits_rules() -> None:
    config = BotConfig()
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = _make_bot(config=config, game=game)
        await bot.dispatch_dm(_dm_message("!help"), reply)
        await bot.drain()

    assert not any("!rules" in r for r in replies), f"Got: {replies}"


@pytest.mark.asyncio
async def test_help_on_channel_omits_rules_even_with_session() -> None:
    """!rules is DM-only content — channel !help never shows it, regardless
    of session state, since the channel isn't where sessions are played."""
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

    assert not any("!rules" in r for r in replies), f"Got: {replies}"


@pytest.mark.asyncio
@respx.mock
async def test_rules_command_returns_zork_rules() -> None:
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
        replies.clear()

        await bot.dispatch_dm(_dm_message("!rules"), reply)
        await bot.drain()

    assert replies == [RULES_TEXT]


@pytest.mark.asyncio
async def test_rules_without_session_prompts_start() -> None:
    config = BotConfig()
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = _make_bot(config=config, game=game)
        await bot.dispatch_dm(_dm_message("!rules"), reply)
        await bot.drain()

    assert any("!start" in r for r in replies), f"Got: {replies}"


@pytest.mark.asyncio
async def test_rules_command_not_available_on_channel() -> None:
    config = BotConfig()
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = _make_bot(config=config, game=game)
        await bot.dispatch_channel(
            _channel_message("!rules", channel_idx=config.channel.index),
            reply,
        )
        await bot.drain()

    assert replies == ["Send !start and then DM me to play."]


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
async def test_start_sends_initial_look_via_dm() -> None:
    config = BotConfig(rate_limit_seconds=0.0)
    respx.post("http://game:8080/sessions").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.post(f"http://game:8080/sessions/{PLAYER_ID}/command").mock(
        return_value=httpx.Response(200, json={"ok": True, "output": "West of House.\n"})
    )
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = _make_bot(config=config, game=game)
        await bot.dispatch_dm(_dm_message("!start"), reply)
        await bot.drain()

    assert any("Session #1" in r for r in replies), f"Got: {replies}"
    assert any("West of House" in r for r in replies), f"Got: {replies}"


@pytest.mark.asyncio
@respx.mock
async def test_start_sends_initial_look_via_channel_dm() -> None:
    config = BotConfig(rate_limit_seconds=0.0)
    respx.post("http://game:8080/sessions").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.post(f"http://game:8080/sessions/{PLAYER_ID}/command").mock(
        return_value=httpx.Response(200, json={"ok": True, "output": "West of House.\n"})
    )
    channel_replies: list[str] = []

    async def reply(text: str) -> None:
        channel_replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = _make_bot(config=config, game=game)
        await bot.dispatch_channel(
            _channel_message("!start", channel_idx=config.channel.index), reply
        )
        await bot.drain()
        dm_texts = [call.args[1] for call in bot._send_dm.await_args_list]

    assert any("Session #1" in r for r in channel_replies), f"Got: {channel_replies}"
    assert any("West of House" in t for t in dm_texts), f"Got: {dm_texts}"


@pytest.mark.asyncio
@respx.mock
async def test_reset_sends_initial_look() -> None:
    config = BotConfig(rate_limit_seconds=0.0)
    respx.post("http://game:8080/sessions").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.delete(f"http://game:8080/sessions/{PLAYER_ID}/save").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.post(f"http://game:8080/sessions/{PLAYER_ID}/command").mock(
        return_value=httpx.Response(200, json={"ok": True, "output": "West of House.\n"})
    )
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = _make_bot(config=config, game=game)
        await bot.dispatch_dm(_dm_message("!start"), reply)
        await bot.drain()
        replies.clear()

        await bot.dispatch_dm(_dm_message("!reset"), reply)
        await bot.drain()

    assert any("started fresh" in r for r in replies), f"Got: {replies}"
    assert any("West of House" in r for r in replies), f"Got: {replies}"


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
