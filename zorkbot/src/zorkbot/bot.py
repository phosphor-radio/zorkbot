"""Zorkbot core dispatch logic."""

from __future__ import annotations

import asyncio
import logging

from zorkbot.addressing import parse_command, strip_address
from zorkbot.advertiser import Advertiser
from zorkbot.channels import is_zork_channel
from zorkbot.commands.bots import handle_bots
from zorkbot.commands.end import handle_end
from zorkbot.commands.list_sessions import handle_list
from zorkbot.commands.reset import handle_reset
from zorkbot.commands.rules import handle_rules
from zorkbot.commands.start import AUTHOR_TEXT, HELP_TEXT, handle_start
from zorkbot.commands.watch import handle_watch
from zorkbot.commands.watchers import handle_watchers
from zorkbot.commands.zork import (
    _HELP_PACKETS,
    _HELP_PACKETS_IN_SESSION,
    handle_game_command,
)
from zorkbot.config import BotConfig
from zorkbot.context import Context, IncomingMessage, ReplyFunc
from zorkbot.game_client import GameClient
from zorkbot.rate_limit import RateLimiter
from zorkbot.session_state import SessionState

logger = logging.getLogger(__name__)

BUSY_REPLY = "The bot is busy, try again."
RATE_LIMIT_REPLY = "Slow down — try again in a moment."

# Commands accepted from the #zork channel (lobby).
_LOBBY_COMMANDS = frozenset({
    "help", "commands", "start", "end", "list", "watch", "watchers", "author", "bots",
})


class ZorkBot:
    def __init__(
        self,
        config: BotConfig,
        game: GameClient,
        advertiser: Advertiser,
        meshcore: object,
    ) -> None:
        self.config = config
        self.game = game
        self.advertiser = advertiser
        self.meshcore = meshcore

        self._state = SessionState(
            max_watchers_per_session=config.max_watchers_per_session,
        )
        self._rate_limiter = RateLimiter(config.rate_limit_seconds)

        # Per-player asyncio queues: pubkey_prefix → asyncio.Queue
        self._queues: dict[str, asyncio.Queue] = {}
        # Per-player worker tasks
        self._workers: dict[str, asyncio.Task] = {}
        # Fire-and-forget tasks (e.g. delayed !bots reply) not tied to a
        # player's command queue, tracked so stop() can cancel them cleanly.
        self._background_tasks: set[asyncio.Task] = set()

        # Injected by the runner after construction.
        self._send_dm: ReplyFunc | None = None

    @property
    def name(self) -> str:
        return self.config.name

    def set_send_dm(self, func: ReplyFunc) -> None:
        self._send_dm = func

    async def dispatch_channel(self, message: IncomingMessage, reply: ReplyFunc) -> None:
        """Handle a message from the #zork channel."""
        if not is_zork_channel(message.channel_idx, self.config.channel):
            logger.debug("ignoring message on channel %s", message.channel_idx)
            return

        rest, _mentioned = strip_address(message.text.strip(), self.name)
        args = parse_command(rest)
        if args is None:
            logger.debug("ignoring non-command message: %r", message.text)
            return

        command, _, rest_args = args.partition(" ")
        command = command.lower()

        if not await self._rate_check(message, reply):
            return

        if command not in _LOBBY_COMMANDS:
            await reply("Send !start and then DM me to play.")
            return

        ctx = Context(
            message=message,
            args=args,
            _reply=reply,
            config=self.config,
        )
        await self._enqueue(ctx, command, rest_args, reply)

    async def dispatch_dm(self, message: IncomingMessage, reply: ReplyFunc) -> None:
        """Handle a direct message."""
        text = message.text.strip()
        if not text:
            return

        if not await self._rate_check(message, reply):
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
        await self._enqueue(ctx, command, rest_args, reply)

    async def _rate_check(self, message: IncomingMessage, reply: ReplyFunc) -> bool:
        exempt = bool(
            message.pubkey_prefix
            and message.pubkey_prefix.lower() in self.config.admin_pubkeys
        )
        if not self._rate_limiter.allow(message.pubkey_prefix, exempt=exempt):
            logger.info(
                "rate limited player=%s: %r",
                (message.pubkey_prefix or "?")[:8],
                message.text,
            )
            await reply(RATE_LIMIT_REPLY)
            return False
        return True

    async def _enqueue(
        self, ctx: Context, command: str, rest_args: str, reply: ReplyFunc
    ) -> None:
        player_id = ctx.pubkey_prefix or "anon"

        if player_id not in self._queues:
            self._queues[player_id] = asyncio.Queue(maxsize=self.config.command_queue_size)

        queue = self._queues[player_id]
        try:
            queue.put_nowait((ctx, command, rest_args))
        except asyncio.QueueFull:
            logger.info(
                "queue full for player=%s, rejecting: %r", player_id[:8], ctx.message.text
            )
            await reply(BUSY_REPLY)
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
            await ctx.reply_many(_HELP_PACKETS_IN_SESSION if in_session else _HELP_PACKETS)
            return

        if command == "author":
            await ctx.reply(AUTHOR_TEXT)
            return

        if command == "bots":
            self._spawn(handle_bots(ctx))
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
            await handle_end(ctx, self.game, self._state, rest_args)
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
        for task in self._workers.values():
            task.cancel()
        for task in self._background_tasks:
            task.cancel()
        await asyncio.gather(
            *self._workers.values(), *self._background_tasks, return_exceptions=True
        )
        self._workers.clear()
        self._background_tasks.clear()
