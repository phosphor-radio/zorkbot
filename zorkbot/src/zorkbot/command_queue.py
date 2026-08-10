"""Serial command queue for game requests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from zorkbot.context import Context


@dataclass(frozen=True)
class QueuedCommand:
    ctx: Context


class CommandQueue:
    def __init__(self, max_size: int) -> None:
        self._queue: asyncio.Queue[QueuedCommand] = asyncio.Queue(maxsize=max_size)

    def try_enqueue(self, command: QueuedCommand) -> bool:
        try:
            self._queue.put_nowait(command)
        except asyncio.QueueFull:
            return False
        return True

    async def dequeue(self) -> QueuedCommand:
        return await self._queue.get()

    def mark_done(self) -> None:
        self._queue.task_done()

    async def join(self) -> None:
        await self._queue.join()
