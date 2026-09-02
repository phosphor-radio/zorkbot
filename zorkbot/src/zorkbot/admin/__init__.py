"""Administrative web UI: embedded FastAPI app + lifecycle management.

`AdminUIServer` owns the SQLite store, the OAuth2 auth service, the live
SSE session bus, and the batched event sink, and runs uvicorn as an asyncio
task alongside `MeshCoreRunner` inside the same process — see
docs/specs/admin-web-ui.md for why this isn't a separate service.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from zorkbot.admin.auth import AuthService
from zorkbot.admin.bus import SessionBus
from zorkbot.admin.context import AdminContext
from zorkbot.admin.events import SqliteEventSink
from zorkbot.admin.routes import auth as auth_routes
from zorkbot.admin.routes import meta as meta_routes
from zorkbot.admin.routes import players as players_routes
from zorkbot.admin.routes import sessions as sessions_routes
from zorkbot.admin.routes import stats as stats_routes
from zorkbot.admin.store import Store

if TYPE_CHECKING:
    from zorkbot.bot import ZorkBot
    from zorkbot.config import AdminUIConfig

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"

_SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'"
    ),
}


def create_app(ctx: AdminContext) -> FastAPI:
    app = FastAPI(title="zorkbot admin", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        for key, value in _SECURITY_HEADERS.items():
            response.headers[key] = value
        return response

    app.include_router(auth_routes.router, prefix="/api")
    app.include_router(sessions_routes.router, prefix="/api")
    app.include_router(stats_routes.router, prefix="/api")
    app.include_router(players_routes.router, prefix="/api")
    app.include_router(meta_routes.router, prefix="/api")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    if _STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")

    return app


class AdminUIServer:
    """Two-phase lifecycle: construct with just the config (so its
    `EventSink` exists before `ZorkBot` does — the sink is a constructor
    argument to `ZorkBot`/`SessionState`), then `start(bot)` once the bot
    exists to bring up the HTTP server and hand it a live bot reference.
    """

    def __init__(self, config: "AdminUIConfig") -> None:
        self.config = config
        self.bot: "ZorkBot | None" = None
        self.store = Store(
            config.db_path,
            event_queue_size=config.event_queue_size,
            retention_days=config.event_retention_days,
        )
        self.bus = SessionBus(
            buffer_size=config.live_buffer_events,
            max_streams=config.max_live_streams,
        )
        self.auth = AuthService(
            self.store,
            access_token_ttl_seconds=config.access_token_ttl_seconds,
            refresh_token_ttl_seconds=config.refresh_token_ttl_seconds,
        )
        self.bot_run_id = uuid.uuid4().hex
        self.sink = SqliteEventSink(
            self.store,
            self.bus,
            bot_run_id=self.bot_run_id,
            queue_size=config.event_queue_size,
        )
        self._server: uvicorn.Server | None = None
        self._server_task: asyncio.Task | None = None
        self._retention_task: asyncio.Task | None = None

    async def start(self, bot: "ZorkBot") -> None:
        self.bot = bot
        await self.store.start()
        await self.auth.ensure_admin_user()
        self.sink.start()

        ctx = AdminContext(
            store=self.store,
            bus=self.bus,
            auth=self.auth,
            sink=self.sink,
            bot=bot,
            config=self.config,
            process_started_at=time.monotonic(),
        )
        app = create_app(ctx)
        uv_config = uvicorn.Config(
            app, host=self.config.bind, port=self.config.port, log_level="warning"
        )
        self._server = uvicorn.Server(uv_config)
        self._server_task = asyncio.create_task(self._server.serve(), name="admin-ui-http")
        self._retention_task = asyncio.create_task(
            self._retention_loop(), name="admin-ui-retention"
        )

        if self.config.bind not in ("127.0.0.1", "localhost", "::1"):
            logger.warning(
                "admin UI bound to %s:%d — traffic (including the login password and bearer "
                "tokens) is plaintext HTTP; restrict to a trusted network or front it with TLS",
                self.config.bind,
                self.config.port,
            )
        logger.info("admin UI listening on %s:%d", self.config.bind, self.config.port)

    async def _retention_loop(self) -> None:
        while True:
            await asyncio.sleep(86400)
            try:
                await self.store.prune_retention()
            except Exception:
                logger.exception("admin-ui: retention prune failed")

    async def stop(self) -> None:
        if self._retention_task is not None:
            self._retention_task.cancel()
        if self._server is not None:
            self._server.should_exit = True
        if self._server_task is not None:
            try:
                await self._server_task
            except Exception:
                logger.exception("admin-ui: HTTP server task raised on shutdown")
        if self._retention_task is not None:
            try:
                await self._retention_task
            except asyncio.CancelledError:
                pass
        await self.sink.stop()
        self.store.close()
