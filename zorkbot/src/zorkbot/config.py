"""TOML configuration for zorkbot."""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from zorkbot.channels import BOTS_CHANNEL_NAME, ChannelConfig, ZORK_CHANNEL_NAME

logger = logging.getLogger(__name__)

_ADMIN_KEYS = frozenset({"pubkeys"})
_CHANNEL_KEYS = frozenset({"index", "name", "secret"})
_ROOT_OPTIONAL_KEYS = frozenset({
    "log_level",
    "packet_max_chars",
    "announce_on_start",
    "command_queue_size",
    "rate_limit_seconds",
    "max_watchers_per_session",
    "advert_enabled",
    "advert_flood",
    "advert_interval_seconds",
    "advert_cooldown_seconds",
    "send_spacing_seconds",
    "max_send_queue_depth",
    "bots_enabled",
    "session_poll_seconds",
})


@dataclass
class AdminConfig:
    # List of 12-character lowercase hex pubkey prefixes for admin users.
    pubkeys: list[str] = field(default_factory=list)


@dataclass
class BotConfig:
    name: str = "zorkbot"
    channel: ChannelConfig = field(default_factory=lambda: ChannelConfig(index=1))
    game_url: str = "http://game:8080"
    admin: AdminConfig = field(default_factory=AdminConfig)
    log_level: str | None = None
    packet_max_chars: int = 120
    announce_on_start: bool = False
    command_queue_size: int = 8

    # Rate limiting
    rate_limit_seconds: float = 3.0

    # Session management
    max_watchers_per_session: int = 2
    # How often to poll the game service for sessions it ended server-side
    # (inactivity timeout, PTY crash) so their watchers can be notified —
    # the bot has no other way to learn about those. 0 disables polling.
    session_poll_seconds: int = 30

    # Advertising
    advert_enabled: bool = False
    advert_flood: bool = True
    advert_interval_seconds: int = 300
    advert_cooldown_seconds: int = 300

    # RF send serialization
    send_spacing_seconds: float = 2.0
    max_send_queue_depth: int = 64

    # Mesh bot discovery (!bots roll call) — a separate channel from the
    # game lobby, disabled by default, and only active once both a
    # [bots_channel] section is configured and this is set.
    bots_enabled: bool = False
    bots_channel: ChannelConfig | None = None

    @property
    def admin_pubkeys(self) -> frozenset[str]:
        return frozenset(pk.lower() for pk in self.admin.pubkeys)


def load_config(path: str | Path | None) -> BotConfig:
    config = BotConfig()
    if path is not None:
        with Path(path).open("rb") as handle:
            data = tomllib.load(handle)
        _apply_toml(config, data)

    if game_url := os.getenv("GAME_URL"):
        config.game_url = game_url

    return config


def _apply_toml(config: BotConfig, data: dict) -> None:
    admin = data.get("admin", {})
    channel = data.get("channel", {})
    bots_channel = data.get("bots_channel", {})
    _warn_misplaced_section_keys("admin", admin, _ADMIN_KEYS)
    _warn_misplaced_section_keys("channel", channel, _CHANNEL_KEYS)
    _warn_misplaced_section_keys("bots_channel", bots_channel, _CHANNEL_KEYS)

    if name := data.get("name"):
        config.name = str(name)
    if game_url := data.get("game_url"):
        config.game_url = str(game_url)
    if log_level := _root_value(data, channel, admin, "log_level"):
        config.log_level = str(log_level)
    if packet_max_chars := _root_value(data, channel, admin, "packet_max_chars"):
        config.packet_max_chars = int(packet_max_chars)
    announce_on_start = _root_value(data, channel, admin, "announce_on_start")
    if announce_on_start is not None:
        config.announce_on_start = bool(announce_on_start)
    if command_queue_size := _root_value(data, channel, admin, "command_queue_size"):
        config.command_queue_size = int(command_queue_size)
    if rate_limit_seconds := _root_value(data, channel, admin, "rate_limit_seconds"):
        config.rate_limit_seconds = float(rate_limit_seconds)
    if max_watchers_per_session := _root_value(data, channel, admin, "max_watchers_per_session"):
        config.max_watchers_per_session = int(max_watchers_per_session)
    session_poll_seconds = _root_value(data, channel, admin, "session_poll_seconds")
    if session_poll_seconds is not None:
        config.session_poll_seconds = int(session_poll_seconds)
    advert_enabled = _root_value(data, channel, admin, "advert_enabled")
    if advert_enabled is not None:
        config.advert_enabled = bool(advert_enabled)
    advert_flood = _root_value(data, channel, admin, "advert_flood")
    if advert_flood is not None:
        config.advert_flood = bool(advert_flood)
    if advert_interval_seconds := _root_value(data, channel, admin, "advert_interval_seconds"):
        config.advert_interval_seconds = int(advert_interval_seconds)
    if advert_cooldown_seconds := _root_value(data, channel, admin, "advert_cooldown_seconds"):
        config.advert_cooldown_seconds = int(advert_cooldown_seconds)
    if send_spacing_seconds := _root_value(data, channel, admin, "send_spacing_seconds"):
        config.send_spacing_seconds = float(send_spacing_seconds)
    if max_send_queue_depth := _root_value(data, channel, admin, "max_send_queue_depth"):
        config.max_send_queue_depth = int(max_send_queue_depth)
    bots_enabled = _root_value(data, channel, admin, "bots_enabled")
    if bots_enabled is None:
        bots_enabled = bots_channel.get("bots_enabled")
    if bots_enabled is not None:
        config.bots_enabled = bool(bots_enabled)

    if channel:
        config.channel = ChannelConfig(
            index=int(channel.get("index", config.channel.index)),
            name=str(channel.get("name", ZORK_CHANNEL_NAME)),
        )

    if bots_channel:
        config.bots_channel = ChannelConfig(
            index=int(bots_channel.get("index", 0)),
            name=str(bots_channel.get("name", BOTS_CHANNEL_NAME)),
        )

    if pubkeys := admin.get("pubkeys"):
        config.admin = AdminConfig(pubkeys=[str(pk) for pk in pubkeys])


def _warn_misplaced_section_keys(section: str, values: dict, allowed: frozenset[str]) -> None:
    for key in sorted(values):
        if key in allowed:
            continue
        if key in _ROOT_OPTIONAL_KEYS:
            logger.warning(
                "%s is under [%s] but belongs at the top level of zorkbot.toml; "
                "applying anyway",
                key,
                section,
            )
            continue
        logger.warning("unknown key under [%s]: %s", section, key)


def _root_value(data: dict, channel: dict, admin: dict, key: str):
    if key in data:
        return data[key]
    if key in channel:
        return channel[key]
    if key in admin:
        return admin[key]
    return None
