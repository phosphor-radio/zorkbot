"""Tests for ZorkBot dispatch logic."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import respx
import httpx

from zorkbot.advertiser import Advertiser
from zorkbot.bot import HELP_TEXT, ZorkBot
from zorkbot.channels import ChannelConfig
from zorkbot.commands.bots import build_reply_text
from zorkbot.commands.rules import RULES_TEXT
from zorkbot.config import AdminConfig, BotConfig
from zorkbot.context import IncomingMessage
from zorkbot.game_client import GameClient, GameServiceError, SessionInfo

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


def _bots_enabled_config(bots_index: int = 2) -> BotConfig:
    config = BotConfig()
    config.bots_enabled = True
    config.bots_channel = ChannelConfig(index=bots_index, name="#bots")
    return config


@pytest.mark.asyncio
async def test_bots_command_on_bots_channel() -> None:
    config = _bots_enabled_config()
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    with patch("zorkbot.commands.bots.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        async with GameClient("http://game:8080") as game:
            bot = _make_bot(config=config, game=game)
            await bot.dispatch_bots_channel(
                _channel_message("!bots", channel_idx=config.bots_channel.index),
                reply,
            )
            # !bots replies via a fire-and-forget background task.
            await asyncio.gather(*bot._background_tasks)

    mock_sleep.assert_awaited_once()
    delay = mock_sleep.await_args.args[0]
    assert 5.0 <= delay <= 10.0
    assert replies == [build_reply_text(config.channel.name)]


@pytest.mark.asyncio
async def test_bots_command_ignored_when_disabled() -> None:
    config = BotConfig()  # bots_enabled=False by default
    config.bots_channel = ChannelConfig(index=2, name="#bots")
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = _make_bot(config=config, game=game)
        await bot.dispatch_bots_channel(
            _channel_message("!bots", channel_idx=2), reply
        )

    assert replies == []
    assert not bot._background_tasks


@pytest.mark.asyncio
async def test_bots_command_ignored_on_wrong_channel() -> None:
    config = _bots_enabled_config(bots_index=2)
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = _make_bot(config=config, game=game)
        # Message arrives on a different channel than config.bots_channel.
        await bot.dispatch_bots_channel(
            _channel_message("!bots", channel_idx=99), reply
        )

    assert replies == []
    assert not bot._background_tasks


@pytest.mark.asyncio
async def test_bots_command_not_supported_via_dm() -> None:
    config = _bots_enabled_config()
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = _make_bot(config=config, game=game)
        await bot.dispatch_dm(_dm_message("!bots"), reply)
        await bot.drain()

    assert any("Unknown command" in r for r in replies), f"Got: {replies}"


@pytest.mark.asyncio
async def test_bots_command_not_supported_on_zork_channel() -> None:
    config = _bots_enabled_config()
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = _make_bot(config=config, game=game)
        await bot.dispatch_channel(
            _channel_message("!bots", channel_idx=config.channel.index), reply
        )
        await bot.drain()

    assert replies == ["Send !start and then DM me to play."]


@pytest.mark.asyncio
@respx.mock
async def test_help_via_dm_in_active_session_includes_rules() -> None:
    config = BotConfig()
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
    config = BotConfig()
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
    config = BotConfig()
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
    config = BotConfig()
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
    config = BotConfig()
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
    config = BotConfig()
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
    config = BotConfig()
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
@respx.mock
async def test_watcher_sees_echo_title_and_description_as_separate_lines() -> None:
    """A watcher's relay includes a "[Name] > command" echo line ahead of
    the game's own output — this must not defeat title/line-break detection
    on the real output, which only looks at that output's own first line."""
    watcher_id = "112233445566"
    config = BotConfig()
    respx.post("http://game:8080/sessions").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.post(f"http://game:8080/sessions/{PLAYER_ID}/command").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "output": "North of House\nYou are facing the north side of a white house.",
            },
        )
    )

    async def reply(text: str) -> None:
        pass

    async with GameClient("http://game:8080") as game:
        bot = _make_bot(config=config, game=game)
        await bot.dispatch_dm(_dm_message("!start"), reply)
        await bot.drain()
        await bot.dispatch_dm(_dm_message("!watch 1", pubkey_prefix=watcher_id), reply)
        await bot.drain()
        bot._send_dm.reset_mock()

        await bot.dispatch_dm(_dm_message("north"), reply)
        await bot.drain()

    dm_texts = [call.args[1] for call in bot._send_dm.await_args_list]
    assert dm_texts == [
        "[Alice] > north\nNorth of House\nYou are facing the north side of a white house."
    ]


