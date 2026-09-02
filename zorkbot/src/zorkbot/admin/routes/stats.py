"""Time-bucketed charts: sessions, messages, commands."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from zorkbot.admin.deps import get_ctx, require_scope
from zorkbot.admin.store import Store

router = APIRouter(tags=["stats"])

_MAX_POINTS = 5000
_VALID_TRANSPORTS = {"dm", "channel", "both"}
_VALID_DIRECTIONS = {"rx", "tx"}


def _range(from_: int | None, to: int | None, bucket: str) -> tuple[int, int, int]:
    try:
        bucket_seconds = Store.bucket_seconds(bucket)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_request", "error_description": f"invalid bucket: {bucket!r}"},
        )
    now = int(time.time())
    to = now if to is None else to
    from_ = to - 86400 if from_ is None else from_
    if to < from_:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_request", "error_description": "'to' must be >= 'from'"},
        )
    span_buckets = (to - from_) // bucket_seconds + 1
    if span_buckets > _MAX_POINTS:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_request",
                "error_description": f"range too wide for bucket={bucket!r} (max {_MAX_POINTS} points)",
            },
        )
    return from_, to, bucket_seconds


def _zero_fill(from_: int, to: int, bucket_seconds: int, keys: tuple[str, ...]) -> dict[int, dict]:
    buckets: dict[int, dict] = {}
    start = from_ - (from_ % bucket_seconds)
    t = start
    while t <= to:
        buckets[t] = {k: 0 for k in keys}
        t += bucket_seconds
    return buckets


@router.get("/stats/sessions")
async def stats_sessions(
    request: Request,
    from_: int | None = Query(None, alias="from"),
    to: int | None = Query(None),
    bucket: str = Query("hour"),
    _entry: dict = Depends(require_scope()),
) -> list[dict]:
    ctx = get_ctx(request)
    from_, to, bucket_seconds = _range(from_, to, bucket)
    buckets = _zero_fill(from_, to, bucket_seconds, ("started", "ended"))

    started_rows = await ctx.store.query(
        "SELECT started_at FROM sessions WHERE started_at BETWEEN ? AND ?", (from_, to)
    )
    for row in started_rows:
        t = row["started_at"] - (row["started_at"] % bucket_seconds)
        buckets.setdefault(t, {"started": 0, "ended": 0})["started"] += 1

    ended_rows = await ctx.store.query(
        "SELECT ended_at FROM sessions WHERE ended_at IS NOT NULL AND ended_at BETWEEN ? AND ?",
        (from_, to),
    )
    for row in ended_rows:
        t = row["ended_at"] - (row["ended_at"] % bucket_seconds)
        buckets.setdefault(t, {"started": 0, "ended": 0})["ended"] += 1

    return [{"t": t, **counts} for t, counts in sorted(buckets.items())]


@router.get("/stats/messages")
async def stats_messages(
    request: Request,
    from_: int | None = Query(None, alias="from"),
    to: int | None = Query(None),
    bucket: str = Query("hour"),
    direction: str = Query("rx"),
    transport: str = Query("both"),
    _entry: dict = Depends(require_scope()),
) -> list[dict]:
    if direction not in _VALID_DIRECTIONS:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_request", "error_description": f"invalid direction: {direction!r}"},
        )
    if transport not in _VALID_TRANSPORTS:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_request", "error_description": f"invalid transport: {transport!r}"},
        )
    ctx = get_ctx(request)
    from_, to, bucket_seconds = _range(from_, to, bucket)
    buckets = _zero_fill(from_, to, bucket_seconds, ("count", "chars"))

    sql = "SELECT at, chars FROM messages WHERE direction = ? AND at BETWEEN ? AND ?"
    params: list = [direction, from_, to]
    if transport != "both":
        sql += " AND transport = ?"
        params.append(transport)

    rows = await ctx.store.query(sql, tuple(params))
    for row in rows:
        t = row["at"] - (row["at"] % bucket_seconds)
        b = buckets.setdefault(t, {"count": 0, "chars": 0})
        b["count"] += 1
        b["chars"] += row["chars"]

    return [{"t": t, **counts} for t, counts in sorted(buckets.items())]


@router.get("/stats/commands")
async def stats_commands(
    request: Request,
    from_: int | None = Query(None, alias="from"),
    to: int | None = Query(None),
    bucket: str = Query("hour"),
    command: str | None = Query(None),
    transport: str = Query("both"),
    _entry: dict = Depends(require_scope()),
) -> list[dict]:
    if transport not in _VALID_TRANSPORTS:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_request", "error_description": f"invalid transport: {transport!r}"},
        )
    ctx = get_ctx(request)
    from_, to, bucket_seconds = _range(from_, to, bucket)
    buckets = _zero_fill(from_, to, bucket_seconds, ("accepted", "rejected"))

    sql = "SELECT at, accepted FROM commands WHERE at BETWEEN ? AND ?"
    params: list = [from_, to]
    if command is not None:
        sql += " AND command = ?"
        params.append(command)
    if transport != "both":
        sql += " AND transport = ?"
        params.append(transport)

    rows = await ctx.store.query(sql, tuple(params))
    for row in rows:
        t = row["at"] - (row["at"] % bucket_seconds)
        b = buckets.setdefault(t, {"accepted": 0, "rejected": 0})
        if row["accepted"]:
            b["accepted"] += 1
        else:
            b["rejected"] += 1

    return [{"t": t, **counts} for t, counts in sorted(buckets.items())]
