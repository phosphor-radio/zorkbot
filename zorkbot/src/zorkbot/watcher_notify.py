"""Shared helper for telling a session's watchers that it has ended.

Used both when a player (or admin) ends a session directly, and by
ZorkBot's session poller when the game service ends one server-side
(inactivity timeout, PTY crash) with no other way to signal the bot.
Only watchers are notified — never the player or the game channel.
"""

from __future__ import annotations

from zorkbot.session_state import SessionRecord


async def notify_watchers_session_ended(
    send_dm_func,   # async (pubkey_prefix, text) -> None
    record: SessionRecord,
) -> None:
    if not record.watchers:
        return
    text = (
        f"Zork I Session #{record.num} ({record.player_name}) has ended. "
        "You are no longer watching."
    )
    for watcher_id in list(record.watchers):
        await send_dm_func(watcher_id, text)
