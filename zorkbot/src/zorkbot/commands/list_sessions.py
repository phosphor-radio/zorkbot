"""!list command handler — lists active sessions."""

from __future__ import annotations

import time

from zorkbot.context import Context
from zorkbot.session_state import SessionState


async def handle_list(ctx: Context, state: SessionState) -> None:
    sessions = state.all_sessions()
    if not sessions:
        await ctx.reply("No active sessions right now.")
        return

    # Sort by session number for stable output.
    sessions.sort(key=lambda s: s.num)

    parts = []
    now = time.monotonic()
    for s in sessions:
        elapsed = int(now - s.started_at)
        mins = elapsed // 60
        secs = elapsed % 60
        age = f"{mins}m" if mins else f"{secs}s"
        parts.append(f"#{s.num} {s.player_name} ({age})")

    await ctx.reply("Active sessions: " + "  ".join(parts))
