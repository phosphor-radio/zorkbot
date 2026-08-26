"""Tests for the local development simulator."""

from unittest.mock import AsyncMock, MagicMock

import pytest
import respx

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
    return ZorkBot(config, game, advertiser, mc)


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


@pytest.mark.asyncio
@respx.mock
async def test_simulator_start_channel_shows_dm() -> None:
    """!start on channel should show both the channel ack and the DM intro."""
    import httpx

    config = BotConfig(game_url="http://game:8080", rate_limit_seconds=0.0)
    respx.post("http://game:8080/sessions").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    # Meshcore that has the player in its contact table so !start can proceed.
    mc = MagicMock()
    mc.get_contact_by_key_prefix = MagicMock(return_value={"adv_name": "you"})
    advertiser = Advertiser()
    advertiser.send_if_due = AsyncMock()

    async with GameClient(config.game_url) as game:
        bot = ZorkBot(config, game, advertiser, mc)
        sim = Simulator(bot)
        lines = await sim.handle_line("!start")

    # Channel ack visible.
    assert any("Session #1" in line for line in lines), lines
    # DM intro also visible.
    assert any("[DM" in line for line in lines), lines
