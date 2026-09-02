"""Per-player statistics, derived by query rather than maintained counters."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from zorkbot.admin.deps import get_ctx, require_scope
from zorkbot.admin.store import Store

router = APIRouter(tags=["players"])

_PUBKEY_RE = re.compile(r"^[0-9a-f]{12}$")

_DETAIL_SQL = """
SELECT
  p.pubkey_prefix,
  p.name,
  p.first_seen_at,
  p.last_seen_at,
  p.banned_at,
  (SELECT COUNT(*) FROM messages WHERE direction = 'rx' AND pubkey_prefix = p.pubkey_prefix) AS messages_received_from,
  (SELECT COUNT(*) FROM messages WHERE direction = 'tx' AND pubkey_prefix = p.pubkey_prefix) AS messages_sent_to,
  (SELECT COUNT(*) FROM sessions WHERE pubkey_prefix = p.pubkey_prefix) AS sessions_started,
  (SELECT COALESCE(SUM(COALESCE(ended_at, strftime('%s','now')) - started_at), 0)
     FROM sessions WHERE pubkey_prefix = p.pubkey_prefix) AS total_play_seconds
FROM players p
"""


def _row_to_dict(row) -> dict:
    return {
        "pubkey_prefix": row["pubkey_prefix"],
        "name": row["name"],
        "first_active_at": row["first_seen_at"],
        "last_active_at": row["last_seen_at"],
        "messages_received_from": row["messages_received_from"],
        "messages_sent_to": row["messages_sent_to"],
        "sessions_started": row["sessions_started"],
        "total_play_seconds": row["total_play_seconds"],
        "banned": row["banned_at"] is not None,
    }


@router.get("/players")
async def list_players(
    request: Request,
    sort: str = Query("last_active"),
    order: str = Query("desc"),
    limit: int = Query(50, le=500),
    cursor: int = Query(0),
    _entry: dict = Depends(require_scope()),
) -> dict:
    try:
        sort_column = Store.player_sort_column(sort)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_request", "error_description": f"invalid sort: {sort!r}"},
        )
    if order not in ("asc", "desc"):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_request", "error_description": f"invalid order: {order!r}"},
        )
    ctx = get_ctx(request)
    direction = "ASC" if order == "asc" else "DESC"
    rows = await ctx.store.query(
        f"SELECT * FROM ({_DETAIL_SQL}) ORDER BY {sort_column} {direction} LIMIT ? OFFSET ?",
        (limit, cursor),
    )
    players = [_row_to_dict(r) for r in rows]
    next_cursor = cursor + limit if len(rows) == limit else None
    return {"players": players, "next_cursor": next_cursor}


@router.get("/players/{pubkey_prefix}")
async def player_detail(
    request: Request, pubkey_prefix: str, _entry: dict = Depends(require_scope())
) -> dict:
    if not _PUBKEY_RE.match(pubkey_prefix):
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_request", "error_description": "invalid pubkey_prefix"},
        )
    ctx = get_ctx(request)
    row = await ctx.store.query_one(
        f"SELECT * FROM ({_DETAIL_SQL}) WHERE pubkey_prefix = ?", (pubkey_prefix,)
    )
    if row is None:
        raise HTTPException(
            status_code=404, detail={"error": "not_found", "error_description": "unknown player"}
        )
    return _row_to_dict(row)
