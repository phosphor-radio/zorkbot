"""Bot status and liveness."""

from __future__ import annotations

import time
from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter, Depends, Request

from zorkbot.admin.deps import get_ctx, require_scope

router = APIRouter(tags=["meta"])


def _version() -> str:
    try:
        return version("zorkbot")
    except PackageNotFoundError:
        return "unknown"


@router.get("/status")
async def status(request: Request, _entry: dict = Depends(require_scope())) -> dict:
    ctx = get_ctx(request)
    bot = ctx.bot
    try:
        game_reachable = await bot.game.health()
    except Exception:
        game_reachable = False

    return {
        "bot_run_id": ctx.sink.bot_run_id,
        "version": _version(),
        "uptime_seconds": int(time.monotonic() - bot._started_at),
        "active_sessions": len(bot.session_state.all_sessions()),
        "send_queue_depth": bot.send_queue_depth,
        "game_service_reachable": game_reachable,
        "event_queue_depth": ctx.sink._queue.qsize(),
        "event_queue_dropped": ctx.sink._drop_count,
    }
