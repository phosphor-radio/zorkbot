import asyncio
import json
from unittest.mock import patch

import pytest
import respx
from httpx import Response

from zorkbot.bot import ZorkBot
from zorkbot.config import AdminConfig, BotConfig
from zorkbot.context import IncomingMessage
from zorkbot.game_client import GameClient


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_game_command() -> None:
    config = BotConfig()
    respx.post("http://game:8080/command").mock(
        return_value=Response(200, json={"ok": True, "output": "Taken.\n"})
    )

    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = ZorkBot(config, game)
        await bot.dispatch(
            IncomingMessage(
                text="!zork take lamp",
                sender_name="player",
                channel_idx=config.channel.index,
            ),
            reply,
        )
        await bot.drain()

    assert replies == ["Taken."]


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_ignores_other_channels() -> None:
    config = BotConfig()
    route = respx.post("http://game:8080/command").mock(
        return_value=Response(200, json={"ok": True, "output": "Nope\n"})
    )
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = ZorkBot(config, game)
        await bot.dispatch(
            IncomingMessage(
                text="!zork look",
                sender_name="player",
                channel_idx=99,
            ),
            reply,
        )
        await bot.drain()

    assert replies == []
    assert not route.called


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_help_command() -> None:
    config = BotConfig()
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = ZorkBot(config, game)
        await bot.dispatch(
            IncomingMessage(
                text="!help",
                sender_name="player",
                channel_idx=config.channel.index,
            ),
            reply,
        )
        await bot.drain()

    assert any("!zork help" in line or "!help" in line for line in replies)


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_author_command() -> None:
    config = BotConfig()
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = ZorkBot(config, game)
        await bot.dispatch(
            IncomingMessage(
                text="!author",
                sender_name="player",
                channel_idx=config.channel.index,
            ),
            reply,
        )
        await bot.drain()

    assert replies == [
        "Meshcore: phr5🐧\nDiscord: @phosphor_radio\n"
        "Source: https://github.com/phosphor-radio/zorkbot"
    ]


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_mentioned_command() -> None:
    config = BotConfig()
    respx.post("http://game:8080/command").mock(
        return_value=Response(200, json={"ok": True, "output": "Taken.\n"})
    )
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = ZorkBot(config, game)
        await bot.dispatch(
            IncomingMessage(
                text="@[zorkbot] !zork take lamp",
                sender_name="player",
                channel_idx=config.channel.index,
            ),
            reply,
        )
        await bot.drain()

    assert replies == ["Taken."]


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_quit_requires_admin() -> None:
    config = BotConfig()
    route = respx.post("http://game:8080/command").mock(
        return_value=Response(200, json={"ok": True, "output": "Goodbye.\n"})
    )
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = ZorkBot(config, game)
        await bot.dispatch(
            IncomingMessage(
                text="!zork quit",
                sender_name="player",
                channel_idx=config.channel.index,
            ),
            reply,
        )
        await bot.drain()

    assert replies == ["You are not authorized for that command."]
    assert not route.called


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_quit_allows_admin() -> None:
    config = BotConfig(admin=AdminConfig(names=["admin"]))
    route = respx.post("http://game:8080/command").mock(
        return_value=Response(200, json={"ok": True, "output": "Goodbye.\n"})
    )
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = ZorkBot(config, game)
        await bot.dispatch(
            IncomingMessage(
                text="!zork quit",
                sender_name="admin",
                channel_idx=config.channel.index,
            ),
            reply,
        )
        await bot.drain()

    assert replies == ["Goodbye."]
    assert route.called
    assert json.loads(route.calls.last.request.content) == {
        "text": "quit",
        "admin": True,
    }


@pytest.mark.asyncio
@respx.mock
async def test_dispatch_rate_limits_sender() -> None:
    config = BotConfig(rate_limit_seconds=60.0)
    respx.post("http://game:8080/command").mock(
        return_value=Response(200, json={"ok": True, "output": "Ok.\n"})
    )
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    async with GameClient("http://game:8080") as game:
        bot = ZorkBot(config, game)
        await bot.dispatch(
            IncomingMessage(
                text="!zork look",
                sender_name="player",
                channel_idx=config.channel.index,
            ),
            reply,
        )
        await bot.drain()
        await bot.dispatch(
            IncomingMessage(
                text="!zork look",
                sender_name="player",
                channel_idx=config.channel.index,
            ),
            reply,
        )

    assert replies == ["Ok.", "Slow down — try again in a moment."]


@pytest.mark.asyncio
async def test_dispatch_queue_full_replies_busy() -> None:
    config = BotConfig(command_queue_size=1)
    hold = asyncio.Event()

    async def slow_handle(ctx, game, game_lock) -> None:
        await hold.wait()
        await ctx.reply("Ok.")

    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    with patch("zorkbot.bot.handle_zork", side_effect=slow_handle):
        async with GameClient("http://game:8080") as game:
            bot = ZorkBot(config, game)
            bot.start()
            await bot.dispatch(
                IncomingMessage(
                    text="!zork look",
                    sender_name="alice",
                    channel_idx=config.channel.index,
                ),
                reply,
            )
            await asyncio.sleep(0)
            await bot.dispatch(
                IncomingMessage(
                    text="!zork look",
                    sender_name="bob",
                    channel_idx=config.channel.index,
                ),
                reply,
            )
            await asyncio.sleep(0)
            await bot.dispatch(
                IncomingMessage(
                    text="!zork look",
                    sender_name="carol",
                    channel_idx=config.channel.index,
                ),
                reply,
            )
            hold.set()
            await bot.drain()

    assert "The game is busy, try again." in replies
