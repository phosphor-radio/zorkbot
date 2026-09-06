"""Live tail of the process log, as a server-sent event stream."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from zorkbot.admin.deps import get_ctx, require_scope
from zorkbot.admin.logbus import LEVEL_NAMES, parse_level

router = APIRouter(tags=["logs"])


@router.get("/logs/stream")
async def stream_logs(
    request: Request,
    level: str = Query("INFO"),
    _entry: dict = Depends(require_scope()),
) -> StreamingResponse:
    """`text/event-stream` of log records at or above `level`.

    Auth works the same way as the session transcript stream: a bearer header
    on a `fetch()`-read stream, never a token in the query string. See
    docs/specs/admin-web-ui.md.
    """
    ctx = get_ctx(request)
    min_level = parse_level(level)
    if min_level is None:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_request",
                "error_description": f"invalid level: {level!r} (one of {', '.join(LEVEL_NAMES)})",
            },
        )
    if not ctx.logbus.can_subscribe():
        raise HTTPException(
            status_code=429,
            detail={
                "error": "too_many_streams",
                "error_description": "max concurrent log streams reached",
            },
        )

    queue, backlog = ctx.logbus.subscribe(min_level=min_level)

    async def event_source():
        try:
            # The process log level bounds what any filter can show; the client
            # says so in the header rather than leaving an operator staring at
            # an empty DEBUG view on a bot running at INFO.
            yield _sse(
                "hello",
                {
                    "root_level": ctx.logbus.effective_level_name(),
                    "buffer_size": ctx.logbus.buffer_size,
                    "replayed": len(backlog),
                },
            )
            for item in backlog:
                yield _sse("log", item)
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield _sse("log", item)
        finally:
            ctx.logbus.unsubscribe(queue)

    return StreamingResponse(event_source(), media_type="text/event-stream")


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
