"""!bots command handler — mesh bot discovery roll call.

Only reachable from the dedicated bots-discovery channel (see
config.bots_channel / bot.dispatch_bots_channel) — never via the game
channel or DM.
"""

from __future__ import annotations

import asyncio
import random

from zorkbot.context import Context

# Base delay plus jitter before replying, so that when multiple bots on the
# mesh answer the same roll-call broadcast, their replies are less likely to
# collide on the air.
REPLY_DELAY_BASE_SECONDS = 5.0
REPLY_DELAY_JITTER_SECONDS = 5.0


def build_reply_text(game_channel_name: str) -> str:
    return (
        "zorkbot - private Zork I game sessions over mesh DMs.\n"
        f"Join {game_channel_name} and send !help for commands."
    )


async def handle_bots(ctx: Context) -> None:
    delay = REPLY_DELAY_BASE_SECONDS + random.uniform(0, REPLY_DELAY_JITTER_SECONDS)
    await asyncio.sleep(delay)
    await ctx.reply(build_reply_text(ctx.config.channel.name))
