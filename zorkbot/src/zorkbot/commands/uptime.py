"""!uptime command handler — channel-only bot process uptime."""

from __future__ import annotations

from zorkbot.context import Context


def format_uptime(total_seconds: float) -> str:
    total_seconds = int(total_seconds)
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


async def handle_uptime(ctx: Context, uptime_seconds: float) -> None:
    await ctx.reply(f"Uptime: {format_uptime(uptime_seconds)}")
