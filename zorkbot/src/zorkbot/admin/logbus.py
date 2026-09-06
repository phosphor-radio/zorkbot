"""In-memory tail of the process log, fanned out to the admin UI over SSE.

`LogBus` is a `logging.Handler` installed on the root logger: every record the
process emits lands in a bounded ring buffer, and is pushed to whichever admin
streams are open. The buffer is replayed on connect, so opening the Logs view
shows the recent tail rather than a blank pane — the same recent-context trick
`SessionBus` uses, for the same reason.

Two properties this module has to hold on to:

*   **Never raise, never log.** A logging handler that logs on its own error
    path recurses until the stack runs out, and one that raises breaks the
    caller that was only trying to log. Every failure here is swallowed.
*   **Thread-safe hand-off.** Records arrive from whichever thread logged them:
    the event loop for most of the bot, but an `asyncio.to_thread` worker for
    every SQLite call in `store.py`. `asyncio.Queue` is not thread-safe, so
    delivery is marshalled back onto the loop with `call_soon_threadsafe`.

Nothing here is persisted. The log is the operator's live window on the
process; SQLite holds the event history, and the two are deliberately separate.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import threading
from collections import deque
from dataclasses import dataclass

# A single runaway record (a multi-megabyte traceback, a dumped payload) should
# not be able to evict the whole ring buffer or stall an SSE client.
_MAX_MESSAGE_CHARS = 8000

# "%(message)s" plus the default exception/stack handling: `Formatter.format`
# appends `exc_info` and `stack_info` for us, so tracebacks survive intact.
_FORMATTER = logging.Formatter("%(message)s")

LEVEL_NAMES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


@dataclass
class _Subscriber:
    queue: asyncio.Queue
    min_level: int
    # Records dropped because this subscriber's queue was full. Reported on the
    # next record that does get through, so a slow client sees an explicit gap
    # marker instead of a silently incomplete tail.
    dropped: int = 0


class LogBus(logging.Handler):
    def __init__(self, *, buffer_size: int = 500, max_streams: int = 2) -> None:
        super().__init__(level=logging.NOTSET)
        self._buffer: deque[dict] = deque(maxlen=buffer_size)
        # Monotonic per-process record id. A reconnecting client replays the
        # whole ring buffer again, so it needs a watermark to tell the lines it
        # already has from the ones it missed.
        self._seq = itertools.count(1)
        self._subscribers: list[_Subscriber] = []
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._max_streams = max_streams
        self._attached_to: logging.Logger | None = None

    # -- handler lifecycle ------------------------------------------------

    def attach(self, logger: logging.Logger | None = None) -> None:
        """Install on the root logger (or `logger`) and start buffering.

        Called before the event loop exists so that startup — config load,
        radio connect, the failures most worth reading later — is already in
        the buffer by the time an operator opens the view. The loop is picked
        up lazily in `subscribe`, which by construction runs on it.
        """
        target = logger or logging.getLogger()
        self._attached_to = target
        target.addHandler(self)

    def detach(self) -> None:
        if self._attached_to is not None:
            self._attached_to.removeHandler(self)
            self._attached_to = None
        with self._lock:
            self._loop = None
            self._subscribers.clear()

    # -- logging.Handler --------------------------------------------------

    def emit(self, record: logging.LogRecord) -> None:
        try:
            item = _render(record)
        except Exception:  # pragma: no cover - defensive; must never escape
            return
        with self._lock:
            item["seq"] = next(self._seq)
            self._buffer.append(item)
            loop = self._loop
            subscribers = list(self._subscribers)
        if loop is None or not subscribers:
            return
        try:
            # Always via the loop, even when already on it: `call_soon` is FIFO,
            # so records keep the order they were logged in regardless of which
            # thread produced them. Delivering inline for loop-thread records
            # would let them overtake worker-thread ones already queued.
            loop.call_soon_threadsafe(_deliver, subscribers, item)
        except RuntimeError:
            # Loop closed between the snapshot and the hand-off (shutdown).
            pass

    def handleError(self, record: logging.LogRecord) -> None:
        # The base implementation writes to stderr. Nothing this handler can
        # fail at is worth interleaving noise into the operator's console.
        pass

    # -- subscription -----------------------------------------------------

    def can_subscribe(self) -> bool:
        with self._lock:
            return len(self._subscribers) < self._max_streams

    def subscribe(self, *, min_level: int = logging.NOTSET) -> tuple[asyncio.Queue, list[dict]]:
        """Returns the subscriber's queue and the buffered tail to replay."""
        sub = _Subscriber(queue=asyncio.Queue(maxsize=256), min_level=min_level)
        with self._lock:
            # Captured here rather than at attach time: subscribe only ever
            # runs inside a request handler, so it is on the serving loop.
            self._loop = asyncio.get_running_loop()
            self._subscribers.append(sub)
            backlog = [item for item in self._buffer if item["levelno"] >= min_level]
        return sub.queue, backlog

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers = [s for s in self._subscribers if s.queue is not queue]

    # -- introspection ----------------------------------------------------

    @property
    def buffer_size(self) -> int:
        return self._buffer.maxlen or 0

    def effective_level_name(self) -> str:
        """The level the process actually logs at.

        The view can only filter what reaches the handler: a bot running at
        `log_level = "WARNING"` has no DEBUG records to show, however the UI's
        dropdown is set. Surfacing this stops that reading as a broken stream.
        """
        target = self._attached_to or logging.getLogger()
        return logging.getLevelName(target.getEffectiveLevel())


def _deliver(subscribers: list[_Subscriber], item: dict) -> None:
    for sub in subscribers:
        if item["levelno"] < sub.min_level:
            continue
        payload = item if not sub.dropped else {**item, "dropped_before": sub.dropped}
        try:
            sub.queue.put_nowait(payload)
        except asyncio.QueueFull:
            sub.dropped += 1
        else:
            sub.dropped = 0


def _render(record: logging.LogRecord) -> dict:
    try:
        message = _FORMATTER.format(record)
    except Exception:
        # Bad %-args in the caller's log line: report the record rather than
        # dropping it, since a malformed log call is itself worth seeing.
        message = f"<unformattable log record from {record.name}>"
    if len(message) > _MAX_MESSAGE_CHARS:
        message = message[:_MAX_MESSAGE_CHARS] + "… [truncated]"
    return {
        "seq": 0,  # assigned under the lock in `emit`, so ids stay ordered
        "at": record.created,
        "level": record.levelname,
        "levelno": record.levelno,
        "logger": record.name,
        "message": message,
    }


def parse_level(name: str) -> int | None:
    """Resolve a level name from an untrusted query parameter.

    Allowlisted rather than passed to `logging.getLevelName`, which happily
    accepts arbitrary strings and returns them back as a level.
    """
    upper = name.strip().upper()
    if upper not in LEVEL_NAMES:
        return None
    return getattr(logging, upper)
