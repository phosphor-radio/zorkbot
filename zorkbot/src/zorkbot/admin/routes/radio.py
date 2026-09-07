"""The attached radio: identity, RF settings, contacts, channels, recent traffic.

Mostly read-only. The two channel writes are the console's first mutating
routes outside /auth — see docs/specs/admin-radio-edit.md. Nothing here
transmits, so no request on these routes reaches the RF send gate.

Channel responses are assembled field by field, never forwarded as parsed:
the CHANNEL_INFO payload carries `channel_secret`, the 16-byte pre-shared key,
which must never leave the process. The write path is the mirror image — the
key arrives in a JSON body (never a URL) and appears in no response and no
log line.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request

from zorkbot.admin.deps import get_ctx, require_scope
from zorkbot.radio_state import MAX_CHANNEL_NAME_BYTES, RadioWriteError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["radio"])

_PUBKEY_RE = re.compile(r"^[0-9a-f]{6,64}$")
_SECRET_RE = re.compile(r"^[0-9a-fA-F]{32}$")

# RadioWriteError codes to HTTP statuses. The device is upstream of us, so
# its refusal is a 502 rather than a 400: the request was well formed.
_WRITE_STATUS = {
    "radio_unavailable": 503,
    "write_too_frequent": 429,
    "radio_write_failed": 502,
}


def _fail(status: int, error: str, description: str) -> HTTPException:
    return HTTPException(
        status_code=status, detail={"error": error, "error_description": description}
    )


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
            # Who owns this slot, decided server-side for the same reason
            # `tracked` and `distance_km` are: the rule belongs in one place,
            # not re-derived by every client that draws a row.
            "editable": state.write_enabled and state.channel_role(idx) is None,
        })
    return {
        "channels": listed,
        "free_slots": await state.free_slots(),
        "stale_since": state.stale_since,
    }


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


# ----------------------------------------------------------------------
# Channel writes. Add and remove are the same firmware command: there is no
# delete on this device, only assignment to a slot that always exists.
# See docs/specs/admin-radio-edit.md.
# ----------------------------------------------------------------------


@router.put("/radio/channels/{idx}")
async def put_channel(
    idx: int, request: Request, body: dict | None = None,
    _entry: dict = Depends(require_scope()),
) -> dict:
    bot = get_ctx(request).bot
    state = bot.radio_state
    _require_writes(state)

    name, secret, key_source = _channel_from_body(body)
    existing = await _writable_slot(state, idx)
    if existing is not None and body.get("replace") is not True:
        # The same call adds and overwrites. An add that silently clobbers a
        # channel the operator had forgotten about is a bad way to find out.
        raise _fail(
            409,
            "slot_occupied",
            f"channel {idx} already holds {existing['name']!r}; "
            "pass replace: true to overwrite it",
        )

    try:
        channel = await state.set_channel(idx, name, secret)
    except RadioWriteError as exc:
        raise _fail(_WRITE_STATUS.get(exc.code, 502), exc.code, exc.description)

    # The audit record for the write. Never the key — only where it came from.
    logger.info(
        "radio: admin console set channel %d to %r (was %r, key %s)",
        idx, name, existing["name"] if existing else None, key_source,
    )
    return {
        "idx": idx,
        "name": channel["name"],
        "hash": channel["hash"],
        "key_source": key_source,
        "role": state.channel_role(idx),
        "tracked": bot.message_windows.tracks_channel(idx),
        "editable": True,
    }


@router.delete("/radio/channels/{idx}")
async def delete_channel(
    idx: int, request: Request, _entry: dict = Depends(require_scope())
) -> dict:
    state = get_ctx(request).bot.radio_state
    _require_writes(state)

    existing = await _writable_slot(state, idx)
    if existing is None:
        # Already empty. Idempotent rather than an error, and without a write:
        # every accepted write rewrites the radio's channel file to flash, and
        # there is nothing here to clear.
        return {"idx": idx, "cleared": True}

    try:
        await state.clear_channel(idx)
    except RadioWriteError as exc:
        raise _fail(_WRITE_STATUS.get(exc.code, 502), exc.code, exc.description)

    logger.info("radio: admin console cleared channel %d (was %r)", idx, existing["name"])
    return {"idx": idx, "cleared": True}


def _require_writes(state) -> None:
    if not state.write_enabled:
        raise _fail(
            403,
            "writes_disabled",
            "radio writes are off; set admin_ui.radio_write_enabled = true and restart",
        )


async def _writable_slot(state, idx: int) -> dict | None:
    """Check the slot exists and is ours to write, and return what it holds.

    Returns the current channel, or None for a free slot.
    """
    bound = await state.max_channels()
    if bound is None or not state.connected:
        raise _fail(
            503,
            "radio_unavailable",
            "the radio did not report how many channel slots it has, so writes are refused",
        )
    if not 0 <= idx < bound:
        raise _fail(404, "unknown_channel_slot", f"the radio has no channel slot {idx}")

    role = state.channel_role(idx)
    if role is not None:
        # The bot rewrites its own channels from config on every startup, so
        # a console write here would appear to work and then silently revert.
        # One owner per slot; for these two it is zorkbot.toml.
        section = "[channel]" if role == "zork" else "[bots_channel]"
        raise _fail(
            409,
            "channel_is_served",
            f"channel {idx} is the bot's {role} channel and is owned by {section} in "
            "zorkbot.toml; change it there and restart",
        )
    return next((c for c in await state.channels() if c["idx"] == idx), None)


def _channel_from_body(body: dict) -> tuple[str, bytes | None, str]:
    """Validate a PUT body into (name, secret, key_source).

    A secret of None means the library derives the key from the name, which is
    correct for a "#" channel and dangerous for anything else — see
    docs/specs/admin-radio-edit.md, "A missing secret is not a random secret".
    """
    if not isinstance(body, dict):
        raise _fail(400, "invalid_name", "a JSON object with a name is required")

    name = body.get("name")
    if not isinstance(name, str):
        raise _fail(400, "invalid_name", "name is required")
    name = name.strip()
    if not name:
        raise _fail(400, "invalid_name", "name is required")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        raise _fail(400, "invalid_name", "name must not contain control characters")
    # On the encoded length, not the character count: the client truncates
    # with encode()[:32], which will happily split a multi-byte codepoint.
    if len(name.encode("utf-8")) > MAX_CHANNEL_NAME_BYTES:
        raise _fail(
            400,
            "name_too_long",
            f"name must be at most {MAX_CHANNEL_NAME_BYTES} bytes as UTF-8; "
            "the radio silently truncates anything longer",
        )

    secret = body.get("secret")
    if name.startswith("#"):
        if secret:
            raise _fail(
                400,
                "secret_not_used",
                "a channel whose name starts with '#' derives its key from the name, so the "
                "key given here would be ignored — and the channel is public either way",
            )
        return name, None, "derived"

    if not secret:
        raise _fail(
            400,
            "secret_required",
            "a channel whose name does not start with '#' needs a 16-byte key; without one "
            "the radio would derive the key from the name, and anyone who can read the name "
            "could compute it",
        )
    if not isinstance(secret, str) or not _SECRET_RE.match(secret):
        raise _fail(400, "invalid_secret", "the key must be 32 hex characters (16 bytes)")
    return name, bytes.fromhex(secret), "provided"
