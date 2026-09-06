"""Message and handler context types.

IncomingMessage, Context, and ReplyFunc follow patterns from ottobot
(https://github.com/tahnok/ottobot, MIT License, Copyright (c) Wesley Ellis).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from zorkbot.config import BotConfig

# Returns whether the packet reached its recipient. False only ever means a
# measured, failed delivery: a sender that does not measure (channel traffic,
# the CLI simulator, a test double) returns True or None, and None is treated
# as "unknown", never as failure — see Context.reply_many.
ReplyFunc = Callable[[str], Awaitable[bool | None]]


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

    async def reply(self, text: str) -> bool:
        return await self._reply(text) is not False

    async def reply_many(self, texts: Iterable[str]) -> bool:
        """Send each packet in turn, stopping if one is not delivered.

        A packet that failed every retry says the link is not carrying
        traffic right now, and each remaining packet would cost several more
        transmissions to prove it again. Returns False if the response was cut
        short.

        Only an explicit False counts as a failure. A sender that returns None
        has not measured delivery rather than observed it fail, and treating
        that as a failure would truncate every multi-packet reply the moment
        it went through an unmeasured path.
        """
        for text in texts:
            if await self._reply(text) is False and self.config.dm_ack_abandon_response:
                return False
        return True
