import pytest
import respx
from httpx import Response

from zorkbot.bot import ZorkBot
from zorkbot.config import BotConfig
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

    assert replies == ["@[player] Taken."]


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

    assert any("!zork help" in line or "!help" in line for line in replies)


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

    assert replies == ["@[player] Taken."]
