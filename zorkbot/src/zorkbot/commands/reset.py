"""!reset command handler — DM only."""

from __future__ import annotations

import logging

from zorkbot.commands.zork import send_initial_look
from zorkbot.context import Context
from zorkbot.game_client import GameClient, GameServiceError, SessionFullError
from zorkbot.session_state import SessionState

logger = logging.getLogger(__name__)


async def handle_reset(ctx: Context, game: GameClient, state: SessionState) -> None:
    if not ctx.is_dm:
        await ctx.reply("!reset is only available via DM.")
        return

    player_id = ctx.pubkey_prefix
    if not player_id:
        await ctx.reply("Cannot identify you - please send an Advert.")
        return

    # Remove any existing session from state (no end_session call; reset handles it).
    record = state.remove_session(player_id, reason="reset")
    old_num = record.num if record else None

    player_name = ctx.sender_name or player_id[:8]

    try:
        await game.reset_session(player_id)
    except SessionFullError:
        await ctx.reply("All game slots are active - could not restart. Try again shortly.")
        return
    except GameServiceError as exc:
        await ctx.reply(f"Reset failed: {exc}")
        return

    new_record = state.add_session(player_id, player_name)
    logger.info(
        "reset player=%s old_session=%s new_session=%d",
        player_id, old_num, new_record.num,
    )
    delivered = await ctx.reply(
        f"Game reset. Zork I Session #{new_record.num} started fresh."
    )
    # Same rule as !start: the look is several more packets down the link the
    # confirmation just failed on, and the reset itself has already happened.
    if not delivered and ctx.config.dm_ack_abandon_response:
        logger.warning(
            "session=%d reset confirmation not delivered to player=%s - "
            "skipping initial look",
            new_record.num, player_id[:8],
        )
        return
    await send_initial_look(ctx, game, player_id)
