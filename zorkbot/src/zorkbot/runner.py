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
    "Zork I is live - DM me !start to begin your private session. "
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


# The radio queues everything it receives while no client is attached and
# hands the whole backlog over on connect. Without draining it first, a
# restart makes the bot answer commands sent hours ago the moment it comes
# up: stale !start requests spawn sessions nobody is waiting on, and old
# channel chatter gets replayed at everyone. The cap keeps startup bounded
# if the device never reports NO_MORE_MSGS or the mesh is busy enough that
# new traffic arrives as fast as it is pulled.
_MAX_FLUSH_MESSAGES = 500


async def flush_pending_messages(meshcore: MeshCore) -> int:
    """Pull and discard the messages queued while the bot was offline.

    Must run before the CHANNEL_MSG_RECV/CONTACT_MSG_RECV subscriptions
    exist: get_msg() dispatches each fetched message as an event, so
    draining early means the backlog is delivered to no handler.
    """
    flushed = 0
    while flushed < _MAX_FLUSH_MESSAGES:
        result = await meshcore.commands.get_msg()
        if result.type in (EventType.NO_MORE_MSGS, EventType.ERROR):
            break
        flushed += 1
    else:
        logger.warning(
            "stopped flushing after %d queued message(s); the rest will be "
            "handled live",
            flushed,
        )

    if flushed:
        logger.info("flushed %d message(s) queued while offline", flushed)
    else:
        logger.info("no queued messages to flush")
    return flushed


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
        # When a channel message last arrived. The mesh is still repeating
        # that flood for a moment afterwards, so transmissions hold off for
        # channel_rx_guard_seconds past it - see _send_with_spacing.
        self._last_channel_rx_at: float | None = None
        # Running total of packets pending in the lock queue.
        self._send_queue_depth: int = 0

        # Use the advertiser owned by the bot so cooldown state is shared,
        # and route its transmissions through this runner's send gate.
        self.advertiser = bot.advertiser
        self.advertiser.set_transmit(self.send_advert)

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

        # Drop the offline backlog before any handler is listening. The
        # count is surfaced on the admin UI's /api/status so a restart that
        # silently swallowed a pile of traffic is visible after the fact.
        self.bot.set_startup_flushed_messages(
            await flush_pending_messages(self.meshcore)
        )

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

        # Give the bot a reference to send DMs. Two of them: player DMs wait
        # for the recipient's ACK, watcher fan-out does not.
        self.bot.set_send_dm(self._send_dm_packets)
        self.bot.set_send_watcher_dm(self._send_watcher_dm)
        self.bot.set_send_queue_depth_getter(lambda: self._send_queue_depth)
        self.bot.start_session_poller()

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
        self._record_rx(message)
        self._note_channel_rx()

        async def reply(text: str) -> bool:
            # A channel message is a broadcast with no per-recipient ACK, so
            # delivery is never measured and a reply is never abandoned.
            await self._send_chan_msg(message.channel_idx, text)
            return True

        await self.bot.dispatch_channel(message, reply)

    async def _on_bots_channel_msg(self, event: Event) -> None:
        message = self._build_channel_message(event.payload)
        logger.info(
            "bots-channel %d msg player=%s: %r",
            message.channel_idx,
            (message.pubkey_prefix or "?")[:8],
            message.text,
        )
        self._record_rx(message)
        self._note_channel_rx()

        async def reply(text: str) -> bool:
            await self._send_chan_msg(message.channel_idx, text)
            return True

        await self.bot.dispatch_bots_channel(message, reply)

    def _note_channel_rx(self) -> None:
        self._last_channel_rx_at = asyncio.get_running_loop().time()

    def _record_rx(self, message: IncomingMessage) -> None:
        if message.pubkey_prefix:
            self.bot.event_sink.player_seen(
                pubkey_prefix=message.pubkey_prefix, name=message.sender_name
            )
        self.bot.event_sink.message_rx(
            transport="dm" if message.is_dm else "channel",
            channel_idx=None if message.is_dm else message.channel_idx,
            pubkey_prefix=message.pubkey_prefix,
            chars=len(message.text),
        )

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
        self._record_rx(message)

        async def reply(reply_text: str) -> bool:
            if not pubkey_prefix:
                # Nothing to send to, so nothing was measured. True, not
                # False: False means a measured delivery failure and would
                # truncate the rest of the response.
                return True
            return await self._send_dm(pubkey_prefix, reply_text)

        await self.bot.dispatch_dm(message, reply)

    # ------------------------------------------------------------------
    # Send helpers — all go through the unified lock
    # ------------------------------------------------------------------

    async def _send_dm_packets(self, pubkey_prefix: str, text: str) -> bool:
        """Called by bot.py to send a player DM. Goes through the unified
        send gate. True when the recipient acknowledged it."""
        return await self._send_dm(pubkey_prefix, text)

    def _dm_destination(self, pubkey_prefix: str) -> Any:
        """The full contact if the radio knows one, else the bare prefix.

        The contact carries the 32-byte public key and out_path_len, which is
        what lets the library tell a direct path from a flood one and reset
        the path after repeated failures. A 12-char prefix cannot express
        either, so passing one silently means "assume flood, never re-route".
        Falling back to it when the contact is unknown reproduces that
        conservative behaviour rather than failing the send.
        """
        contact = self.meshcore.get_contact_by_key_prefix(pubkey_prefix)
        return contact if contact else pubkey_prefix

    async def _send_dm(self, pubkey_prefix: str, text: str) -> bool:
        """Send one DM to a player and wait for its delivery ACK.

        Returns False for both a delivery failure and a local queue-overflow
        drop — from the caller's point of view the packet did not arrive
        either way. The two are distinguished in the stats, not here.
        """
        config = self.bot.config
        if not config.dm_ack_enabled:
            result = await self._send_with_spacing(
                self.meshcore.commands.send_msg(pubkey_prefix, text),
                transport="dm",
                pubkey_prefix=pubkey_prefix,
                chars=len(text),
            )
            return result is not None

        result = await self._send_with_spacing(
            self.meshcore.commands.send_msg_with_retry(
                self._dm_destination(pubkey_prefix),
                text,
                max_attempts=config.dm_ack_max_attempts,
                max_flood_attempts=config.dm_ack_max_flood_attempts,
                flood_after=config.dm_ack_flood_after,
                timeout=config.dm_ack_timeout_seconds,
                # Every attempt after the first is another transmission, and
                # the ACK wait is the gap in front of it — so flooring that
                # wait at send_spacing_seconds is what keeps retries subject
                # to the same spacing as every other packet the bot sends.
                min_timeout=config.send_spacing_seconds,
            ),
            transport="dm",
            pubkey_prefix=pubkey_prefix,
            chars=len(text),
            ack_aware=True,
        )
        return result is not None

    async def _send_watcher_dm(self, pubkey_prefix: str, text: str) -> None:
        """Send one watcher fan-out DM, fire and forget.

        Deliberately not ACK-waited and not retried (docs/specs/dm-ack-retry.md).
        Watcher output is a courtesy stream about someone else's game, and
        fan-out is where ACK-waiting would cost most: watchers x packets, each
        one holding the send gate through its own ACK window, in airtime taken
        from every player. That is the tax the decoupled fan-out queue exists
        to keep off the player being watched.
        """
        await self._send_with_spacing(
            self.meshcore.commands.send_msg(pubkey_prefix, text),
            transport="dm",
            pubkey_prefix=pubkey_prefix,
            chars=len(text),
        )

    async def send_advert(self, *, flood: bool) -> Any:
        """Transmit an advert through the same gate as messages.

        An advert is RF like anything else - a flood advert reaches further
        than any message the bot sends - so it queues behind whatever is
        already going out rather than jumping the line.
        """
        return await self._send_with_spacing(
            self.meshcore.commands.send_advert(flood=flood),
            # Not recorded in message_tx: the stats tables count messages by
            # dm/channel transport, and "both" there means the sum of the
            # two. Adding a third value would quietly break that. Advert
            # airtime staying unaccounted is pre-existing, and a separate
            # question from whether adverts respect the send gate.
            record=False,
        )

    async def _send_chan_msg(self, channel_idx: int, text: str) -> Event:
        return await self._send_with_spacing(
            self.meshcore.commands.send_chan_msg(channel_idx, text),
            transport="channel",
            channel_idx=channel_idx,
            chars=len(text),
        )

    async def _wait_for_quiet_air(self) -> None:
        """Hold the send lock until it is this transmission's turn on the air.

        Two deadlines, tracked independently and both enforced:

        - send_spacing_seconds after the bot's own last transmission,
        - channel_rx_guard_seconds after the last channel message arrived.

        Whichever falls later governs. Neither overrides the other: a long
        spacing that happens to outlast the guard satisfies it on the way
        past, and with nothing recently transmitted the guard governs on its
        own - a reply to a channel command goes out once the flood has
        settled rather than waiting on a spacing gap that isn't there.

        Re-checked after every sleep, so a channel message that arrives
        while this transmission is already waiting re-arms the guard instead
        of being missed. The guard is deliberately not scoped to the reply
        for one particular message: the mesh is busy repeating the flood
        regardless of which of the bot's packets is next in line.
        """
        while True:
            now = asyncio.get_running_loop().time()
            deadlines = []
            if self._last_send_at is not None:
                deadlines.append(self._last_send_at + self.bot.config.send_spacing_seconds)
            if self._last_channel_rx_at is not None:
                deadlines.append(
                    self._last_channel_rx_at + self.bot.config.channel_rx_guard_seconds
                )
            remaining = max((deadline - now for deadline in deadlines), default=0.0)
            if remaining <= 0:
                return
            await asyncio.sleep(remaining)

    async def _send_with_spacing(
        self,
        coro,
        *,
        transport: str = "dm",
        channel_idx: int | None = None,
        pubkey_prefix: str | None = None,
        chars: int = 0,
        record: bool = True,
        ack_aware: bool = False,
    ) -> Any:
        max_depth = self.bot.config.max_send_queue_depth
        if self._send_queue_depth >= max_depth:
            logger.warning(
                "send queue overflow (depth=%d) - dropping packet", self._send_queue_depth
            )
            # coro was built by the caller and is never awaited on this path;
            # close it so it doesn't surface as a "never awaited" RuntimeWarning
            # right when the log is being read for the overflow itself.
            coro.close()
            if record:
                # acked stays None: the packet was never transmitted, which is
                # a different fact from "transmitted and not acknowledged".
                self.bot.event_sink.message_tx(
                    transport=transport,
                    channel_idx=channel_idx,
                    pubkey_prefix=pubkey_prefix,
                    chars=chars,
                    dropped=True,
                )
            return None

        self._send_queue_depth += 1
        try:
            async with self._send_lock:
                await self._wait_for_quiet_air()
                try:
                    result = await coro
                    # None only means "not delivered" for a send that waited
                    # for an ACK; everything else returns None routinely.
                    acked = (result is not None) if ack_aware else None
                    if acked is False:
                        # "up to": a flood-routed contact is capped at
                        # dm_ack_max_flood_attempts, and the library does not
                        # report how many attempts it actually made.
                        logger.warning(
                            "DM not acknowledged player=%s chars=%d after up to %d attempt(s)",
                            (pubkey_prefix or "?")[:8],
                            chars,
                            self.bot.config.dm_ack_max_attempts,
                        )
                    if record:
                        self.bot.event_sink.message_tx(
                            transport=transport,
                            channel_idx=channel_idx,
                            pubkey_prefix=pubkey_prefix,
                            chars=chars,
                            acked=acked,
                        )
                    return result
                finally:
                    self._last_send_at = asyncio.get_running_loop().time()
        finally:
            self._send_queue_depth -= 1
