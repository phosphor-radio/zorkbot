"""Tests for the local development simulator."""

import asyncio
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

    config = BotConfig(game_url="http://game:8080")
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


@pytest.mark.asyncio
@respx.mock
async def test_simulator_shows_watcher_fanout_with_the_command_that_caused_it() -> None:
    """Watcher fan-out must land in the output of the line that produced it.

    handle_line prints whatever reached _pending_dms by the time drain()
    returns, and clears the buffer on the next line — so fan-out that
    outlives drain() is not merely late, it is lost. Nothing about the
    simulator enforces that on its own; drain() has to.
    """
    import httpx

    config = BotConfig(game_url="http://game:8080")
    respx.post("http://game:8080/sessions").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.post("http://game:8080/sessions/616c69636500/command").mock(
        return_value=httpx.Response(200, json={"ok": True, "output": "You go north."})
    )
    mc = MagicMock()
    mc.get_contact_by_key_prefix = MagicMock(return_value={"adv_name": "alice"})
    advertiser = Advertiser()
    advertiser.send_if_due = AsyncMock()

    async with GameClient(config.game_url) as game:
        bot = ZorkBot(config, game, advertiser, mc)
        sim = Simulator(bot)

        # The simulator bypasses the runner, so its DM sink never suspends
        # and fan-out happens to finish inside drain()'s other awaits. Under
        # the runner a send does suspend — send_spacing_seconds, behind a
        # shared lock — so model that here. The test then rests on drain()
        # covering fan-out rather than on that scheduling coincidence.
        instant_send = bot._send_dm

        async def spaced_send(pubkey_prefix: str, text: str) -> None:
            await asyncio.sleep(0.01)
            await instant_send(pubkey_prefix, text)

        bot.set_send_dm(spaced_send)

        await sim.handle_line("/name alice")
        await sim.handle_line("!start")
        await sim.handle_line("/name bob")
        await sim.handle_line("dm:!watch 1")
        await sim.handle_line("/name alice")

        lines = await sim.handle_line("dm:north")

    assert any("> north" in line for line in lines), (
        f"watcher fan-out never made it into the command's output: {lines}"
    )


@pytest.mark.asyncio
async def test_simulator_shows_the_delayed_bots_roll_call_reply() -> None:
    """The !bots answer is spawned off its dispatch and sleeps before it
    replies, so handle_line returns long before it arrives. drain() has to
    wait for it, or the simulator reports "(no response)" for a command the
    bot in fact answered.
    """
    from unittest.mock import patch

    import zorkbot.commands.bots as bots_module
    from zorkbot.channels import ChannelConfig

    config = BotConfig(game_url="http://game:8080")
    config.bots_enabled = True
    config.bots_channel = ChannelConfig(index=2, name="#bots")

    async with GameClient(config.game_url) as game:
        sim = Simulator(_make_bot(config, game))
        sim.channel_idx = config.bots_channel.index
        # A real, if brief, sleep: the point is that drain() waits through an
        # actual suspension, not that the delay is short.
        with (
            patch.object(bots_module, "REPLY_DELAY_BASE_SECONDS", 0.01),
            patch.object(bots_module, "REPLY_DELAY_JITTER_SECONDS", 0.0),
        ):
            lines = await sim.handle_line("!bots")

    assert any("zorkbot" in line for line in lines), lines
    assert any(config.channel.name in line for line in lines), lines
