"""Zorkbot core dispatch logic."""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from typing import Callable

from zorkbot.addressing import parse_command, strip_address
from zorkbot.advertiser import Advertiser
from zorkbot.channels import channel_matches
from zorkbot.commands.bots import handle_bots
from zorkbot.commands.end import handle_end
from zorkbot.commands.list_sessions import handle_list
from zorkbot.commands.reset import handle_reset
from zorkbot.commands.rules import handle_rules
from zorkbot.commands.start import AUTHOR_TEXT, HELP_TEXT, handle_start
from zorkbot.commands.uptime import handle_uptime
from zorkbot.commands.watch import handle_watch
from zorkbot.commands.watchers import handle_watchers
from zorkbot.commands.zork import (
    channel_help_packets,
    dm_help_packets,
    handle_game_command,
)
from zorkbot.admin.events import EventSink, NullEventSink
from zorkbot.config import BotConfig
from zorkbot.context import Context, IncomingMessage, ReplyFunc
from zorkbot.game_client import GameClient
from zorkbot.cooldown import Cooldown
from zorkbot.message_window import MessageWindows
from zorkbot.radio_state import RadioState
from zorkbot.session_state import SessionState
from zorkbot.watcher_notify import notify_watchers_session_ended

logger = logging.getLogger(__name__)

# Commands allowed through while a player's previous response is still being
# transmitted. !end is how a player stops a runaway session, so dropping it
# would leave them with no way out until the session times out on its own.
_INTERRUPT_COMMANDS = frozenset({"end"})

# Commands accepted from the #zork channel (lobby). !bots is deliberately
# excluded — it's only reachable from the separate bots-discovery channel.
_LOBBY_COMMANDS = frozenset({
    "help", "commands", "start", "end", "list", "watch", "watchers",
    "author", "uptime",
})


