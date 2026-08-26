"""Message and handler context types.

IncomingMessage, Context, and ReplyFunc follow patterns from ottobot
(https://github.com/tahnok/ottobot, MIT License, Copyright (c) Wesley Ellis).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from zorkbot.config import BotConfig

ReplyFunc = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class IncomingMessage:
    text: str
    sender_name: str | None = None
    # 12-char hex pubkey prefix (6 bytes) — stable cryptographic identity.
    pubkey_prefix: str | None = None
    # True when this message arrived via CONTACT_MSG_RECV (DM).
    is_dm: bool = False
    channel_idx: int = 0
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class Context:
    message: IncomingMessage
    args: str
    _reply: ReplyFunc
    config: BotConfig = field(default_factory=BotConfig)

    @property
    def sender_name(self) -> str | None:
        return self.message.sender_name

    @property
    def pubkey_prefix(self) -> str | None:
        return self.message.pubkey_prefix

    @property
    def is_dm(self) -> bool:
        return self.message.is_dm

    def is_admin(self) -> bool:
        return bool(
            self.pubkey_prefix
            and self.pubkey_prefix.lower() in self.config.admin_pubkeys
        )

    async def reply(self, text: str) -> None:
        await self._reply(text)

    async def reply_many(self, texts: Iterable[str]) -> None:
        for text in texts:
            await self._reply(text)
