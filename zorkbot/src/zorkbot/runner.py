"""MeshCore device connection and message loop.

Channel message parsing, send spacing, and serial send locking follow
patterns from ottobot's runner
(https://github.com/tahnok/ottobot, MIT License, Copyright (c) Wesley Ellis).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from meshcore import EventType, MeshCore
from meshcore.events import Event

from zorkbot.bot import ZorkBot
from zorkbot.config import BotConfig
from zorkbot.context import IncomingMessage

logger = logging.getLogger(__name__)

STARTUP_MESSAGE = (
    "Zork I is live — DM me !start to begin your private session. "
    "Use !list, !watch, !help on this channel."
)


async def connect(
    *,
    serial: str | None = None,
    baudrate: int = 115200,
    ble: str | None = None,
    tcp: str | None = None,
) -> MeshCore:
    given = [value for value in (serial, ble, tcp) if value]
    if len(given) != 1:
        raise ValueError("specify exactly one of serial, ble, or tcp")

    if serial:
        return await MeshCore.create_serial(serial, baudrate)
    if ble:
        return await MeshCore.create_ble(ble)
    assert tcp is not None
    host, _, port = tcp.partition(":")
    return await MeshCore.create_tcp(host, int(port or 5000))


# autoadd_config bit 0: evict the oldest non-favourite contact when the
# radio's contact table (100 slots) is full, instead of silently dropping
# the new advert. Off by default in firmware. A bot radio has no reason to
# prefer dropping new players over LRU-evicting stale contacts, so this is
# applied unconditionally rather than gated behind a config option.
_AUTOADD_OVERWRITE_OLDEST = 0x01


async def apply_settings(meshcore: MeshCore, config: BotConfig) -> None:
    if config.name:
        result = await meshcore.commands.set_name(config.name)
        _log_apply(f"name={config.name!r}", result)
    channel = config.channel
    result = await meshcore.commands.set_channel(
        channel.index,
        channel.name,
        channel.secret,
    )
    _log_apply(f"channel {channel.index} name={channel.name!r}", result)

    if config.bots_enabled and config.bots_channel is not None:
        bots_channel = config.bots_channel
        result = await meshcore.commands.set_channel(
            bots_channel.index,
            bots_channel.name,
            bots_channel.secret,
        )
        _log_apply(f"channel {bots_channel.index} name={bots_channel.name!r}", result)

    await _ensure_contacts_overwrite_oldest(meshcore)


async def _ensure_contacts_overwrite_oldest(meshcore: MeshCore) -> None:
    current = await meshcore.commands.get_autoadd_config()
    if current.type == EventType.ERROR:
        logger.warning("failed to read autoadd config: %r", current.payload)
        return

    flag = current.payload.get("config", 0)
    if flag & _AUTOADD_OVERWRITE_OLDEST:
        logger.info("contact table overwrite-oldest already enabled")
        return

    result = await meshcore.commands.set_autoadd_config(flag | _AUTOADD_OVERWRITE_OLDEST)
    _log_apply("contact table overwrite-oldest on full", result)


def _log_apply(description: str, result: Event) -> None:
    if result.type == EventType.ERROR:
        logger.warning("failed to %s: %r", description, result.payload)
    else:
        logger.info("applied %s", description)


class MeshCoreRunner:
    def __init__(self, bot: ZorkBot, meshcore: MeshCore) -> None:
        self.bot = bot
        self.meshcore = meshcore
        self._channel_sub: Any | None = None
        self._bots_channel_sub: Any | None = None
        self._dm_sub: Any | None = None
        # Unified send lock: all RF transmissions (channel + DM) serialized here.
        self._send_lock = asyncio.Lock()
        self._last_send_at: float | None = None
        # Running total of packets pending in the lock queue.
        self._send_queue_depth: int = 0

        # Use the advertiser owned by the bot so cooldown state is shared.
        self.advertiser = bot.advertiser

    async def start(self) -> None:
        # Without this, the library only refreshes its local contact cache
        # once (here, on startup). A new advert received afterward marks the
        # cache dirty but is never re-fetched, so get_contact_by_name/
        # get_contact_by_key_prefix keep missing the new contact until the
        # process restarts and takes a fresh snapshot. Setting this makes an
        # ADVERTISEMENT/PATH_UPDATE push event trigger an incremental refetch
        # immediately, so newly-advertised players are recognized live.
        self.meshcore.auto_update_contacts = True
        await self.meshcore.ensure_contacts()

        self._channel_sub = self.meshcore.subscribe(
            EventType.CHANNEL_MSG_RECV,
            self._on_channel_msg,
            attribute_filters={"channel_idx": self.bot.config.channel.index},
        )

        bots_channel = self.bot.config.bots_channel
        if self.bot.config.bots_enabled and bots_channel is not None:
            self._bots_channel_sub = self.meshcore.subscribe(
                EventType.CHANNEL_MSG_RECV,
                self._on_bots_channel_msg,
                attribute_filters={"channel_idx": bots_channel.index},
            )
            logger.info(
                "listening for !bots on channel %d (%s)",
                bots_channel.index,
                bots_channel.name,
            )

        self._dm_sub = self.meshcore.subscribe(
            EventType.CONTACT_MSG_RECV,
            self._on_dm_msg,
        )

        # Give the bot a reference to send DMs.
        self.bot.set_send_dm(self._send_dm_packets)

        await self.meshcore.start_auto_message_fetching()
        logger.info(
            "listening on channel %d (%s) as %r",
            self.bot.config.channel.index,
            self.bot.config.channel.name,
            self.bot.name,
        )

        self.advertiser.start(self.meshcore)

        if self.bot.config.announce_on_start:
            await self._send_chan_msg(
                self.bot.config.channel.index,
                STARTUP_MESSAGE,
            )

    async def stop(self) -> None:
        if self._channel_sub is not None:
            self.meshcore.unsubscribe(self._channel_sub)
            self._channel_sub = None
        if self._bots_channel_sub is not None:
            self.meshcore.unsubscribe(self._bots_channel_sub)
            self._bots_channel_sub = None
        if self._dm_sub is not None:
            self.meshcore.unsubscribe(self._dm_sub)
            self._dm_sub = None
        await self.meshcore.stop_auto_message_fetching()
        await self.advertiser.stop()
        await self.bot.stop()

    async def run_forever(self) -> None:
        await self.start()
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await self.stop()

    # ------------------------------------------------------------------
    # Incoming message handlers
    # ------------------------------------------------------------------

    def _build_channel_message(self, payload: dict) -> IncomingMessage:
        channel_idx = payload.get("channel_idx", 0)
        raw_text = payload.get("text", "")
        # CHANNEL_MSG_RECV packets carry no sender key material — group-channel
        # messages are pre-shared-key broadcasts with no per-sender identity
        # field on the wire. Only CONTACT_MSG_RECV (DMs) has pubkey_prefix.
        pubkey_prefix = payload.get("pubkey_prefix")

        sender_name, sep, body = raw_text.partition(":")
        if sep:
            sender_name = sender_name.strip()
            text = body.strip()
        else:
            sender_name = None
            text = raw_text

        # Resolve identity from the contact table via the sender name embedded
        # in the channel text (the "Name: text" convention companion apps use).
        # This is the only source of pubkey_prefix for channel messages.
        if not pubkey_prefix and sender_name:
            contact = self.meshcore.get_contact_by_name(sender_name)
            if contact:
                public_key = contact.get("public_key", "")
                if public_key:
                    pubkey_prefix = public_key[:12]

        # Resolve display name from contact table if not in message text.
        if not sender_name and pubkey_prefix:
            contact = self.meshcore.get_contact_by_key_prefix(pubkey_prefix)
            if contact:
                sender_name = contact.get("adv_name", pubkey_prefix[:8])

        return IncomingMessage(
            text=text,
            sender_name=sender_name,
            pubkey_prefix=pubkey_prefix,
            is_dm=False,
            channel_idx=channel_idx,
            raw=payload,
        )

    async def _on_channel_msg(self, event: Event) -> None:
        message = self._build_channel_message(event.payload)
        logger.info(
            "channel %d msg player=%s: %r",
            message.channel_idx,
            (message.pubkey_prefix or "?")[:8],
            message.text,
        )

        async def reply(text: str) -> None:
            await self._send_chan_msg(message.channel_idx, text)

        await self.bot.dispatch_channel(message, reply)

    async def _on_bots_channel_msg(self, event: Event) -> None:
        message = self._build_channel_message(event.payload)
        logger.info(
            "bots-channel %d msg player=%s: %r",
            message.channel_idx,
            (message.pubkey_prefix or "?")[:8],
            message.text,
        )

        async def reply(text: str) -> None:
            await self._send_chan_msg(message.channel_idx, text)

        await self.bot.dispatch_bots_channel(message, reply)

    async def _on_dm_msg(self, event: Event) -> None:
        payload = event.payload
        pubkey_prefix = payload.get("pubkey_prefix")
        text = payload.get("text", "").strip()

        # Resolve display name from contact table.
        sender_name: str | None = None
        if pubkey_prefix:
            contact = self.meshcore.get_contact_by_key_prefix(pubkey_prefix)
            if contact:
                sender_name = contact.get("adv_name", pubkey_prefix[:8])
            else:
                sender_name = pubkey_prefix[:8]

        message = IncomingMessage(
            text=text,
            sender_name=sender_name,
            pubkey_prefix=pubkey_prefix,
            is_dm=True,
            channel_idx=0,
            raw=payload,
        )
        logger.info(
            "DM from player=%s: %r",
            (pubkey_prefix or "?")[:8],
            text,
        )

        async def reply(reply_text: str) -> None:
            if pubkey_prefix:
                await self._send_dm(pubkey_prefix, reply_text)

        await self.bot.dispatch_dm(message, reply)

    # ------------------------------------------------------------------
    # Send helpers — all go through the unified lock
    # ------------------------------------------------------------------

    async def _send_dm_packets(self, pubkey_prefix: str, text: str) -> None:
        """Called by bot.py to send a DM. Goes through the unified send gate."""
        await self._send_dm(pubkey_prefix, text)

    async def _send_dm(self, pubkey_prefix: str, text: str) -> None:
        await self._send_with_spacing(
            self.meshcore.commands.send_msg(pubkey_prefix, text)
        )

    async def _send_chan_msg(self, channel_idx: int, text: str) -> Event:
        return await self._send_with_spacing(
            self.meshcore.commands.send_chan_msg(channel_idx, text)
        )

    async def _send_with_spacing(self, coro) -> Any:
        max_depth = self.bot.config.max_send_queue_depth
        if self._send_queue_depth >= max_depth:
            logger.warning(
                "send queue overflow (depth=%d) — dropping packet", self._send_queue_depth
            )
            return None

        self._send_queue_depth += 1
        try:
            async with self._send_lock:
                if self._last_send_at is not None:
                    elapsed = asyncio.get_running_loop().time() - self._last_send_at
                    remaining = self.bot.config.send_spacing_seconds - elapsed
                    if remaining > 0:
                        await asyncio.sleep(remaining)
                try:
                    return await coro
                finally:
                    self._last_send_at = asyncio.get_running_loop().time()
        finally:
            self._send_queue_depth -= 1
