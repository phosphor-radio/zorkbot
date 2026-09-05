"""Zorkbot core dispatch logic."""

from __future__ import annotations

import asyncio
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
    _HELP_PACKETS,
    _HELP_PACKETS_DM,
    _HELP_PACKETS_IN_SESSION,
    handle_game_command,
)
from zorkbot.admin.events import EventSink, NullEventSink
from zorkbot.config import BotConfig
from zorkbot.context import Context, IncomingMessage, ReplyFunc
from zorkbot.game_client import GameClient
from zorkbot.cooldown import Cooldown
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

        # Per-player asyncio queues: pubkey_prefix → asyncio.Queue
        self._queues: dict[str, asyncio.Queue] = {}
        # Per-player worker tasks
        self._workers: dict[str, asyncio.Task] = {}
        # Fire-and-forget tasks (e.g. delayed !bots reply) not tied to a
        # player's command queue, tracked so stop() can cancel them cleanly.
        self._background_tasks: set[asyncio.Task] = set()

        self._started_at = time.monotonic()

        # Injected by the runner after construction.
        self._send_dm: ReplyFunc | None = None
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

    def set_send_queue_depth_getter(self, func: Callable[[], int | None]) -> None:
        self._send_queue_depth_getter = func

    @property
    def startup_flushed_messages(self) -> int | None:
        return self._startup_flushed_messages

    def set_startup_flushed_messages(self, count: int) -> None:
        self._startup_flushed_messages = count

    def start_session_poller(self) -> None:
        """Start polling the game service for sessions it ended server-side
        (inactivity timeout, PTY crash) so their watchers get notified — the
        bot has no other way to learn about those. No-op when disabled."""
        if self.config.session_poll_seconds <= 0:
            return
        self._spawn(self._session_poll_loop())

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
            if self._send_dm:
                await notify_watchers_session_ended(self._send_dm, record)

    async def dispatch_channel(self, message: IncomingMessage, reply: ReplyFunc) -> None:
        """Handle a message from the #zork channel."""
        if not channel_matches(message.channel_idx, self.config.channel):
            logger.debug("ignoring message on channel %s", message.channel_idx)
            return

        rest, _mentioned = strip_address(message.text.strip(), self.name)
        args = parse_command(rest)
        if args is None:
            logger.debug("ignoring non-command message: %r", message.text)
            return

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
            return

        ctx = Context(
            message=message,
            args=args,
            _reply=reply,
            config=self.config,
        )
        self._enqueue(ctx, command, rest_args)

    async def dispatch_bots_channel(self, message: IncomingMessage, reply: ReplyFunc) -> None:
        """Handle a message from the dedicated bots-discovery channel.

        Only !bots is recognized here — everything else is ignored, since
        this channel is for mesh bot roll-calls, not the game lobby. Inert
        unless bots_enabled and a [bots_channel] are both configured.
        """
        if not self.config.bots_enabled or self.config.bots_channel is None:
            return
        if not channel_matches(message.channel_idx, self.config.bots_channel):
            return

        rest, _mentioned = strip_address(message.text.strip(), self.name)
        args = parse_command(rest)
        if args is None:
            return

        command, _, _rest_args = args.partition(" ")
        command = command.lower()
        if command != "bots":
            return

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
            return

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

    def _enqueue(self, ctx: Context, command: str, rest_args: str) -> None:
        player_id = ctx.pubkey_prefix or "anon"

        # Drop whatever arrives while this player's previous response is still
        # going out. The gate is state, not elapsed time, so a player who waits
        # for their reply is never caught by it — only someone typing ahead of
        # one, which is what a spammer does and a person reading the game does
        # not. The bot's output rate is then governed by send_spacing_seconds.
        worker = self._workers.get(player_id)
        if worker is not None and not worker.done() and command not in _INTERRUPT_COMMANDS:
            self._drop(ctx, command, "response_pending")
            return

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
            return

        if player_id not in self._workers or self._workers[player_id].done():
            self._workers[player_id] = asyncio.create_task(
                self._run_worker(player_id),
                name=f"zorkbot-worker-{player_id[:8]}",
            )

    def _spawn(self, coro) -> None:
        """Run coro as a fire-and-forget task, not blocking the caller's
        command queue. Tracked so stop() can cancel it on shutdown."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._on_background_done)

    def _on_background_done(self, task: asyncio.Task) -> None:
        self._background_tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            logger.error("background task failed", exc_info=exc)

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

        async def send_dm(pubkey_prefix: str, text: str) -> None:
            if self._send_dm:
                await self._send_dm(pubkey_prefix, text)

        async def send_advert() -> None:
            await self.advertiser.send_if_due(self.meshcore)

        if command in ("help", "commands"):
            in_session = (
                ctx.is_dm
                and self._state.active_state(ctx.pubkey_prefix or "") == "playing"
            )
            if in_session:
                packets = _HELP_PACKETS_IN_SESSION
            elif ctx.is_dm:
                packets = _HELP_PACKETS_DM
            else:
                packets = _HELP_PACKETS
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
            await handle_end(ctx, self.game, self._state, rest_args, send_dm)
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
                ctx, self.game, self._state, rest_args, send_dm
            )
            return

        # Unknown command from DM context — show help.
        if ctx.is_dm:
            await ctx.reply(f"Unknown command: {command!r}. Send !help for a list.")

    async def drain(self) -> None:
        for q in self._queues.values():
            await q.join()

    async def stop(self) -> None:
        # Close out session-history rows so they don't read as still-active
        # forever — the game service keeps the actual saves/PTYs regardless.
        for record in list(self._state.all_sessions()):
            self._state.remove_session(record.player_id, reason="shutdown")
        for task in self._workers.values():
            task.cancel()
        for task in self._background_tasks:
            task.cancel()
        await asyncio.gather(
            *self._workers.values(), *self._background_tasks, return_exceptions=True
        )
        self._workers.clear()
        self._background_tasks.clear()
