"""!watchers command handler — lists all active watchers."""

from __future__ import annotations

from zorkbot.context import Context
from zorkbot.session_state import SessionState


async def handle_watchers(ctx: Context, state: SessionState, meshcore) -> None:
    pairs = state.all_watchers()
    if not pairs:
        await ctx.reply("No active watchers right now.")
        return

    # Sort by session number then watcher id for stable output.
    pairs.sort(key=lambda p: (p[1], p[0]))

    parts = []
    for watcher_id, session_num in pairs:
        contact = meshcore.get_contact_by_key_prefix(watcher_id)
        name = contact["adv_name"] if contact else watcher_id[:8]
        parts.append(f"{name} → #{session_num}")

    await ctx.reply("Watchers: " + "  ".join(parts))
