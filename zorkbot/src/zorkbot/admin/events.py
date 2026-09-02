"""Instrumentation seam between the bot and the admin UI.

`EventSink` is a `Protocol` with a no-op default (`NullEventSink`), injected
into `SessionState`, `ZorkBot`, and `MeshCoreRunner`. With `[admin_ui]
enabled = false`, `NullEventSink` is what gets injected, so the bot does no
extra work and touches no disk. All methods are synchronous and
fire-and-forget — implementations must never block or raise into the RF
path.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from zorkbot.admin.bus import SessionBus
    from zorkbot.admin.store import Store
    from zorkbot.session_state import SessionRecord

logger = logging.getLogger(__name__)


class EventSink(Protocol):
    def message_rx(
        self, *, transport: str, channel_idx: int | None, pubkey_prefix: str | None, chars: int
    ) -> None: ...

    def message_tx(
        self,
        *,
        transport: str,
        channel_idx: int | None,
        pubkey_prefix: str | None,
        chars: int,
        dropped: bool = False,
    ) -> None: ...

    def command(
        self,
        *,
        pubkey_prefix: str | None,
        command: str,
        transport: str,
        channel_idx: int | None,
        accepted: bool,
        reject_reason: str | None = None,
    ) -> None: ...

    def session_started(self, record: "SessionRecord") -> None: ...

    def session_ended(self, record: "SessionRecord", reason: str) -> None: ...

    def watchers_changed(self, record: "SessionRecord") -> None: ...

    def player_seen(self, *, pubkey_prefix: str, name: str | None) -> None: ...

    def transcript(
        self, *, session_num: int, player_name: str, command: str | None, output: str
    ) -> None: ...


class NullEventSink:
    """No-op EventSink used when the admin UI is disabled."""

    def message_rx(self, **kwargs) -> None:
        pass

    def message_tx(self, **kwargs) -> None:
        pass

    def command(self, **kwargs) -> None:
        pass

    def session_started(self, record: "SessionRecord") -> None:
        pass

    def session_ended(self, record: "SessionRecord", reason: str) -> None:
        pass

    def watchers_changed(self, record: "SessionRecord") -> None:
        pass

    def player_seen(self, **kwargs) -> None:
        pass

    def transcript(self, **kwargs) -> None:
        pass


class SqliteEventSink:
    """EventSink backed by a batched SQLite writer plus a live SessionBus.

    Every method is synchronous and non-blocking: it only enqueues. A single
    background task drains the queue in batches (every `batch_interval_s` or
    `batch_size` events, whichever comes first) and writes them to `store` in
    one transaction. On queue overflow, events are dropped and a
    rate-limited warning is logged — this must never apply backpressure to
    the caller (the RF send/receive path).
    """

    def __init__(
        self,
        store: "Store",
        bus: "SessionBus",
        *,
        bot_run_id: str,
        queue_size: int = 1024,
        batch_interval_s: float = 0.5,
        batch_size: int = 100,
    ) -> None:
        self.bot_run_id = bot_run_id
        self._store = store
        self._bus = bus
        self._batch_interval_s = batch_interval_s
        self._batch_size = batch_size
        self._queue: asyncio.Queue[tuple[str, tuple]] = asyncio.Queue(maxsize=queue_size)
        self._writer_task: asyncio.Task | None = None
        self._drop_count = 0
        self._last_warn_at = 0.0

    def start(self) -> None:
        self._writer_task = asyncio.create_task(self._writer_loop(), name="admin-ui-writer")

    async def stop(self) -> None:
        if self._writer_task is not None:
            self._writer_task.cancel()
            try:
                await self._writer_task
            except asyncio.CancelledError:
                pass
            self._writer_task = None
        await self._drain_remaining()

    async def _drain_remaining(self) -> None:
        batch: list[tuple[str, tuple]] = []
        while not self._queue.empty():
            batch.append(self._queue.get_nowait())
        if batch:
            try:
                await self._store.run_many(batch)
            except Exception:
                logger.exception("admin-ui: failed to flush final event batch")

    async def _writer_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            batch = [await self._queue.get()]
            deadline = loop.time() + self._batch_interval_s
            while len(batch) < self._batch_size:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(self._queue.get(), timeout=remaining))
                except asyncio.TimeoutError:
                    break
            try:
                await self._store.run_many(batch)
            except Exception:
                logger.exception("admin-ui: failed to write event batch (%d events)", len(batch))

    def _enqueue(self, sql: str, params: tuple) -> None:
        try:
            self._queue.put_nowait((sql, params))
        except asyncio.QueueFull:
            self._drop_count += 1
            now = time.monotonic()
            if now - self._last_warn_at > 30:
                logger.warning(
                    "admin-ui event queue full — dropped %d event(s) in the last 30s",
                    self._drop_count,
                )
                self._last_warn_at = now
                self._drop_count = 0

    # ------------------------------------------------------------------
    # EventSink implementation
    # ------------------------------------------------------------------

    def message_rx(self, *, transport, channel_idx, pubkey_prefix, chars) -> None:
        self._enqueue(
            "INSERT INTO messages(at, direction, transport, channel_idx, pubkey_prefix, chars, dropped) "
            "VALUES (?, 'rx', ?, ?, ?, ?, 0)",
            (int(time.time()), transport, channel_idx, pubkey_prefix, chars),
        )

    def message_tx(self, *, transport, channel_idx, pubkey_prefix, chars, dropped=False) -> None:
        self._enqueue(
            "INSERT INTO messages(at, direction, transport, channel_idx, pubkey_prefix, chars, dropped) "
            "VALUES (?, 'tx', ?, ?, ?, ?, ?)",
            (int(time.time()), transport, channel_idx, pubkey_prefix, chars, int(dropped)),
        )

    def command(
        self, *, pubkey_prefix, command, transport, channel_idx, accepted, reject_reason=None
    ) -> None:
        self._enqueue(
            "INSERT INTO commands(at, pubkey_prefix, command, transport, channel_idx, accepted, reject_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                int(time.time()),
                pubkey_prefix,
                command,
                transport,
                channel_idx,
                int(accepted),
                reject_reason,
            ),
        )

    def session_started(self, record: "SessionRecord") -> None:
        started_at = int(getattr(record, "started_wall_at", time.time()))
        # sessions.pubkey_prefix has a FK to players, but session_started can
        # fire before any message from this player has gone through
        # player_seen (e.g. the CLI simulator drives the bot directly and
        # never calls runner.py's message handlers at all). Upsert the
        # player row here too rather than relying on call-site ordering
        # across modules — both enqueue calls land in the same batch/
        # transaction since nothing awaits between them.
        self._enqueue(
            "INSERT INTO players(pubkey_prefix, name, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(pubkey_prefix) DO UPDATE SET "
            "  name = COALESCE(excluded.name, players.name), "
            "  last_seen_at = excluded.last_seen_at",
            (record.player_id, record.player_name, started_at, started_at),
        )
        self._enqueue(
            "INSERT INTO sessions(bot_run_id, session_num, pubkey_prefix, started_at, peak_watchers) "
            "VALUES (?, ?, ?, ?, 0)",
            (self.bot_run_id, record.num, record.player_id, started_at),
        )

    def session_ended(self, record: "SessionRecord", reason: str) -> None:
        self._enqueue(
            "UPDATE sessions SET ended_at = ?, end_reason = ? "
            "WHERE bot_run_id = ? AND session_num = ?",
            (int(time.time()), reason, self.bot_run_id, record.num),
        )
        self._bus.publish(
            record.num,
            "session_end",
            {"session": record.num, "at": int(time.time()), "reason": reason},
        )

    def watchers_changed(self, record: "SessionRecord") -> None:
        self._enqueue(
            "UPDATE sessions SET peak_watchers = MAX(peak_watchers, ?) "
            "WHERE bot_run_id = ? AND session_num = ?",
            (len(record.watchers), self.bot_run_id, record.num),
        )
        self._bus.publish(
            record.num,
            "watchers",
            {
                "session": record.num,
                "watchers": [{"pubkey_prefix": w} for w in sorted(record.watchers)],
            },
        )

    def player_seen(self, *, pubkey_prefix, name) -> None:
        now = int(time.time())
        self._enqueue(
            "INSERT INTO players(pubkey_prefix, name, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(pubkey_prefix) DO UPDATE SET "
            "  name = COALESCE(excluded.name, players.name), "
            "  last_seen_at = excluded.last_seen_at",
            (pubkey_prefix, name, now, now),
        )

    def transcript(self, *, session_num, player_name, command, output) -> None:
        now = time.time()
        if command is not None:
            self._bus.publish(
                session_num,
                "command",
                {"session": session_num, "at": now, "player": player_name, "text": command},
            )
        self._bus.publish(
            session_num, "output", {"session": session_num, "at": now, "text": output}
        )
