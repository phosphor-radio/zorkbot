import pytest
import respx
from httpx import Response

from zorkbot.bot import ZorkBot
from zorkbot.config import BotConfig
from zorkbot.game_client import GameClient
from zorkbot.simulator import Simulator


@pytest.mark.asyncio
@respx.mock
async def test_simulator_help() -> None:
    respx.get("http://game:8080/status").mock(
        return_value=Response(200, json={"uptime": "1m", "busy": False})
    )
    config = BotConfig(game_url="http://game:8080")
    async with GameClient(config.game_url) as game:
        sim = Simulator(ZorkBot(config, game))
        lines = await sim.handle_line("!zork help")
    assert any("shared world" in line.lower() for line in lines)
