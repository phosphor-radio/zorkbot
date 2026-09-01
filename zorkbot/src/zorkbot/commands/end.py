"""!end command handler.

Handles:
- !end              — end own session or stop watching
- !end <N>          — admin-only force-end of session N
"""

from __future__ import annotations

import logging

from zorkbot.context import Context
from zorkbot.game_client import GameClient, GameServiceError, SessionNotFoundError
from zorkbot.session_state import SessionState
from zorkbot.watcher_notify import notify_watchers_session_ended

logger = logging.getLogger(__name__)


async def handle_end(
    ctx: Context,
    game: GameClient,
    state: SessionState,
    args: str,
    send_dm_func,   # async (pubkey_prefix, text) -> None
) -> None:
    player_id = ctx.pubkey_prefix
    if not player_id:
        await ctx.reply("Cannot identify you — please send an Advert.")
        return

    # Admin force-end: !end <N>
    if args.strip():
        await _handle_admin_end(ctx, game, state, args.strip(), send_dm_func)
        return

    active = state.active_state(player_id)

    if active == "watching":
        session_num = state.remove_watcher(player_id)
        logger.info("watcher=%s stopped watching session=%s", player_id, session_num)
        await ctx.reply(f"Stopped watching session #{session_num}.")
        return

    if active == "playing":
        record = state.remove_session(player_id)
        num = record.num if record else "?"
        try:
            await game.end_session(player_id)
        except (GameServiceError, SessionNotFoundError) as exc:
            logger.warning("end_session failed for player=%s: %s", player_id, exc)
        logger.info("session=%s ended by player=%s", num, player_id)
        await ctx.reply(f"Zork I Session #{num} saved and ended.")
        if record is not None:
            await notify_watchers_session_ended(send_dm_func, record)
        return

    await ctx.reply("You don't have an active session or watch to end.")


async def _handle_admin_end(
    ctx: Context,
    game: GameClient,
    state: SessionState,
    arg: str,
    send_dm_func,   # async (pubkey_prefix, text) -> None
) -> None:
    if not ctx.is_admin():
        await ctx.reply("You are not authorized for that command.")
        return

    try:
        session_num = int(arg)
    except ValueError:
        await ctx.reply(f"Invalid session number: {arg!r}")
        return

    record = state.get_session_by_num(session_num)
    if record is None:
        await ctx.reply(f"Zork I Session #{session_num} is not active.")
        return

    target_id = record.player_id
    state.remove_session(target_id)
    try:
        await game.end_session(target_id)
    except (GameServiceError, SessionNotFoundError) as exc:
        logger.warning("admin end_session failed for player=%s: %s", target_id, exc)

    logger.info(
        "session=%d force-ended by admin=%s", session_num, ctx.pubkey_prefix
    )
    await ctx.reply(f"Zork I Session #{session_num} ({record.player_name}) has been ended.")
    await notify_watchers_session_ended(send_dm_func, record)
