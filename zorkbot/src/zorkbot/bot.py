"""Zorkbot core dispatch logic."""

from __future__ import annotations

import asyncio
import logging

from zorkbot.addressing import parse_zork_command, strip_address
from zorkbot.channels import is_zork_channel
from zorkbot.commands.zork import handle_zork
from zorkbot.config import BotConfig
from zorkbot.context import Context, IncomingMessage, ReplyFunc
from zorkbot.game_client import GameClient

logger = logging.getLogger(__name__)


class ZorkBot:
    def __init__(self, config: BotConfig, game: GameClient) -> None:
        self.config = config
        self.game = game
        self._game_lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return self.config.name

    async def dispatch(self, message: IncomingMessage, reply: ReplyFunc) -> None:
        if not is_zork_channel(message.channel_idx, self.config.channel):
            logger.debug("ignoring message on channel %s", message.channel_idx)
            return

        text, addressed = strip_address(message.text, self.name)
        if not addressed:
            logger.debug("ignoring message not addressed to %r", self.name)
            return

        args = parse_zork_command(text)
        if args is None:
            logger.debug("ignoring non-zork message: %r", text)
            return

        ctx = Context(
            message=message,
            args=args,
            _reply=reply,
            config=self.config,
        )
        await handle_zork(ctx, self.game, self._game_lock)