@pytest.mark.asyncio
@respx.mock
async def test_end_notifies_watchers_but_not_player_or_channel() -> None:
    watcher_id = "112233445566"
    config = BotConfig()
    respx.post("http://game:8080/sessions").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.delete(f"http://game:8080/sessions/{PLAYER_ID}").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = _make_bot(config=config, game=game)
        await bot.dispatch_dm(_dm_message("!start"), reply)
        await bot.drain()
        await bot.dispatch_dm(_dm_message("!watch 1", pubkey_prefix=watcher_id), reply)
        await bot.drain()
        bot._send_dm.reset_mock()
        replies.clear()

        await bot.dispatch_dm(_dm_message("!end"), reply)
        await bot.drain()

    # Player still gets their own end confirmation.
    assert replies == ["Zork I Session #1 saved and ended."]
    # Watcher gets a separate notification, and nothing else was sent.
    watcher_calls = bot._send_dm.await_args_list
    assert len(watcher_calls) == 1
    assert watcher_calls[0].args == (
        watcher_id,
        "Zork I Session #1 (Alice) has ended. You are no longer watching.",
    )
    # The watcher is no longer tracked as watching anything.
    assert bot._state.active_state(watcher_id) == "none"


@pytest.mark.asyncio
@respx.mock
async def test_end_without_watchers_sends_no_dm() -> None:
    config = BotConfig()
    respx.post("http://game:8080/sessions").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.delete(f"http://game:8080/sessions/{PLAYER_ID}").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    async def reply(text: str) -> None:
        pass

    async with GameClient("http://game:8080") as game:
        bot = _make_bot(config=config, game=game)
        await bot.dispatch_dm(_dm_message("!start"), reply)
        await bot.drain()
        bot._send_dm.reset_mock()

        await bot.dispatch_dm(_dm_message("!end"), reply)
        await bot.drain()

    bot._send_dm.assert_not_awaited()


@pytest.mark.asyncio
@respx.mock
async def test_admin_end_notifies_watchers() -> None:
    watcher_id = "112233445566"
    admin_id = "aaaaaaaaaaaa"
    config = BotConfig(
        admin=AdminConfig(pubkeys=[admin_id]),
    )
    respx.post("http://game:8080/sessions").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.delete(f"http://game:8080/sessions/{PLAYER_ID}").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    async def reply(text: str) -> None:
        pass

    async with GameClient("http://game:8080") as game:
        bot = _make_bot(config=config, game=game)
        await bot.dispatch_dm(_dm_message("!start"), reply)
        await bot.drain()
        await bot.dispatch_dm(_dm_message("!watch 1", pubkey_prefix=watcher_id), reply)
        await bot.drain()
        bot._send_dm.reset_mock()

        await bot.dispatch_dm(_dm_message("!end 1", pubkey_prefix=admin_id), reply)
        await bot.drain()

    watcher_calls = bot._send_dm.await_args_list
    assert len(watcher_calls) == 1
    assert watcher_calls[0].args == (
        watcher_id,
        "Zork I Session #1 (Alice) has ended. You are no longer watching.",
    )


@pytest.mark.asyncio
async def test_reconcile_sessions_notifies_watchers_of_server_side_timeout() -> None:
    """The game service has no way to push a notification when it ends a
    session on its own (inactivity timeout, PTY crash) — the bot's poller
    is the only way watchers learn about it."""
    watcher_id = "112233445566"
    config = BotConfig()

    async def reply(text: str) -> None:
        pass

    game = MagicMock()
    game.list_sessions = AsyncMock(return_value=[])  # server no longer has it

    bot = _make_bot(config=config, game=game)
    bot._state.add_session(PLAYER_ID, PLAYER_NAME)
    bot._state.add_watcher(watcher_id, 1)

    await bot._reconcile_sessions()

    game.list_sessions.assert_awaited_once()
    bot._send_dm.assert_awaited_once_with(
        watcher_id,
        "Zork I Session #1 (Alice) has ended. You are no longer watching.",
    )
    assert bot._state.get_session(PLAYER_ID) is None
    assert bot._state.active_state(watcher_id) == "none"


@pytest.mark.asyncio
async def test_reconcile_sessions_leaves_active_sessions_alone() -> None:
    config = BotConfig()
    game = MagicMock()
    game.list_sessions = AsyncMock(
        return_value=[SessionInfo(num=1, player_id=PLAYER_ID, started_at="")]
    )

    bot = _make_bot(config=config, game=game)
    bot._state.add_session(PLAYER_ID, PLAYER_NAME)

    await bot._reconcile_sessions()

    bot._send_dm.assert_not_awaited()
    assert bot._state.get_session(PLAYER_ID) is not None


@pytest.mark.asyncio
async def test_reconcile_sessions_survives_list_sessions_failure() -> None:
    """A transient game-service error must never be treated as "no
    sessions" — that would wrongly end and notify watchers of everyone."""
    config = BotConfig()
    game = MagicMock()
    game.list_sessions = AsyncMock(side_effect=GameServiceError("boom"))

    bot = _make_bot(config=config, game=game)
    bot._state.add_session(PLAYER_ID, PLAYER_NAME)

    await bot._reconcile_sessions()

    bot._send_dm.assert_not_awaited()
    assert bot._state.get_session(PLAYER_ID) is not None


