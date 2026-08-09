"""!zork command handler."""

from __future__ import annotations

import asyncio
import logging

from zorkbot.config import BotConfig
from zorkbot.context import Context
from zorkbot.game_client import GameClient, GameServiceError
from zorkbot.packetize import packetize
from zorkbot.sanitize import NotAllowedError, validate

logger = logging.getLogger(__name__)

HELP_TEXT = """!zork help — this message
!help / !commands — same as !zork help
!zork — game status
!zork <command> — play Zork I

e.g. !zork look

@[zorkbot] may prefix any command.

Everyone shares one game."""


def is_admin(sender_name: str | None, token: str | None, config: BotConfig) -> bool:
    if token and config.admin_token and token == config.admin_token:
        return True
    if sender_name and sender_name.lower() in config.admin_names:
        return True
    return False


def _sender_prefix(sender_name: str | None) -> str:
    if not sender_name:
        return ""
    return f"@[{sender_name}] "


async def handle_zork(
    ctx: Context,
    game: GameClient,
    game_lock: asyncio.Lock,
) -> None:
    args = ctx.args.strip()

    if not args:
        await _reply_status(ctx, game)
        return

    if args.lower() == "help":
        await ctx.reply(HELP_TEXT)
        return

    parts = args.split()
    subcommand = parts[0].lower()
    token = parts[1] if len(parts) > 1 else None

    if subcommand in {"save", "restore", "reset"}:
        await _handle_admin(ctx, game, game_lock, subcommand, token)
        return

    await _handle_game_command(ctx, game, game_lock, args)


async def _reply_status(ctx: Context, game: GameClient) -> None:
    try:
        status = await game.status()
    except GameServiceError as exc:
        await ctx.reply(f"Game service unavailable: {exc}")
        return

    busy = "busy" if status.busy else "ready"
    await ctx.reply(
        f"Zork I is {busy} (uptime {status.uptime}). Try !zork look"
    )


async def _handle_admin(
    ctx: Context,
    game: GameClient,
    game_lock: asyncio.Lock,
    subcommand: str,
    token: str | None,
) -> None:
    if not is_admin(ctx.sender_name, token, ctx.config):
        await ctx.reply("You are not authorized for that command.")
        return

    if subcommand == "reset":
        async with game_lock:
            try:
                await game.reset()
            except GameServiceError as exc:
                await ctx.reply(f"Reset failed: {exc}")
                return
        await ctx.reply("Game reset.")
        return

    async with game_lock:
        try:
            result = await game.command(subcommand, admin=True)
        except GameServiceError as exc:
            await ctx.reply(str(exc))
            return

    if not result.ok:
        await ctx.reply(result.error or "That command isn't allowed.")
        return

    await _reply_game_output(ctx, result.output)


async def _handle_game_command(
    ctx: Context,
    game: GameClient,
    game_lock: asyncio.Lock,
    command_text: str,
) -> None:
    try:
        validate(command_text, admin=False)
    except NotAllowedError:
        await ctx.reply("That command isn't allowed.")
        return

    if game_lock.locked():
        await ctx.reply("The game is busy, try again.")
        return

    async with game_lock:
        try:
            result = await game.command(command_text, admin=False)
        except GameServiceError as exc:
            message = str(exc)
            if "busy" in message.lower():
                await ctx.reply("The game is busy, try again.")
            else:
                await ctx.reply(f"Game error: {message}")
            return

    if not result.ok:
        await ctx.reply(result.error or "That command isn't allowed.")
        return

    await _reply_game_output(ctx, result.output)


async def _reply_game_output(ctx: Context, output: str) -> None:
    packets = packetize(
        output,
        max_chars=ctx.config.packet_max_chars,
        prefix=_sender_prefix(ctx.sender_name),
    )
    if not packets:
        return
    await ctx.reply_many(packets)
