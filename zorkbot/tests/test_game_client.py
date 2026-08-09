import httpx
import pytest
import respx

from zorkbot.game_client import GameClient, GameServiceError


@pytest.mark.asyncio
@respx.mock
async def test_command_success() -> None:
    route = respx.post("http://game:8080/command").mock(
        return_value=httpx.Response(
            200,
            json={"ok": True, "output": "West of House\n"},
        )
    )

    async with GameClient("http://game:8080") as client:
        result = await client.command("look")

    assert route.called
    assert result.ok is True
    assert result.output == "West of House\n"


@pytest.mark.asyncio
@respx.mock
async def test_command_blocked() -> None:
    respx.post("http://game:8080/command").mock(
        return_value=httpx.Response(
            200,
            json={"ok": False, "error": "that command isn't allowed"},
        )
    )

    async with GameClient("http://game:8080") as client:
        result = await client.command("$quit")

    assert result.ok is False
    assert result.error == "that command isn't allowed"


@pytest.mark.asyncio
@respx.mock
async def test_health() -> None:
    respx.get("http://game:8080/health").mock(
        return_value=httpx.Response(200, text="ok")
    )

    async with GameClient("http://game:8080") as client:
        assert await client.health() is True


@pytest.mark.asyncio
@respx.mock
async def test_status() -> None:
    respx.get("http://game:8080/status").mock(
        return_value=httpx.Response(
            200,
            json={"uptime": "5m", "busy": False},
        )
    )

    async with GameClient("http://game:8080") as client:
        status = await client.status()

    assert status.uptime == "5m"
    assert status.busy is False


@pytest.mark.asyncio
@respx.mock
async def test_reset_requires_admin_token() -> None:
    async with GameClient("http://game:8080") as client:
        with pytest.raises(GameServiceError, match="admin token not configured"):
            await client.reset()


@pytest.mark.asyncio
@respx.mock
async def test_reset_success() -> None:
    route = respx.post("http://game:8080/reset").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    async with GameClient("http://game:8080", admin_token="secret") as client:
        await client.reset()

    assert route.called
    assert route.calls.last.request.headers["X-Admin-Token"] == "secret"


@pytest.mark.asyncio
@respx.mock
async def test_command_http_error() -> None:
    respx.post("http://game:8080/command").mock(
        return_value=httpx.Response(
            409,
            json={"ok": False, "error": "game is busy, try again"},
        )
    )

    async with GameClient("http://game:8080") as client:
        with pytest.raises(GameServiceError, match="game is busy"):
            await client.command("look")
