"""Bounded in-memory windows of recent mesh traffic, for the admin radio view.

Nothing here is persisted. The admin event log records that a message happened
and how long it was; this keeps the text of the last few, in RAM, so an
operator can see what was actually said. The split is deliberate — see
docs/specs/admin-radio-view.md, "Message history".

Capacity is partitioned rather than shared. The channels the bot serves get
reserved windows that are never evicted; contacts share a separate LRU pool.
A busy channel therefore cannot flush the contacts playing on it, and a crowd
of chatty contacts cannot flush the game channel.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class WindowMessage:
    at: int
    direction: str  # "rx" | "tx"
    text: str
    sender_name: str | None
    pubkey_prefix: str | None
    # False only for a channel message the bot received: those carry no sender
    # key material on the wire, so the name is whatever the sender typed.
    # See MessageWindows.record_rx.
    sender_verified: bool

    def as_dict(self) -> dict:
        return {
            "at": self.at,
            "direction": self.direction,
            "text": self.text,
            "sender_name": self.sender_name,
            "pubkey_prefix": self.pubkey_prefix,
            "sender_verified": self.sender_verified,
        }


class MessageWindows:
    """Per-peer rings of recent messages.

    `window=0` disables capture entirely: every record call becomes a no-op
    and every window reads as empty. That is the switch for an operator who
    does not want message text held in memory at all.
    """

    def __init__(
        self,
        *,
        window: int = 20,
        max_contacts: int = 50,
        channels: Iterable[int] = (),
        bot_name: str = "zorkbot",
    ) -> None:
        self.window = max(0, window)
        self.max_contacts = max(0, max_contacts)
        self.bot_name = bot_name
        # Reserved at construction and never added to or evicted: tracking
        # follows the bot's configured channels, so a channel appearing here
        # later would mean the config changed under a running process.
        self._channels: dict[int, deque[WindowMessage]] = {
            idx: deque(maxlen=self.window) for idx in channels
        }
        # LRU by last activity; the oldest-touched contact is dropped first.
        self._contacts: OrderedDict[str, deque[WindowMessage]] = OrderedDict()

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_rx(
        self,
        *,
        transport: str,
        channel_idx: int | None,
        pubkey_prefix: str | None,
        sender_name: str | None,
        text: str,
    ) -> None:
        """Record a message the bot received.

        Channel senders are unverified by construction: CHANNEL_MSG_RECV has
        no sender field, so `sender_name` came from the "Name: text" convention
        in the message body and anyone on the channel can type anyone's name.
        DM senders come from the packet's own pubkey prefix and are verified.
        """
        is_dm = transport == "dm"
        self._append(
            transport=transport,
            channel_idx=channel_idx,
            pubkey_prefix=pubkey_prefix,
            message=WindowMessage(
                at=int(time.time()),
                direction="rx",
                text=text,
                sender_name=sender_name,
                pubkey_prefix=pubkey_prefix,
                sender_verified=is_dm,
            ),
        )

    def record_tx(
        self,
        *,
        transport: str,
        channel_idx: int | None,
        pubkey_prefix: str | None,
        text: str,
    ) -> None:
        """Record a message the bot sent. Always attributed to the bot."""
        self._append(
            transport=transport,
            channel_idx=channel_idx,
            pubkey_prefix=pubkey_prefix,
            message=WindowMessage(
                at=int(time.time()),
                direction="tx",
                text=text,
                sender_name=self.bot_name,
                pubkey_prefix=None,
                sender_verified=True,
            ),
        )

    def _append(
        self,
        *,
        transport: str,
        channel_idx: int | None,
        pubkey_prefix: str | None,
        message: WindowMessage,
    ) -> None:
        if self.window == 0:
            return
        if transport == "dm":
            if not pubkey_prefix:
                return
            self._contact_window(pubkey_prefix).append(message)
        else:
            # Untracked channel: the subscription that would feed it does not
            # exist, so this is only reachable if a caller invents one.
            ring = self._channels.get(channel_idx)
            if ring is not None:
                ring.append(message)

    def _contact_window(self, pubkey_prefix: str) -> deque[WindowMessage]:
        ring = self._contacts.get(pubkey_prefix)
        if ring is None:
            ring = deque(maxlen=self.window)
            self._contacts[pubkey_prefix] = ring
            while len(self._contacts) > self.max_contacts:
                self._contacts.popitem(last=False)
        self._contacts.move_to_end(pubkey_prefix)
        return ring

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def tracks_channel(self, channel_idx: int) -> bool:
        """Whether this channel has a window at all.

        Distinct from "its window is empty": an untracked channel reports n/a
        in the UI, a tracked-but-quiet one reports nothing seen yet.
        """
        return channel_idx in self._channels

    def contact_messages(self, pubkey_prefix: str) -> list[WindowMessage]:
        ring = self._contacts.get(pubkey_prefix)
        return list(ring) if ring else []

    def channel_messages(self, channel_idx: int) -> list[WindowMessage]:
        ring = self._channels.get(channel_idx)
        return list(ring) if ring else []

    def contact_count(self, pubkey_prefix: str) -> int:
        ring = self._contacts.get(pubkey_prefix)
        return len(ring) if ring else 0

    def channel_count(self, channel_idx: int) -> int | None:
        """Messages held for a channel, or None when it is not tracked."""
        ring = self._channels.get(channel_idx)
        return len(ring) if ring is not None else None

    def channel_last_at(self, channel_idx: int) -> int | None:
        """When this channel last carried traffic, or None.

        None for both "not tracked" and "nothing seen"; callers separate the
        two with tracks_channel().
        """
        ring = self._channels.get(channel_idx)
        return ring[-1].at if ring else None
