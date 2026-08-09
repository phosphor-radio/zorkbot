"""Message and handler context types."""

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

    async def reply(self, text: str) -> None:
        await self._reply(text)

    async def reply_many(self, texts: Iterable[str]) -> None:
        for text in texts:
            await self._reply(text)
