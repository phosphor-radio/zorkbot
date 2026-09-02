"""End-to-end tests for the admin HTTP API, driven in-process via ASGITransport."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from zorkbot.admin import create_app
from zorkbot.admin.auth import DEFAULT_PASSWORD, AuthService
from zorkbot.admin.bus import SessionBus
from zorkbot.admin.context import AdminContext
from zorkbot.admin.events import SqliteEventSink
from zorkbot.admin.store import Store
from zorkbot.advertiser import Advertiser
from zorkbot.bot import ZorkBot
from zorkbot.config import BotConfig
from zorkbot.game_client import GameClient


@pytest.fixture
async def client():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "admin.db")
        await store.start()
        auth = AuthService(store)
        await auth.ensure_admin_user()
        bus = SessionBus(max_streams=2)
        sink = SqliteEventSink(store, bus, bot_run_id="testrun")
        sink.start()

        config = BotConfig()
        game = GameClient(config.game_url)
        meshcore = MagicMock()
        meshcore.get_contact_by_key_prefix.return_value = None
        bot = ZorkBot(config, game, Advertiser(), meshcore, event_sink=sink)

        ctx = AdminContext(
            store=store, bus=bus, auth=auth, sink=sink, bot=bot,
            config=config.admin_ui, process_started_at=0.0,
        )
        app = create_app(ctx)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            c.bot = bot  # type: ignore[attr-defined]
            yield c

        await sink.stop()
        store.close()
        await game.close()


async def _admin_token(client) -> str:
    r = await client.post(
        "/api/token",
        data={"grant_type": "password", "username": "admin", "password": DEFAULT_PASSWORD},
    )
    await client.post(
        "/api/auth/password",
        headers={"Authorization": f"Bearer {r.json()['access_token']}"},
        json={"current_password": DEFAULT_PASSWORD, "new_password": "a-strong-new-password"},
    )
    r = await client.post(
        "/api/token",
        data={"grant_type": "password", "username": "admin", "password": "a-strong-new-password"},
    )
    return r.json()["access_token"]


@pytest.mark.asyncio
async def test_health_requires_no_auth(client) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_token_requires_username_and_password(client) -> None:
    r = await client.post("/api/token", data={"grant_type": "password"})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_request"


@pytest.mark.asyncio
async def test_token_unsupported_grant_type(client) -> None:
    r = await client.post("/api/token", data={"grant_type": "client_credentials"})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "unsupported_grant_type"


@pytest.mark.asyncio
async def test_missing_bearer_token_rejected(client) -> None:
    r = await client.get("/api/sessions")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_must_change_password_blocks_admin_scope(client) -> None:
    r = await client.post(
        "/api/token",
        data={"grant_type": "password", "username": "admin", "password": DEFAULT_PASSWORD},
    )
    access = r.json()["access_token"]
    r = await client.get("/api/sessions", headers={"Authorization": f"Bearer {access}"})
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "insufficient_scope"


@pytest.mark.asyncio
async def test_active_sessions_reflect_live_bot_state(client) -> None:
    token = await _admin_token(client)
    client.bot.session_state.add_session("aabbccddeeff", "Alice")

    r = await client.get("/api/sessions", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["player"]["pubkey_prefix"] == "aabbccddeeff"


@pytest.mark.asyncio
async def test_stream_404_for_unknown_session(client) -> None:
    token = await _admin_token(client)
    r = await client.get("/api/sessions/999/stream", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_players_pubkey_validation(client) -> None:
    token = await _admin_token(client)
    r = await client.get("/api/players/not-hex!!", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_players_unknown_player_404s(client) -> None:
    token = await _admin_token(client)
    r = await client.get("/api/players/aabbccddeeff", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_stats_rejects_invalid_bucket(client) -> None:
    token = await _admin_token(client)
    r = await client.get(
        "/api/stats/sessions?bucket=fortnight", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_stats_messages_rejects_invalid_transport(client) -> None:
    token = await _admin_token(client)
    r = await client.get(
        "/api/stats/messages?transport=carrier-pigeon",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_stats_sessions_zero_fills_empty_range(client) -> None:
    token = await _admin_token(client)
    r = await client.get(
        "/api/stats/sessions?from=0&to=3600&bucket=hour",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body) >= 1
    assert all(point["started"] == 0 and point["ended"] == 0 for point in body)


@pytest.mark.asyncio
async def test_status_endpoint(client) -> None:
    token = await _admin_token(client)
    r = await client.get("/api/status", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["bot_run_id"] == "testrun"
    assert body["active_sessions"] == 0
