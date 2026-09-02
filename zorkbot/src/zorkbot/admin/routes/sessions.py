"""Active sessions, session history, and the live SSE transcript stream."""

from __future__ import annotations

import asyncio
import json
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from zorkbot.admin.deps import get_ctx, require_scope

router = APIRouter(tags=["sessions"])

_PUBKEY_RE = re.compile(r"^[0-9a-f]{12}$")


def _resolve_name(bot, pubkey_prefix: str) -> str:
    try:
        contact = bot.meshcore.get_contact_by_key_prefix(pubkey_prefix)
    except Exception:
        contact = None
    if contact:
        return contact.get("adv_name", pubkey_prefix[:8])
    return pubkey_prefix[:8]


@router.get("/sessions")
async def list_sessions(request: Request, _entry: dict = Depends(require_scope())) -> dict:
    ctx = get_ctx(request)
    bot = ctx.bot
    now = time.monotonic()
    sessions = []
    for record in bot.session_state.all_sessions():
        sessions.append(
            {
                "num": record.num,
                "player": {"pubkey_prefix": record.player_id, "name": record.player_name},
                "started_at": int(getattr(record, "started_wall_at", time.time())),
                "duration_seconds": int(now - record.started_at),
                "watchers": [
                    {"pubkey_prefix": w, "name": _resolve_name(bot, w)}
                    for w in sorted(record.watchers)
                ],
            }
        )
    sessions.sort(key=lambda s: s["num"])
    return {"bot_run_id": ctx.sink.bot_run_id, "sessions": sessions}


@router.get("/sessions/history")
async def session_history(
    request: Request,
    from_: int | None = Query(None, alias="from"),
    to: int | None = Query(None),
    player: str | None = Query(None),
    limit: int = Query(100, le=500),
    cursor: str | None = Query(None),
    _entry: dict = Depends(require_scope()),
) -> dict:
    ctx = get_ctx(request)
    if player is not None and not _PUBKEY_RE.match(player):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_request", "error_description": "invalid player pubkey_prefix"},
        )

    clauses = []
    params: list = []
    if from_ is not None:
        clauses.append("started_at >= ?")
        params.append(from_)
    if to is not None:
        clauses.append("started_at <= ?")
        params.append(to)
    if player is not None:
        clauses.append("pubkey_prefix = ?")
        params.append(player)
    if cursor:
        try:
            cur_started, cur_id = _decode_cursor(cursor)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_request", "error_description": "invalid cursor"},
            )
        clauses.append("(started_at < ? OR (started_at = ? AND id < ?))")
        params.extend([cur_started, cur_started, cur_id])

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = await ctx.store.query(
        f"SELECT s.*, p.name AS player_name FROM sessions s "
        f"LEFT JOIN players p ON p.pubkey_prefix = s.pubkey_prefix "
        f"{where} ORDER BY started_at DESC, id DESC LIMIT ?",
        (*params, limit),
    )

    result = []
    for row in rows:
        duration = None
        if row["ended_at"] is not None:
            duration = row["ended_at"] - row["started_at"]
        result.append(
            {
                "id": row["id"],
                "session_num": row["session_num"],
                "bot_run_id": row["bot_run_id"],
                "player": {"pubkey_prefix": row["pubkey_prefix"], "name": row["player_name"]},
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "duration_seconds": duration,
                "end_reason": row["end_reason"],
                "peak_watchers": row["peak_watchers"],
            }
        )

    next_cursor = None
    if len(rows) == limit:
        last = rows[-1]
        next_cursor = _encode_cursor(last["started_at"], last["id"])

    return {"sessions": result, "next_cursor": next_cursor}


def _encode_cursor(started_at: int, row_id: int) -> str:
    import base64

    return base64.urlsafe_b64encode(f"{started_at}:{row_id}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[int, int]:
    import base64

    raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    started_at, row_id = raw.split(":")
    return int(started_at), int(row_id)


@router.get("/sessions/{num}/stream")
async def stream_session(
    request: Request, num: int, _entry: dict = Depends(require_scope())
) -> StreamingResponse:
    ctx = get_ctx(request)
    if ctx.bot.session_state.get_session_by_num(num) is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "error_description": "no such active session"})
    if not ctx.bus.can_subscribe():
        raise HTTPException(status_code=429, detail={"error": "too_many_streams", "error_description": "max concurrent admin streams reached"})

    queue, backlog = ctx.bus.subscribe(num)

    async def event_source():
        try:
            for item in backlog:
                yield _sse_format(item)
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield _sse_format(item)
                if item["event"] == "session_end":
                    break
        finally:
            ctx.bus.unsubscribe(num, queue)

    return StreamingResponse(event_source(), media_type="text/event-stream")


def _sse_format(item: dict) -> str:
    return f"event: {item['event']}\ndata: {json.dumps(item['data'])}\n\n"
