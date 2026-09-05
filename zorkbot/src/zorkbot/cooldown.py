"""Fixed-window cooldown for broadcast replies."""

from __future__ import annotations

import time


class Cooldown:
    """Allow an action at most once per `interval_seconds`.

    Global rather than per-sender, which suits a broadcast roll call: several
    players asking in quick succession should draw one reply for the channel,
    not one each. Requests inside an open window are dropped in silence — a
    "wait your turn" packet would cost the same airtime as the reply it
    replaces.
    """

    def __init__(self, interval_seconds: float) -> None:
        self._interval = interval_seconds
        self._last_at: float | None = None

    def claim(self) -> bool:
        """Open a new window and return True, or return False if one is open."""
        if self._interval <= 0:
            return True

        now = time.monotonic()
        if self._last_at is not None and now - self._last_at < self._interval:
            return False

        self._last_at = now
        return True
