"""Tests for the local development simulator."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from zorkbot.advertiser import Advertiser
from zorkbot.bot import ZorkBot
from zorkbot.config import BotConfig
from zorkbot.game_client import GameClient
from zorkbot.simulator import Simulator


def _make_bot(config: BotConfig, game: GameClient) -> ZorkBot:
    mc = MagicMock()
    mc.get_contact_by_key_prefix = MagicMock(return_value=None)
    advertiser = Advertiser()
    advertiser.send_if_due = AsyncMock()
    bot = ZorkBot(config, game, advertiser, mc)
    bot.set_send_dm(AsyncMock())
    return bot


@pytest.mark.asyncio
async def test_simulator_help() -> None:
    config = BotConfig(game_url="http://game:8080")
    async with GameClient(config.game_url) as game:
        sim = Simulator(_make_bot(config, game))
        lines = await sim.handle_line("!help")
    assert any("!start" in line.lower() for line in lines)


@pytest.mark.asyncio
async def test_simulator_dm_prefix() -> None:
    config = BotConfig(game_url="http://game:8080")
    async with GameClient(config.game_url) as game:
        sim = Simulator(_make_bot(config, game))
        lines = await sim.handle_line("dm:take lamp")
    # Without a session, should get a nudge to !start.
    assert any("start" in line.lower() for line in lines)


@pytest.mark.asyncio
async def test_simulator_control_name() -> None:
    config = BotConfig(game_url="http://game:8080")
    async with GameClient(config.game_url) as game:
        sim = Simulator(_make_bot(config, game))
        lines = await sim.handle_line("/name alice")
    assert sim.sender_name == "alice"
    assert any("alice" in line.lower() for line in lines)


@pytest.mark.asyncio
async def test_simulator_control_channel() -> None:
    config = BotConfig(game_url="http://game:8080")
    async with GameClient(config.game_url) as game:
        sim = Simulator(_make_bot(config, game))
        lines = await sim.handle_line("/channel 3")
    assert sim.channel_idx == 3


@pytest.mark.asyncio
async def test_simulator_control_quit() -> None:
    config = BotConfig(game_url="http://game:8080")
    async with GameClient(config.game_url) as game:
        sim = Simulator(_make_bot(config, game))
        lines = await sim.handle_line("/quit")
    assert sim.done is True
    assert any("bye" in line.lower() for line in lines)
