"""!watch command handler."""

from __future__ import annotations

import logging

from zorkbot.context import Context
from zorkbot.session_state import SessionState

logger = logging.getLogger(__name__)


async def handle_watch(ctx: Context, state: SessionState, args: str) -> None:
    player_id = ctx.pubkey_prefix
    if not player_id:
        await ctx.reply("Cannot identify you.")
        return

    # Parse session number.
    arg = args.strip()
    if not arg:
        await ctx.reply("Usage: !watch <session number>  (see !list for active sessions)")
        return
    try:
        session_num = int(arg)
    except ValueError:
        await ctx.reply(f"Invalid session number: {arg!r}")
        return

    # Check one-active-state-per-player rule.
    active = state.active_state(player_id)
    if active == "playing":
        session = state.get_session(player_id)
        num = session.num if session else "?"
        await ctx.reply(
            f"You have an active session (#{num}). Send !end first to start watching."
        )
        return
    if active == "watching":
        watched_num = state.watching_session(player_id)
        await ctx.reply(
            f"You are already watching session #{watched_num}. Send !end first."
        )
        return

    # Cannot watch own session.
    own = state.get_session(player_id)
    if own is not None and own.num == session_num:
        await ctx.reply("You cannot watch your own session.")
        return

    error = state.add_watcher(player_id, session_num)
    if error:
        await ctx.reply(error)
        return

    record = state.get_session_by_num(session_num)
    player_name = record.player_name if record else str(session_num)
    logger.info(
        "watcher=%s watching session=%d player=%s",
        player_id, session_num, player_name,
    )
    await ctx.reply(
        f"Watching session #{session_num} ({player_name}). "
        f"Game output will arrive in your DMs. Send !end to stop."
    )
