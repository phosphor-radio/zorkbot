"""In-game command handler for DM sessions."""

from __future__ import annotations

import logging

from zorkbot.context import Context
from zorkbot.game_client import GameClient, GameServiceError, SessionNotFoundError
from zorkbot.packetize import packetize
from zorkbot.sanitize import NotAllowedError, validate
from zorkbot.session_state import SessionState

logger = logging.getLogger(__name__)

# Manually-grouped packets, each under 120 chars with newlines preserved.
_HELP_PACKET_1 = (
    "!start — begin or resume game\n!end — save & quit\n"
    "!list — active sessions\n!watch <N> — observe a session"
)

# Channel !help — !reset is DM-only, so it's omitted here. !author and
# !uptime are channel-only, so they're shown here and not in the DM packets.
_HELP_PACKETS = [
    _HELP_PACKET_1,
    "!watchers — list all observers\n!author — bot info & source\n!uptime — bot uptime",
]
HELP_TEXT = "\n".join(_HELP_PACKETS)

# DM !help (no active session) — includes !reset since it applies here.
_HELP_PACKETS_DM = [
    _HELP_PACKET_1,
    "!watchers — list all observers\n!reset — wipe save & restart",
]

# Shown instead of _HELP_PACKETS_DM for !help from a DM with an active
# (non-watching) session, so a playing player also sees !rules. Folded into
# the last packet (kept short) rather than its own, to stay under 120 chars.
_HELP_PACKETS_IN_SESSION = _HELP_PACKETS_DM[:-1] + [
    _HELP_PACKETS_DM[-1] + "\n!rules — basic rules",
]

AUTHOR_TEXT = """Meshcore: phr5\U0001f427
Discord: @phosphor_radio
Source: https://github.com/phosphor-radio/zorkbot"""


async def handle_game_command(
    ctx: Context,
    game: GameClient,
    state: SessionState,
    command_text: str,
    send_dm_func,   # async (pubkey_prefix, text) -> None
) -> None:
    """Process a bare game command from a DM session."""
    player_id = ctx.pubkey_prefix
    if not player_id:
        await ctx.reply("Cannot identify you.")
        return

    record = state.get_session(player_id)
    if record is None:
        await ctx.reply(
            "No active session — send !start to begin."
        )
        return

    try:
        validate(command_text, admin=False)
    except NotAllowedError:
        await ctx.reply("That command isn't allowed.")
        return

    try:
        result = await game.command(player_id, command_text)
    except SessionNotFoundError:
        # Session was ended server-side (e.g. inactivity timeout).
        state.remove_session(player_id)
        await ctx.reply(
            "Your session has ended due to inactivity. Send !start to resume."
        )
        return
    except GameServiceError as exc:
        msg = str(exc)
        if "busy" in msg.lower():
            await ctx.reply("The game is busy — try again in a moment.")
        else:
            await ctx.reply(f"Game error: {msg}")
        return

    if not result.ok:
        await ctx.reply(result.error or "That command isn't allowed.")
        return

    output = result.output
    packets = packetize(output, max_chars=ctx.config.packet_max_chars)
    if not packets:
        return

    # Send to the player.
    await ctx.reply_many(packets)

    # Fan-out to watchers.
    if record.watchers:
        watcher_prefix = f"[{record.player_name}] > {command_text}\n"
        watcher_packets = packetize(
            watcher_prefix + output,
            max_chars=ctx.config.packet_max_chars,
        )
        for watcher_id in list(record.watchers):
            for packet in watcher_packets:
                await send_dm_func(watcher_id, packet)

        logger.debug(
            "game fan-out session=%d watchers=%d packets=%d",
            record.num, len(record.watchers), len(watcher_packets),
        )
