"""Soft per-sender rate limiting."""

from __future__ import annotations

import time


class RateLimiter:
    """Enforce a minimum interval between commands from the same sender."""

    def __init__(self, interval_seconds: float) -> None:
        self._interval = interval_seconds
        self._last_at: dict[str, float] = {}

    def allow(self, sender_name: str | None, *, exempt: bool = False) -> bool:
        if exempt or self._interval <= 0:
            return True

        key = (sender_name or "").lower()
        now = time.monotonic()
        last = self._last_at.get(key)
        if last is not None and now - last < self._interval:
            return False

        self._last_at[key] = now
        return True
