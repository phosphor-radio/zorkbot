"""!bots command handler — mesh bot discovery / roll call."""

from __future__ import annotations

import asyncio

from zorkbot.context import Context

# Delay before replying, so that when multiple bots on the mesh answer the
# same roll-call broadcast, their replies don't collide on the air.
REPLY_DELAY_SECONDS = 5.0

REPLY_TEXT = "zorkbot - A Zork I game server.\n!help for details"


async def handle_bots(ctx: Context) -> None:
    await asyncio.sleep(REPLY_DELAY_SECONDS)
    await ctx.reply(REPLY_TEXT)
