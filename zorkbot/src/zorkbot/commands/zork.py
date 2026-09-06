"""In-game command handler for DM sessions."""

from __future__ import annotations

import logging

from zorkbot.context import Context
from zorkbot.game_client import GameClient, GameServiceError, SessionNotFoundError
from zorkbot.packetize import (
    DEFAULT_MAX_CHARS,
    add_sequence_prefixes,
    pack_lines,
    packetize,
)
from zorkbot.sanitize import NotAllowedError, validate
from zorkbot.session_state import SessionState

logger = logging.getLogger(__name__)

# Manually-grouped packets, each under 120 chars with newlines preserved.
# ASCII hyphens rather than em-dashes throughout: an em-dash costs 3 bytes
# to a hyphen's 1, and the packet budget is counted in characters while the
# radio counts bytes.
_HELP_LINES_1 = [
    "!start - begin/resume game",
    "!end - save & quit",
    "!list - active sessions",
    "!watch <N> - observe session",
]
_HELP_PACKET_1 = "\n".join(_HELP_LINES_1)

# Channel !help - !reset is DM-only, so it's omitted here. !author and
# !uptime are channel-only, so they're shown here and not in the DM packets.
_CHANNEL_HELP_LINES_2 = [
    "!watchers - list all observers",
    "!author - bot info & source",
    "!uptime - bot uptime",
]
_HELP_PACKETS = [_HELP_PACKET_1, "\n".join(_CHANNEL_HELP_LINES_2)]
HELP_TEXT = "\n".join(_HELP_PACKETS)


def channel_help_packets(max_chars: int = DEFAULT_MAX_CHARS) -> list[str]:
    return add_sequence_prefixes(
        [_HELP_PACKET_1, *pack_lines(_CHANNEL_HELP_LINES_2, max_chars)]
    )


# DM !help, packet 2. Ends with a pointer back to the game channel, since a DM
# session gives no other hint that #zork (or whatever it's configured as)
# exists - parametrized on channel_name rather than hardcoded, so it stays
# correct if [channel].name is changed. !rules only applies to a player with
# an active session, so it's folded in for that case rather than shown always.
def _dm_help_lines_2(channel_name: str, *, in_session: bool) -> list[str]:
    lines = ["!watchers - list all observers", "!reset - wipe save & restart"]
    if in_session:
        lines.append("!rules - basic rules")
    lines.append(f"Join {channel_name} and send !help for more info")
    return lines


def dm_help_packets(
    channel_name: str, *, in_session: bool, max_chars: int = DEFAULT_MAX_CHARS
) -> list[str]:
    # Packed rather than hand-grouped: the sequence prefix and a long
    # [channel].name both eat into the budget, and one more packet is
    # better than one packet over the radio's limit.
    lines = _dm_help_lines_2(channel_name, in_session=in_session)
    return add_sequence_prefixes([_HELP_PACKET_1, *pack_lines(lines, max_chars)])

AUTHOR_TEXT = """Meshcore: phr5\U0001f427
Discord: @phosphor_radio
Source: https://github.com/phosphor-radio/zorkbot"""


async def send_initial_look(
    ctx: Context,
    game: GameClient,
    player_id: str,
    send_dm_func=None,   # async (pubkey_prefix, text) -> bool, required if not ctx.is_dm
) -> bool:
    """Silently issue a `look` after !start/!reset and forward the room
    description to the player's DM, so they immediately see where they are.

    False when the look was cut short by a delivery failure."""
    try:
        result = await game.command(player_id, "look")
    except (GameServiceError, SessionNotFoundError):
        logger.warning("initial look failed player=%s", player_id)
        return True

    if not result.ok:
        return True

    packets = packetize(result.output, max_chars=ctx.config.packet_max_chars)
    if not packets:
        return True

    if ctx.is_dm:
        return await ctx.reply_many(packets)

    for packet in packets:
        # Only an explicit False is a measured delivery failure; see
        # Context.reply_many for why None must not count as one.
        if await send_dm_func(player_id, packet) is False and (
            ctx.config.dm_ack_abandon_response
        ):
            logger.warning(
                "initial look cut short player=%s - packet not delivered",
                player_id[:8],
            )
            return False
    return True


async def handle_game_command(
    ctx: Context,
    game: GameClient,
    state: SessionState,
    command_text: str,
    send_watcher_dm_func,   # async (pubkey_prefix, text) -> None
    fanout_func,    # (session_num, coroutine) -> None — ordered watcher fan-out
) -> None:
    """Process a bare game command from a DM session.

    The player's own reply goes out through ctx.reply_many; the sender passed
    here is only ever used for watcher fan-out, which is why it is the
    fire-and-forget one."""
    player_id = ctx.pubkey_prefix
    if not player_id:
        await ctx.reply("Cannot identify you - please send an Advert.")
        return

    record = state.get_session(player_id)
    if record is None:
        await ctx.reply(
            "No active session - send !start to begin."
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
        state.remove_session(player_id, reason="server_side")
        await ctx.reply(
            "Your session has ended due to inactivity. Send !start to resume."
        )
        return
    except GameServiceError as exc:
        msg = str(exc)
        if "busy" in msg.lower():
            await ctx.reply("The game is busy - try again in a moment.")
        else:
            await ctx.reply(f"Game error: {msg}")
        return

    if not result.ok:
        await ctx.reply(result.error or "That command isn't allowed.")
        return

    output = result.output
    state.event_sink.transcript(
        session_num=record.num,
        player_name=record.player_name,
        command=command_text,
        output=output,
    )

    packets = packetize(output, max_chars=ctx.config.packet_max_chars)
    if not packets:
        return

    # Send to the player.
    await ctx.reply_many(packets)

    # Hand fan-out to the session's fan-out queue rather than awaiting it.
    # The player already has their reply; watcher delivery is a side effect
    # for a third party and must not hold the player's own worker "busy" —
    # bot.py's pending-command gate drops a player's next message for as
    # long as their worker is running, so awaiting fan-out here would make
    # every watcher an unwitting tax on how fast the player being watched
    # can act. The queue keeps this session's fan-out in command order even
    # though the player is now free to send the next one immediately.
    if record.watchers:
        watcher_packets = packetize(
            output,
            max_chars=ctx.config.packet_max_chars,
            first_line=f"[{record.player_name}] > {command_text}",
        )

        async def _notify_watchers() -> None:
            for watcher_id in list(record.watchers):
                for packet in watcher_packets:
                    await send_watcher_dm_func(watcher_id, packet)

            logger.debug(
                "game fan-out session=%d watchers=%d packets=%d",
                record.num, len(record.watchers), len(watcher_packets),
            )

        fanout_func(record.num, _notify_watchers())
