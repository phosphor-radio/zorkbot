"""Soft per-sender rate limiting."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    """Outcome of a rate-limit check.

    `notify` is true only on the first rejection of a throttling episode, so a
    burst costs one "slow down" packet rather than one per message. Every reply
    goes out through the runner's global send gate, which spaces transmissions
    by `send_spacing_seconds`; replying to each message of a burst would hand a
    single sender that many consecutive transmit slots and starve everyone else.
    """

    allowed: bool
    notify: bool = False


_ALLOWED = Decision(allowed=True)


class RateLimiter:
    """Enforce a minimum interval between commands from the same sender.

    Keys on pubkey_prefix (12-char hex) which is the stable cryptographic
    identity available from both channel and DM events.
    """

    def __init__(self, interval_seconds: float) -> None:
        self._interval = interval_seconds
        self._last_at: dict[str, float] = {}
        # Senders already told to slow down in the current episode. Cleared as
        # soon as they comply, so a later burst is announced again.
        self._notified: set[str] = set()

    def check(self, pubkey_prefix: str | None, *, exempt: bool = False) -> Decision:
        if exempt or self._interval <= 0:
            return _ALLOWED

        key = (pubkey_prefix or "").lower()
        now = time.monotonic()
        last = self._last_at.get(key)
        if last is not None and now - last < self._interval:
            first_of_episode = key not in self._notified
            self._notified.add(key)
            return Decision(allowed=False, notify=first_of_episode)

        self._last_at[key] = now
        self._notified.discard(key)
        return _ALLOWED
