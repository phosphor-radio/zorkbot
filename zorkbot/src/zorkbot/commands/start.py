"""!start command handler."""

from __future__ import annotations

import logging

from zorkbot.context import Context
from zorkbot.game_client import GameClient, SessionFullError, GameServiceError
from zorkbot.session_state import SessionState

logger = logging.getLogger(__name__)

AUTHOR_TEXT = """Meshcore: phr5\U0001f427
Discord: @phosphor_radio
Source: https://github.com/phosphor-radio/zorkbot"""

HELP_TEXT = """\
Commands (channel or DM):
!start  — begin / resume your game
!end    — save & end your game
!list   — show active sessions
!watch <N> — observe session N
!watchers  — list all watchers
!reset  — (DM only) wipe save & restart
!end <N>   — (admin) force-end session N
In your DM: type game commands directly\
"""


async def handle_start(
    ctx: Context,
    game: GameClient,
    state: SessionState,
    send_dm_func,        # async (pubkey_prefix, text) -> None
    send_advert_func,    # async () -> None
    meshcore,            # MeshCore instance for contact lookup
) -> None:
    player_id = ctx.pubkey_prefix
    if not player_id:
        await ctx.reply("Cannot identify you — please DM me directly.")
        return

    # Check one-active-state-per-player rule.
    active = state.active_state(player_id)
    if active == "playing":
        session = state.get_session(player_id)
        num = session.num if session else "?"
        await ctx.reply(f"You already have an active session (#{num}). Send !end to stop.")
        return
    if active == "watching":
        watched_num = state.watching_session(player_id)
        await ctx.reply(
            f"You are currently watching session #{watched_num}. Send !end first."
        )
        return

    # For channel !start: verify we can DM back.
    if not ctx.is_dm:
        contact = meshcore.get_contact_by_key_prefix(player_id)
        if contact is None:
            await ctx.reply(
                "DM me !start — I don't have you in my contacts yet."
            )
            return

    # Send advert so the player can add us if needed.
    await send_advert_func()

    try:
        await game.start_session(player_id)
    except SessionFullError:
        await ctx.reply("All game slots are active right now — try again shortly.")
        return
    except GameServiceError as exc:
        await ctx.reply(f"Could not start game: {exc}")
        return

    player_name = ctx.sender_name or player_id[:8]
    record = state.add_session(player_id, player_name)

    logger.info(
        "session started player=%s name=%s num=%d",
        player_id, player_name, record.num,
    )

    intro = (
        f"Session #{record.num} started. Type your commands here in DM!"
    )
    if ctx.is_dm:
        await ctx.reply(intro)
    else:
        # Acknowledge on channel, send intro via DM.
        await ctx.reply(f"Session #{record.num} started for {player_name} — check your DMs!")
        await send_dm_func(player_id, intro)
