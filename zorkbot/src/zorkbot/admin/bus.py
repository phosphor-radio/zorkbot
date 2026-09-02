"""In-memory live-session fan-out for the admin UI's SSE transcript stream.

Nothing here touches SQLite or SessionState — this is purely a pub/sub ring
buffer per session number, subscribed to by `/api/sessions/{num}/stream`
handlers. Watching a session here does not consume a
`max_watchers_per_session` mesh slot and never appears in `!watchers`.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class _SessionChannel:
    buffer: deque[dict] = field(default_factory=deque)
    subscribers: set[asyncio.Queue] = field(default_factory=set)


class SessionBus:
    def __init__(self, *, buffer_size: int = 50, max_streams: int = 4) -> None:
        self._buffer_size = buffer_size
        self._max_streams = max_streams
        self._channels: dict[int, _SessionChannel] = {}
        self._stream_count = 0

    def _channel(self, session_num: int) -> _SessionChannel:
        chan = self._channels.get(session_num)
        if chan is None:
            chan = _SessionChannel(buffer=deque(maxlen=self._buffer_size))
            self._channels[session_num] = chan
        return chan

    def publish(self, session_num: int, event: str, data: dict) -> None:
        chan = self._channel(session_num)
        item = {"event": event, "data": data, "at": time.time()}
        chan.buffer.append(item)
        for queue in list(chan.subscribers):
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                pass
        if event == "session_end":
            # Let subscribers drain the end event, then drop the channel.
            self._channels.pop(session_num, None)

    def can_subscribe(self) -> bool:
        return self._stream_count < self._max_streams

    def subscribe(self, session_num: int) -> tuple[asyncio.Queue, list[dict]]:
        chan = self._channel(session_num)
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        chan.subscribers.add(queue)
        self._stream_count += 1
        return queue, list(chan.buffer)

    def unsubscribe(self, session_num: int, queue: asyncio.Queue) -> None:
        chan = self._channels.get(session_num)
        if chan is not None:
            chan.subscribers.discard(queue)
        self._stream_count = max(0, self._stream_count - 1)