class ZorkBot:
    def __init__(
        self,
        config: BotConfig,
        game: GameClient,
        advertiser: Advertiser,
        meshcore: object,
        event_sink: EventSink | None = None,
    ) -> None:
        self.config = config
        self.game = game
        self.advertiser = advertiser
        self.meshcore = meshcore
        self._sink = event_sink or NullEventSink()

        self._state = SessionState(
            max_watchers_per_session=config.max_watchers_per_session,
            event_sink=self._sink,
        )
        self._bots_cooldown = Cooldown(config.bots_cooldown_seconds)

        # Radio introspection for the admin UI. Both are inert unless the
        # admin UI reads them: the windows only fill from the runner's taps,
        # and the cache only queries the device when asked.
        served = {config.channel.index: "zork"}
        if config.bots_enabled and config.bots_channel is not None:
            served[config.bots_channel.index] = "bots"
        self.message_windows = MessageWindows(
            window=config.admin_ui.radio_message_window,
            max_contacts=config.admin_ui.radio_message_contacts,
            channels=served,
            bot_name=config.name,
        )
        self.radio_state = RadioState(
            meshcore,
            cache_seconds=config.admin_ui.radio_cache_seconds,
            served_channels=served,
            write_enabled=config.admin_ui.radio_write_enabled,
            write_min_interval=config.admin_ui.radio_write_min_interval_seconds,
        )

        # Per-player asyncio queues: pubkey_prefix → asyncio.Queue
        self._queues: dict[str, asyncio.Queue] = {}
        # Per-player worker tasks
        self._workers: dict[str, asyncio.Task] = {}
        # Fire-and-forget tasks (e.g. delayed !bots reply) not tied to a
        # player's command queue, tracked so stop() can cancel them cleanly.
        # Some of these never return (the session poller), so nothing may
        # wait on this set as a whole — see drain().
        self._background_tasks: set[asyncio.Task] = set()
        # The subset of the above that does finish: work that still owes
        # someone a reply. drain() waits on these, so a caller that drains
        # before reading the replies (the simulator, the tests) sees a
        # delayed answer instead of silence.
        self._response_tasks: set[asyncio.Task] = set()
        # Per-session watcher fan-out: session_num -> queue of coroutines,
        # each with a single consumer task, so a session's watchers see its
        # output in order without the sender's worker waiting on delivery.
        self._fanout_queues: dict[int, asyncio.Queue] = {}
        self._fanout_workers: dict[int, asyncio.Task] = {}

        self._started_at = time.monotonic()

        # Injected by the runner after construction.
        self._send_dm: ReplyFunc | None = None
        # Watcher fan-out goes out fire-and-forget while player DMs wait for
        # an ACK, so the runner injects a second sender. Left None by an
        # embedder that has no radio to distinguish (the CLI simulator), in
        # which case _watcher_sender falls back to the player one.
        self._send_watcher_dm: ReplyFunc | None = None
        self._send_queue_depth_getter: Callable[[], int | None] = lambda: None
        # How many queued-while-offline messages the runner discarded at
        # startup. Stays None when nothing drained a radio (simulate mode),
        # which is a different thing from having drained zero.
        self._startup_flushed_messages: int | None = None

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def session_state(self) -> SessionState:
        return self._state

    @property
    def event_sink(self) -> EventSink:
        return self._sink

    @property
    def send_queue_depth(self) -> int | None:
        return self._send_queue_depth_getter()

    def set_send_dm(self, func: ReplyFunc) -> None:
        self._send_dm = func

    def set_send_watcher_dm(self, func: ReplyFunc) -> None:
        self._send_watcher_dm = func

    @property
    def _watcher_sender(self) -> ReplyFunc | None:
        return self._send_watcher_dm or self._send_dm

    def set_send_queue_depth_getter(self, func: Callable[[], int | None]) -> None:
        self._send_queue_depth_getter = func

    @property
    def startup_flushed_messages(self) -> int | None:
        return self._startup_flushed_messages

    def set_startup_flushed_messages(self, count: int) -> None:
        self._startup_flushed_messages = count

    def has_pending_replies(self) -> bool:
        """Whether a spawned handler still owes a reply — what drain() waits
        on beyond the command and fan-out queues."""
        return bool(self._response_tasks)

    def start_session_poller(self) -> None:
        """Start polling the game service for sessions it ended server-side
        (inactivity timeout, PTY crash) so their watchers get notified — the
        bot has no other way to learn about those. No-op when disabled."""
        if self.config.session_poll_seconds <= 0:
            return
        self._spawn(self._session_poll_loop(), returns=False)

    async def _session_poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.session_poll_seconds)
            await self._reconcile_sessions()

    async def _reconcile_sessions(self) -> None:
        try:
            server_sessions = await self.game.list_sessions()
        except Exception:
            logger.warning("session reconciliation: list_sessions failed", exc_info=True)
            return

        server_player_ids = {s.player_id for s in server_sessions}
        for record in self._state.all_sessions():
            if record.player_id in server_player_ids:
                continue
            self._state.remove_session(record.player_id, reason="server_side")
            logger.info(
                "session=%d player=%s ended server-side — notifying %d watcher(s)",
                record.num, record.player_id[:8], len(record.watchers),
            )
            watcher_sender = self._watcher_sender
            if watcher_sender:
                # Queued, not awaited: it must land behind any fan-out still
                # pending for this session, and polling must not stall on a
                # watcher's radio.
                self._fanout(
                    record.num,
                    notify_watchers_session_ended(watcher_sender, record),
                )

    async def dispatch_channel(self, message: IncomingMessage, reply: ReplyFunc) -> bool:
        """Handle a message from the #zork channel.

        Returns True when the bot answered — a channel is shared with
        conversation the bot has no part in, so the caller uses this to tell
        traffic it served from traffic it merely overheard.
        """
        if not channel_matches(message.channel_idx, self.config.channel):
            logger.debug("ignoring message on channel %s", message.channel_idx)
            return False

        rest, _mentioned = strip_address(message.text.strip(), self.name)
        args = parse_command(rest)
        if args is None:
            logger.debug("ignoring non-command message: %r", message.text)
            return False

        command, _, rest_args = args.partition(" ")
        command = command.lower()

        if command not in _LOBBY_COMMANDS:
            self._sink.command(
                pubkey_prefix=message.pubkey_prefix,
                command=command,
                transport="channel",
                channel_idx=message.channel_idx,
                accepted=False,
                reject_reason="not_in_lobby",
            )
            await reply("Send !start and then DM me to play.")
            return True

        ctx = Context(
            message=message,
            args=args,
            _reply=reply,
            config=self.config,
        )
        return self._enqueue(ctx, command, rest_args)

    async def dispatch_bots_channel(self, message: IncomingMessage, reply: ReplyFunc) -> bool:
        """Handle a message from the dedicated bots-discovery channel.

        Only !bots is recognized here — everything else is ignored, since
        this channel is for mesh bot roll-calls, not the game lobby. Inert
        unless bots_enabled and a [bots_channel] are both configured.

        Returns True when the roll call was answered, on the same terms as
        `dispatch_channel`.
        """
        if not self.config.bots_enabled or self.config.bots_channel is None:
            return False
        if not channel_matches(message.channel_idx, self.config.bots_channel):
            return False

        rest, _mentioned = strip_address(message.text.strip(), self.name)
        args = parse_command(rest)
        if args is None:
            return False

        command, _, _rest_args = args.partition(" ")
        command = command.lower()
        if command != "bots":
            return False

        # Roll calls are answered at most once per window, for the channel as a
        # whole. Extra requests are dropped in silence — see Cooldown.
        if not self._bots_cooldown.claim():
            logger.info(
                "dropped (bots_cooldown) player=%s", (message.pubkey_prefix or "?")[:8]
            )
            self._sink.command(
                pubkey_prefix=message.pubkey_prefix,
                command=command,
                transport="bots_channel",
                channel_idx=message.channel_idx,
                accepted=False,
                reject_reason="bots_cooldown",
            )
            return False

        self._sink.command(
            pubkey_prefix=message.pubkey_prefix,
            command=command,
            transport="bots_channel",
            channel_idx=message.channel_idx,
            accepted=True,
        )
        ctx = Context(
            message=message,
            args=args,
            _reply=reply,
            config=self.config,
        )
        self._spawn(handle_bots(ctx))
        return True

    async def dispatch_dm(self, message: IncomingMessage, reply: ReplyFunc) -> None:
        """Handle a direct message."""
        text = message.text.strip()
        if not text:
            return

        rest, _mentioned = strip_address(text, self.name)

        args = parse_command(rest)
        if args is not None:
            command, _, rest_args = args.partition(" ")
            command = command.lower()
        else:
            # Bare text — treat as a game command if a session is active.
            command = "_game"
            rest_args = rest.strip()

        ctx = Context(
            message=message,
            args=rest,
            _reply=reply,
            config=self.config,
        )
        self._enqueue(ctx, command, rest_args)

    def _drop(self, ctx: Context, command: str, reason: str) -> None:
        """Record a dropped command and deliberately send nothing back.

        Answering would defeat the purpose: every packet the bot emits takes a
        send_spacing_seconds transmit slot from a queue shared by all players,
        so a "slow down" notice costs the mesh as much as the reply it stands
        in for.
        """
        logger.info(
            "dropped (%s) player=%s: %r",
            reason,
            (ctx.pubkey_prefix or "?")[:8],
            ctx.message.text,
        )
        self._sink.command(
            pubkey_prefix=ctx.pubkey_prefix,
            command=command,
            transport="dm" if ctx.is_dm else "channel",
            channel_idx=None if ctx.is_dm else ctx.message.channel_idx,
            accepted=False,
            reject_reason=reason,
        )

    def _enqueue(self, ctx: Context, command: str, rest_args: str) -> bool:
        """Queue one command for this player's worker.

        Returns False when the command was dropped instead — the bot stays
        silent on those paths, so nothing was answered.
        """
        player_id = ctx.pubkey_prefix or "anon"

        # Drop whatever arrives while this player's previous response is still
        # going out. The gate is state, not elapsed time, so a player who waits
        # for their reply is never caught by it — only someone typing ahead of
        # one, which is what a spammer does and a person reading the game does
        # not. The bot's output rate is then governed by send_spacing_seconds.
        worker = self._workers.get(player_id)
        if worker is not None and not worker.done() and command not in _INTERRUPT_COMMANDS:
            self._drop(ctx, command, "response_pending")
            return False

        if player_id not in self._queues:
            # Depth 1: the worker holds the command being answered, leaving room
            # for a single queued interrupt behind it.
            self._queues[player_id] = asyncio.Queue(maxsize=1)

        queue = self._queues[player_id]
        try:
            queue.put_nowait((ctx, command, rest_args))
        except asyncio.QueueFull:
            # An interrupt is already waiting behind the in-flight response.
            self._drop(ctx, command, "queue_full")
            return False

        if player_id not in self._workers or self._workers[player_id].done():
            self._workers[player_id] = asyncio.create_task(
                self._run_worker(player_id),
                name=f"zorkbot-worker-{player_id[:8]}",
            )
        return True

    def _spawn(self, coro, *, returns: bool = True) -> None:
        """Run coro as a fire-and-forget task, not blocking the caller's
        command queue. Tracked so stop() can cancel it on shutdown.

        returns=False marks a loop that runs until cancelled, which drain()
        must not wait on.
        """
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        if returns:
            self._response_tasks.add(task)
        task.add_done_callback(self._on_background_done)

    def _on_background_done(self, task: asyncio.Task) -> None:
        self._background_tasks.discard(task)
        self._response_tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            logger.error("background task failed", exc_info=exc)

    def _fanout(self, session_num: int, coro) -> None:
        """Queue coro as watcher fan-out for one session, off the sender's
        command queue but strictly in order behind that session's earlier
        fan-out.

        Two properties are needed at once, and neither alone is enough:

        - Awaiting fan-out inline would keep the sending player's worker
          "busy", and _enqueue's pending-command gate drops their next
          message for as long as that worker runs — making every watcher a
          tax on how fast the player being watched can act.
        - Spawning each fan-out as an independent task would let two of them
          overlap, and since every packet is a separate acquisition of the
          runner's FIFO send lock, they would interleave: a watcher reading
          half of one room description, then part of the next, then the rest
          of the first. The end-of-session notice could likewise overtake
          the output it is meant to follow.

        So: one queue and one consumer per session. The caller returns
        immediately; the session's watchers see everything in the order it
        happened.
        """
        queue = self._fanout_queues.get(session_num)
        if queue is None:
            queue = self._fanout_queues[session_num] = asyncio.Queue()
        queue.put_nowait(coro)

        worker = self._fanout_workers.get(session_num)
        if worker is None or worker.done():
            self._fanout_workers[session_num] = asyncio.create_task(
                self._run_fanout(session_num),
                name=f"zorkbot-fanout-{session_num}",
            )
            self._fanout_workers[session_num].add_done_callback(
                functools.partial(self._on_fanout_done, session_num)
            )

    async def _run_fanout(self, session_num: int) -> None:
        queue = self._fanout_queues.get(session_num)
        if queue is None:
            return
        # No await between the emptiness check and the task finishing, so a
        # _fanout racing this exit either lands before the check (loop
        # continues) or after the task is done (a fresh worker is started).
        while not queue.empty():
            coro = await queue.get()
            try:
                await coro
            except Exception:
                logger.exception("watcher fan-out failed session=%d", session_num)
            finally:
                queue.task_done()

    def _on_fanout_done(self, session_num: int, task: asyncio.Task) -> None:
        # Sessions are never renumbered, so a finished session's queue is
        # dead weight — but only drop it if this is still the live worker
        # and nothing arrived behind it.
        if self._fanout_workers.get(session_num) is not task:
            return
        queue = self._fanout_queues.get(session_num)
        if queue is not None and not queue.empty():
            return
        self._fanout_workers.pop(session_num, None)
        self._fanout_queues.pop(session_num, None)

    async def _run_worker(self, player_id: str) -> None:
        queue = self._queues.get(player_id)
        if queue is None:
            return
        while not queue.empty():
            ctx, command, rest_args = await queue.get()
            try:
                await self._handle(ctx, command, rest_args)
            except Exception:
                logger.exception(
                    "command failed player=%s command=%r", player_id[:8], command
                )
            finally:
                queue.task_done()

    async def _handle(self, ctx: Context, command: str, rest_args: str) -> None:
        self._sink.command(
            pubkey_prefix=ctx.pubkey_prefix,
            command=command,
            transport="dm" if ctx.is_dm else "channel",
            channel_idx=None if ctx.is_dm else ctx.message.channel_idx,
            accepted=True,
        )

        async def send_dm(pubkey_prefix: str, text: str) -> bool:
            if not self._send_dm:
                return True
            return await self._send_dm(pubkey_prefix, text) is not False

        async def send_watcher_dm(pubkey_prefix: str, text: str) -> None:
            sender = self._watcher_sender
            if sender:
                await sender(pubkey_prefix, text)

        async def send_advert() -> None:
            await self.advertiser.send_if_due(self.meshcore)

        if command in ("help", "commands"):
            in_session = (
                ctx.is_dm
                and self._state.active_state(ctx.pubkey_prefix or "") == "playing"
            )
            if ctx.is_dm:
                packets = dm_help_packets(
                    ctx.config.channel.name,
                    in_session=in_session,
                    max_chars=ctx.config.packet_max_chars,
                )
            else:
                packets = channel_help_packets(ctx.config.packet_max_chars)
            await ctx.reply_many(packets)
            return

        if command == "author" and not ctx.is_dm:
            await ctx.reply(AUTHOR_TEXT)
            return

        if command == "uptime" and not ctx.is_dm:
            await handle_uptime(ctx, time.monotonic() - self._started_at)
            return

        if command == "rules":
            await handle_rules(ctx, self._state)
            return

        if command == "start":
            await handle_start(
                ctx, self.game, self._state,
                send_dm, send_advert, self.meshcore,
            )
            return

        if command == "end":
            await handle_end(
                ctx, self.game, self._state, rest_args, send_watcher_dm, self._fanout
            )
            return

        if command == "list":
            await handle_list(ctx, self._state)
            return

        if command == "watch":
            await handle_watch(ctx, self._state, rest_args)
            return

        if command == "watchers":
            await handle_watchers(ctx, self._state, self.meshcore)
            return

        if command == "reset":
            await handle_reset(ctx, self.game, self._state)
            return

        if command == "_game":
            await handle_game_command(
                ctx, self.game, self._state, rest_args, send_watcher_dm, self._fanout
            )
            return

        # Unknown command from DM context — show help.
        if ctx.is_dm:
            await ctx.reply(f"Unknown command: {command!r}. Send !help for a list.")

    async def drain(self) -> None:
        for q in self._queues.values():
            await q.join()
        # Watcher fan-out outlives the command that queued it, so draining
        # commands alone would leave those sends in flight. The per-session
        # queues can be joined; _background_tasks cannot (the session poller
        # never returns).
        for q in list(self._fanout_queues.values()):
            await q.join()
        # Replies that were spawned off the dispatch that produced them (the
        # !bots roll call waits 5-10s before answering, so that mesh bots
        # don't all transmit at once). Draining without these reports the bot
        # silent while an answer is still on its way. Looped because such a
        # task may itself spawn another.
        while self._response_tasks:
            await asyncio.gather(*self._response_tasks, return_exceptions=True)

    async def stop(self) -> None:
        # Close out session-history rows so they don't read as still-active
        # forever — the game service keeps the actual saves/PTYs regardless.
        for record in list(self._state.all_sessions()):
            self._state.remove_session(record.player_id, reason="shutdown")
        for task in self._workers.values():
            task.cancel()
        for task in self._background_tasks:
            task.cancel()
        for task in self._fanout_workers.values():
            task.cancel()
        await asyncio.gather(
            *self._workers.values(),
            *self._background_tasks,
            *self._fanout_workers.values(),
            return_exceptions=True,
        )
        self._workers.clear()
        self._background_tasks.clear()
        self._response_tasks.clear()
        self._fanout_workers.clear()
        # Close fan-out that never got its turn, so it doesn't resurface as
        # a "coroutine was never awaited" warning at interpreter exit.
        for queue in self._fanout_queues.values():
            while not queue.empty():
                queue.get_nowait().close()
                queue.task_done()
        self._fanout_queues.clear()
