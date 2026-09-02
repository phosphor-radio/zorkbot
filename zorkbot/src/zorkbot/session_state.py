"""In-memory session and watcher state.

Tracks which pubkey_prefix owns which session number, which players are
watching which session, and enforces the one-active-state-per-player rule
(playing XOR watching, never both).

Session numbers are assigned sequentially and are not reused within a
process lifetime.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from zorkbot.admin.events import EventSink


@dataclass
class SessionRecord:
    """Describes one active playing session."""
    num: int
    player_id: str
    player_name: str
    # Monotonic clock — correct for duration math even across a wall-clock
    # step. Never persist this; see started_wall_at for that.
    started_at: float = field(default_factory=time.monotonic)
    # Wall clock (Unix seconds) at start — what the admin UI persists and
    # displays. Kept alongside started_at rather than instead of it: mixing
    # the two (durations from wall clock, timestamps from monotonic) is the
    # easiest bug to write here.
    started_wall_at: float = field(default_factory=time.time)
    # pubkey_prefixes of current watchers for this session.
    watchers: set[str] = field(default_factory=set)


class SessionState:
    """Thread-safe (asyncio single-threaded) registry of sessions and watchers.

    Rules enforced:
    - A player may have at most one active session.
    - A player may watch at most one session at a time.
    - Playing and watching are mutually exclusive.
    - max_watchers_per_session limits observers per session.
    """

    def __init__(
        self,
        max_watchers_per_session: int = 2,
        event_sink: "EventSink | None" = None,
    ) -> None:
        self._max_watchers = max_watchers_per_session
        if event_sink is None:
            from zorkbot.admin.events import NullEventSink

            event_sink = NullEventSink()
        self._sink = event_sink
        # player_id → SessionRecord (playing sessions)
        self._sessions: dict[str, SessionRecord] = {}
        # session_num → player_id (reverse index)
        self._num_to_player: dict[int, str] = {}
        # player_id → session_num being watched
        self._watching: dict[str, int] = {}
        self._next_num: int = 1

    @property
    def event_sink(self) -> "EventSink":
        return self._sink

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def add_session(self, player_id: str, player_name: str) -> SessionRecord:
        """Register a new playing session.  Caller must have already verified
        the player has no active state."""
        num = self._next_num
        self._next_num += 1
        record = SessionRecord(num=num, player_id=player_id, player_name=player_name)
        self._sessions[player_id] = record
        self._num_to_player[num] = player_id
        self._sink.session_started(record)
        return record

    def remove_session(
        self, player_id: str, reason: str = "unknown"
    ) -> Optional[SessionRecord]:
        """Remove and return the session record for player_id, or None.

        *reason* is one of player_end | admin_end | reset | server_side |
        shutdown — recorded as the admin UI's session-history end_reason.
        """
        record = self._sessions.pop(player_id, None)
        if record is not None:
            self._num_to_player.pop(record.num, None)
            # Remove all watchers whose watch target no longer exists.
            for watcher_id in list(record.watchers):
                self._watching.pop(watcher_id, None)
            self._sink.session_ended(record, reason)
        return record

    def get_session(self, player_id: str) -> Optional[SessionRecord]:
        """Return the session record for player_id, or None."""
        return self._sessions.get(player_id)

    def get_session_by_num(self, num: int) -> Optional[SessionRecord]:
        """Return the session record for session number, or None."""
        player_id = self._num_to_player.get(num)
        if player_id is None:
            return None
        return self._sessions.get(player_id)

    def all_sessions(self) -> list[SessionRecord]:
        return list(self._sessions.values())

    # ------------------------------------------------------------------
    # Watcher lifecycle
    # ------------------------------------------------------------------

    def add_watcher(self, watcher_id: str, session_num: int) -> str | None:
        """Add watcher_id as an observer of session_num.

        Returns None on success, or an error message string if the operation
        cannot proceed.
        """
        record = self.get_session_by_num(session_num)
        if record is None:
            return f"Zork I Session #{session_num} is not active."
        if len(record.watchers) >= self._max_watchers:
            return f"Zork I Session #{session_num} already has the maximum number of watchers."
        record.watchers.add(watcher_id)
        self._watching[watcher_id] = session_num
        self._sink.watchers_changed(record)
        return None

    def remove_watcher(self, watcher_id: str) -> Optional[int]:
        """Remove watcher_id from whatever session they are watching.

        Returns the session number they were watching, or None.
        """
        session_num = self._watching.pop(watcher_id, None)
        if session_num is not None:
            record = self.get_session_by_num(session_num)
            if record is not None:
                record.watchers.discard(watcher_id)
                self._sink.watchers_changed(record)
        return session_num

    def watching_session(self, watcher_id: str) -> Optional[int]:
        """Return the session number being watched by watcher_id, or None."""
        return self._watching.get(watcher_id)

    def all_watchers(self) -> list[tuple[str, int]]:
        """Return (watcher_id, session_num) pairs for all active watchers."""
        return list(self._watching.items())

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------

    def active_state(self, player_id: str) -> str:
        """Return 'playing', 'watching', or 'none'."""
        if player_id in self._sessions:
            return "playing"
        if player_id in self._watching:
            return "watching"
        return "none"

    def watchers_for_session(self, session_num: int) -> set[str]:
        """Return the set of watcher pubkey_prefixes for a session."""
        record = self.get_session_by_num(session_num)
        if record is None:
            return set()
        return set(record.watchers)
