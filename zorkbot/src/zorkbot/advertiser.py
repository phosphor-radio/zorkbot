"""Cooldown-gated MeshCore advert sender.

Ensures the bot sends a flood advert on every !start and on a background
timer, but never more than once per advert_cooldown_seconds.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class Advertiser:
    """Manages periodic and on-demand advert sending with a cooldown gate.

    When *enabled* is False all methods are no-ops; no adverts are sent and
    no background task is started.  Set advert_enabled = true in zorkbot.toml
    for a live server.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        interval_seconds: int = 300,
        cooldown_seconds: int = 300,
    ) -> None:
        self._enabled = enabled
        self._interval = interval_seconds
        self._cooldown = cooldown_seconds
        self._last_sent_at: float = 0.0
        self._task: asyncio.Task[None] | None = None

    def start(self, meshcore: object) -> None:
        """Start the background timer task.  No-op when disabled."""
        if not self._enabled:
            logger.debug("adverts disabled — background timer not started")
            return
        if self._task is None:
            self._task = asyncio.create_task(
                self._loop(meshcore),
                name="zorkbot-advertiser",
            )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def send_if_due(self, meshcore: object) -> None:
        """Send an advert if the cooldown has elapsed.  No-op when disabled."""
        if not self._enabled:
            return
        now = time.monotonic()
        if now - self._last_sent_at < self._cooldown:
            logger.debug(
                "advert cooldown active (%.0fs remaining) — skipping",
                self._cooldown - (now - self._last_sent_at),
            )
            return
        await self._send(meshcore)

    async def _send(self, meshcore: object) -> None:
        try:
            result = await meshcore.commands.send_advert(flood=True)
            self._last_sent_at = time.monotonic()
            logger.info("advert sent (flood=True): %s", result.type)
        except Exception:
            logger.exception("failed to send advert")

    async def _loop(self, meshcore: object) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self.send_if_due(meshcore)
