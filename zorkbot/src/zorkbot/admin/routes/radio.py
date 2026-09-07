"""The attached radio: identity, RF settings, contacts, channels, recent traffic.

Read-only. Nothing here transmits, so no request on these routes reaches the
RF send gate — see docs/specs/admin-radio-view.md.

Channel responses are assembled field by field, never forwarded as parsed:
the CHANNEL_INFO payload carries `channel_secret`, the 16-byte pre-shared key,
which must never leave the process.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request

from zorkbot.admin.deps import get_ctx, require_scope

router = APIRouter(tags=["radio"])

_PUBKEY_RE = re.compile(r"^[0-9a-f]{6,64}$")


@router.get("/radio")
async def radio(request: Request, _entry: dict = Depends(require_scope())) -> dict:
    return await get_ctx(request).bot.radio_state.snapshot()


@router.get("/radio/contacts")
async def contacts(request: Request, _entry: dict = Depends(require_scope())) -> dict:
    bot = get_ctx(request).bot
    windows = bot.message_windows
    listed = bot.radio_state.contacts()
    for contact in listed:
        contact["message_count"] = windows.contact_count(contact["pubkey_prefix"])
    return {"contacts": listed}


@router.get("/radio/contacts/{prefix}/messages")
async def contact_messages(
    prefix: str, request: Request, _entry: dict = Depends(require_scope())
) -> dict:
    bot = get_ctx(request).bot
    if not _PUBKEY_RE.match(prefix):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_request",
                "error_description": "prefix must be lowercase hex",
            },
        )
    contact = next(
        (c for c in bot.radio_state.contacts() if c["pubkey_prefix"] == prefix), None
    )
    if contact is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "unknown_contact",
                "error_description": f"no contact with prefix {prefix}",
            },
        )
    return {
        "pubkey_prefix": prefix,
        "name": contact["name"],
        "window": bot.message_windows.window,
        # Always true in this phase. It is a field rather than a hardcoded
        # note so the UI can state the limitation without asserting it, and
        # so it can go false if persistence is ever specified.
        "since_process_start": True,
        "messages": [m.as_dict() for m in bot.message_windows.contact_messages(prefix)],
    }


@router.get("/radio/channels")
async def channels(request: Request, _entry: dict = Depends(require_scope())) -> dict:
    bot = get_ctx(request).bot
    state = bot.radio_state
    windows = bot.message_windows

    listed = []
    for channel in await state.channels():
        idx = channel["idx"]
        tracked = windows.tracks_channel(idx)
        listed.append({
            "idx": idx,
            "name": channel["name"],
            "hash": channel["hash"],
            "role": state.channel_role(idx),
            # tracked separates the two null cases below: on an untracked
            # channel they mean "not applicable" and render as n/a; on a
            # tracked one a null last_message_at means "nothing seen yet"
            # and renders as a dash. Showing both the same way would say a
            # monitored channel is silent when it is simply unmonitored.
            "tracked": tracked,
            "message_count": windows.channel_count(idx),
            "last_message_at": windows.channel_last_at(idx) if tracked else None,
        })
    return {"channels": listed, "stale_since": state.stale_since}


@router.get("/radio/channels/{idx}/messages")
async def channel_messages(
    idx: int, request: Request, _entry: dict = Depends(require_scope())
) -> dict:
    bot = get_ctx(request).bot
    known = {c["idx"] for c in await bot.radio_state.channels()}
    if idx not in known:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "unknown_channel",
                "error_description": f"no channel at index {idx}",
            },
        )
    if not bot.message_windows.tracks_channel(idx):
        # The UI disables the drill-in on tracked: false, so this guards a
        # hand-crafted request or a tab open across a config change — not a
        # path the client walks.
        raise HTTPException(
            status_code=404,
            detail={
                "error": "channel_not_tracked",
                "error_description": (
                    f"channel {idx} is not served by the bot; message history is "
                    "only kept for the channels it serves"
                ),
            },
        )
    return {
        "channel_idx": idx,
        "window": bot.message_windows.window,
        "since_process_start": True,
        "messages": [m.as_dict() for m in bot.message_windows.channel_messages(idx)],
    }