def test_session_poller_zero_interval_is_noop() -> None:
    config = BotConfig(session_poll_seconds=0)
    game = MagicMock()
    bot = _make_bot(config=config, game=game)

    bot.start_session_poller()

    assert not bot._background_tasks


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


class _GatedReplies:
    """Collects replies, holding the first one open.

    The bot's "is a response still pending?" gate is only meaningful while a
    response is genuinely mid-flight, so block inside the first reply rather
    than racing the worker task.
    """

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.sending = asyncio.Event()
        self.release = asyncio.Event()
        self._held = False

    async def reply(self, text: str) -> None:
        self.texts.append(text)
        if not self._held:
            self._held = True
            self.sending.set()
            await self.release.wait()


async def _replies_for(*texts: str) -> list[str]:
    """Dispatch DMs one at a time, letting each finish before the next."""
    collected: list[str] = []

    async def reply(text: str) -> None:
        collected.append(text)

    async with GameClient("http://game:8080") as game:
        bot = _make_bot(game=game)
        for text in texts:
            await bot.dispatch_dm(_dm_message(text), reply)
            await bot.drain()
    return collected


async def _replies_during_pending(*texts: str) -> list[str]:
    """Send "!help", then dispatch `texts` while its response is still going out."""
    async with GameClient("http://game:8080") as game:
        bot = _make_bot(game=game)
        gate = _GatedReplies()

        await bot.dispatch_dm(_dm_message("!help"), gate.reply)
        await gate.sending.wait()  # the worker is now mid-response

        for text in texts:
            await bot.dispatch_dm(_dm_message(text), gate.reply)

        gate.release.set()
        await bot.drain()
    return gate.texts


@pytest.mark.asyncio
async def test_burst_while_response_pending_costs_nothing() -> None:
    """A flood arriving mid-response must add no packets at all.

    The bot answers it with silence rather than a throttle notice: every packet
    it emits takes a send_spacing_seconds transmit slot from a queue shared with
    every other player, so "slow down" costs the mesh as much as the reply it
    stands in for.
    """
    baseline = await _replies_for("!help")
    burst = await _replies_during_pending(*["!help"] * 10)

    assert burst == baseline


@pytest.mark.asyncio
async def test_player_who_waits_for_the_reply_is_never_dropped() -> None:
    """The gate is pending-state, not elapsed time, so back-to-back commands
    from someone reading their replies always go through — however fast."""
    baseline = await _replies_for("!help")
    repeated = await _replies_for("!help", "!help", "!help")

    assert repeated == baseline * 3


@pytest.mark.asyncio
async def test_end_is_exempt_while_a_response_is_pending() -> None:
    """!end is a player's only way out of a runaway session, so it is queued
    behind the in-flight response instead of being dropped with the rest."""
    baseline = await _replies_for("!help")
    with_end = await _replies_during_pending("!end")

    assert with_end[: len(baseline)] == baseline
    assert with_end[len(baseline):] == ["You don't have an active session or watch to end."]


@pytest.mark.asyncio
async def test_only_one_interrupt_may_wait() -> None:
    """Depth-1 queue: a second !end behind the first sends nothing, so !end
    cannot be used to walk straight through the gate."""
    one = await _replies_during_pending("!end")
    many = await _replies_during_pending(*["!end"] * 5)

    assert many == one


@pytest.mark.asyncio
async def test_bots_roll_call_answers_once_per_window() -> None:
    """!bots is a broadcast, so a flood of roll calls draws one reply for the
    channel — the cooldown is global, not per sender."""
    config = _bots_enabled_config()
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    with patch("zorkbot.commands.bots.asyncio.sleep", new=AsyncMock()):
        async with GameClient("http://game:8080") as game:
            bot = _make_bot(config=config, game=game)
            for pubkey in ("aabbccddeeff", "112233445566", "778899aabbcc"):
                await bot.dispatch_bots_channel(
                    _channel_message(
                        "!bots",
                        pubkey_prefix=pubkey,
                        channel_idx=config.bots_channel.index,
                    ),
                    reply,
                )
            if bot._background_tasks:
                await asyncio.gather(*bot._background_tasks)

    assert replies == [build_reply_text(config.channel.name)]


@pytest.mark.asyncio
async def test_bots_cooldown_must_outlast_the_collision_delay() -> None:
    """The window has to exceed handle_bots' 5-10s jittered reply delay, or a
    second roll call is admitted while the first reply is still unsent."""
    from zorkbot.commands.bots import (
        REPLY_DELAY_BASE_SECONDS,
        REPLY_DELAY_JITTER_SECONDS,
    )

    max_delay = REPLY_DELAY_BASE_SECONDS + REPLY_DELAY_JITTER_SECONDS
    assert BotConfig().bots_cooldown_seconds > max_delay


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
