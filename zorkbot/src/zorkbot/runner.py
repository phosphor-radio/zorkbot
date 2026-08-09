"""MeshCore device connection and message loop."""

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

SEND_SPACING_SECONDS = 2.0
STARTUP_MESSAGE = "Zork I is live on #zork — try !zork look"


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


def _log_apply(description: str, result: Event) -> None:
    if result.type == EventType.ERROR:
        logger.warning("failed to %s: %r", description, result.payload)
    else:
        logger.info("applied %s", description)


class MeshCoreRunner:
    def __init__(self, bot: ZorkBot, meshcore: MeshCore) -> None:
        self.bot = bot
        self.meshcore = meshcore
        self._subscription: Any | None = None
        self._send_lock = asyncio.Lock()
        self._last_send_at: float | None = None

    async def start(self) -> None:
        await self.meshcore.ensure_contacts()
        self._subscription = self.meshcore.subscribe(
            EventType.CHANNEL_MSG_RECV,
            self._on_channel_msg,
            attribute_filters={"channel_idx": self.bot.config.channel.index},
        )
        await self.meshcore.start_auto_message_fetching()
        logger.info(
            "listening on channel %d (%s) as %r",
            self.bot.config.channel.index,
            self.bot.config.channel.name,
            self.bot.name,
        )
        if self.bot.config.announce_on_start:
            await self._send_chan_msg(
                self.bot.config.channel.index,
                STARTUP_MESSAGE,
            )

    async def stop(self) -> None:
        if self._subscription is not None:
            self.meshcore.unsubscribe(self._subscription)
            self._subscription = None
        await self.meshcore.stop_auto_message_fetching()

    async def run_forever(self) -> None:
        await self.start()
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await self.stop()

    async def _on_channel_msg(self, event: Event) -> None:
        payload = event.payload
        channel_idx = payload.get("channel_idx", 0)
        raw_text = payload.get("text", "")
        sender_name, sep, body = raw_text.partition(":")
        if sep:
            sender_name = sender_name.strip()
            text = body.strip()
        else:
            sender_name = None
            text = raw_text

        message = IncomingMessage(
            text=text,
            sender_name=sender_name,
            channel_idx=channel_idx,
            raw=payload,
        )
        logger.info(
            "channel %d msg from %s: %r",
            channel_idx,
            sender_name,
            text,
        )

        async def reply(text: str) -> None:
            logger.info("channel %d reply: %r", channel_idx, text)
            result = await self._send_chan_msg(channel_idx, text)
            if result.type == EventType.ERROR:
                logger.error("failed to send reply: %r", result.payload)

        await self.bot.dispatch(message, reply)

    async def _send_chan_msg(self, channel_idx: int, text: str) -> Event:
        async with self._send_lock:
            if self._last_send_at is not None:
                elapsed = asyncio.get_running_loop().time() - self._last_send_at
                remaining = SEND_SPACING_SECONDS - elapsed
                if remaining > 0:
                    await asyncio.sleep(remaining)
            try:
                return await self.meshcore.commands.send_chan_msg(channel_idx, text)
            finally:
                self._last_send_at = asyncio.get_running_loop().time()
