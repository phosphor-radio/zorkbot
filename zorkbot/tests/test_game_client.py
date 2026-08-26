"""Tests for the multi-session GameClient."""

import httpx
import pytest
import respx

from zorkbot.game_client import (
    GameClient,
    GameServiceError,
    SessionFullError,
    SessionNotFoundError,
)

PLAYER = "aabbccddeeff"


@pytest.mark.asyncio
@respx.mock
async def test_start_session_success() -> None:
    route = respx.post("http://game:8080/sessions").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    async with GameClient("http://game:8080") as client:
        await client.start_session(PLAYER)
    assert route.called
    import json
    assert json.loads(route.calls.last.request.content) == {"player_id": PLAYER}


@pytest.mark.asyncio
@respx.mock
async def test_start_session_full() -> None:
    respx.post("http://game:8080/sessions").mock(
        return_value=httpx.Response(503, json={"ok": False, "error": "session pool is full"})
    )
    async with GameClient("http://game:8080") as client:
        with pytest.raises(SessionFullError):
            await client.start_session(PLAYER)


@pytest.mark.asyncio
@respx.mock
async def test_end_session_success() -> None:
    route = respx.delete(f"http://game:8080/sessions/{PLAYER}").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    async with GameClient("http://game:8080") as client:
        await client.end_session(PLAYER)
    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_end_session_not_found() -> None:
    respx.delete(f"http://game:8080/sessions/{PLAYER}").mock(
        return_value=httpx.Response(404, json={"ok": False, "error": "session not found"})
    )
    async with GameClient("http://game:8080") as client:
        with pytest.raises(SessionNotFoundError):
            await client.end_session(PLAYER)


@pytest.mark.asyncio
@respx.mock
async def test_reset_session_success() -> None:
    route = respx.delete(f"http://game:8080/sessions/{PLAYER}/save").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    async with GameClient("http://game:8080") as client:
        await client.reset_session(PLAYER)
    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_command_success() -> None:
    route = respx.post(f"http://game:8080/sessions/{PLAYER}/command").mock(
        return_value=httpx.Response(200, json={"ok": True, "output": "West of House\n"})
    )
    async with GameClient("http://game:8080") as client:
        result = await client.command(PLAYER, "look")
    assert route.called
    assert result.ok is True
    assert result.output == "West of House\n"


@pytest.mark.asyncio
@respx.mock
async def test_command_blocked() -> None:
    respx.post(f"http://game:8080/sessions/{PLAYER}/command").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "that command isn't allowed"})
    )
    async with GameClient("http://game:8080") as client:
        result = await client.command(PLAYER, "$quit")
    assert result.ok is False
    assert result.error == "that command isn't allowed"


@pytest.mark.asyncio
@respx.mock
async def test_list_sessions() -> None:
    respx.get("http://game:8080/sessions").mock(
        return_value=httpx.Response(
            200,
            json={
                "sessions": [
                    {"num": 1, "player_id": PLAYER, "started_at": "2026-01-01T00:00:00Z"}
                ]
            },
        )
    )
    async with GameClient("http://game:8080") as client:
        sessions = await client.list_sessions()
    assert len(sessions) == 1
    assert sessions[0].num == 1
    assert sessions[0].player_id == PLAYER


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
async def test_command_session_not_found() -> None:
    respx.post(f"http://game:8080/sessions/{PLAYER}/command").mock(
        return_value=httpx.Response(404, json={"ok": False, "error": "no active session"})
    )
    async with GameClient("http://game:8080") as client:
        with pytest.raises(SessionNotFoundError):
            await client.command(PLAYER, "look")
