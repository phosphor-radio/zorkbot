"""Zorkbot core dispatch logic."""

from __future__ import annotations

import asyncio
import logging

from zorkbot.addressing import parse_command, strip_address
from zorkbot.channels import is_zork_channel
from zorkbot.command_queue import CommandQueue, QueuedCommand
from zorkbot.commands.zork import handle_zork
from zorkbot.config import BotConfig
from zorkbot.context import Context, IncomingMessage, ReplyFunc
from zorkbot.game_client import GameClient
from zorkbot.rate_limit import RateLimiter

logger = logging.getLogger(__name__)

BUSY_REPLY = "The game is busy, try again."
RATE_LIMIT_REPLY = "Slow down — try again in a moment."


class ZorkBot:
    def __init__(self, config: BotConfig, game: GameClient) -> None:
        self.config = config
        self.game = game
        self._game_lock = asyncio.Lock()
        self._rate_limiter = RateLimiter(config.rate_limit_seconds)
        self._queue = CommandQueue(config.command_queue_size)
        self._worker_task: asyncio.Task[None] | None = None

    @property
    def name(self) -> str:
        return self.config.name

    def start(self) -> None:
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(
                self._run_worker(),
                name="zorkbot-command-worker",
            )

    async def stop(self) -> None:
        if self._worker_task is None:
            return
        self._worker_task.cancel()
        try:
            await self._worker_task
        except asyncio.CancelledError:
            pass
        self._worker_task = None

    async def drain(self) -> None:
        await self._queue.join()

    async def dispatch(self, message: IncomingMessage, reply: ReplyFunc) -> None:
        if not is_zork_channel(message.channel_idx, self.config.channel):
            logger.debug("ignoring message on channel %s", message.channel_idx)
            return

        rest, _mentioned = strip_address(message.text.strip(), self.name)
        args = parse_command(rest)
        if args is None:
            logger.debug("ignoring non-command message: %r", message.text)
            return

        exempt = self._is_rate_limit_exempt(message.sender_name)
        if not self._rate_limiter.allow(message.sender_name, exempt=exempt):
            logger.info(
                "rate limited sender %r: %r",
                message.sender_name,
                message.text,
            )
            await reply(RATE_LIMIT_REPLY)
            return

        ctx = Context(
            message=message,
            args=args,
            _reply=reply,
            config=self.config,
        )
        self.start()
        if not self._queue.try_enqueue(QueuedCommand(ctx)):
            logger.info(
                "command queue full, rejecting from %r: %r",
                message.sender_name,
                message.text,
            )
            await reply(BUSY_REPLY)

    async def _run_worker(self) -> None:
        while True:
            command = await self._queue.dequeue()
            try:
                await handle_zork(command.ctx, self.game, self._game_lock)
            except Exception:
                logger.exception(
                    "command failed for sender %r",
                    command.ctx.sender_name,
                )
            finally:
                self._queue.mark_done()

    def _is_rate_limit_exempt(self, sender_name: str | None) -> bool:
        return bool(sender_name and sender_name.lower() in self.config.admin_names)
