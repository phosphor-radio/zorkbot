"""!rules command handler — DM only, requires an active (playing) session."""

from __future__ import annotations

from zorkbot.context import Context
from zorkbot.session_state import SessionState

RULES_TEXT = (
    "You are in Zork I - a text adventure\n"
    "Use commands such as:\n"
    "look\n"
    "look at leaflet\n"
    "north\n"
    "ne\n"
    "inventory\n"
    "take axe"
)


async def handle_rules(ctx: Context, state: SessionState) -> None:
    if not ctx.is_dm:
        await ctx.reply("!rules is only available via DM.")
        return

    player_id = ctx.pubkey_prefix
    if not player_id:
        await ctx.reply("Cannot identify you.")
        return

    if state.active_state(player_id) != "playing":
        await ctx.reply("No active session — send !start to begin.")
        return

    await ctx.reply(RULES_TEXT)
