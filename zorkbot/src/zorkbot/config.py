"""TOML configuration for zorkbot."""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from zorkbot.channels import ChannelConfig, ZORK_CHANNEL_NAME

logger = logging.getLogger(__name__)

_ADMIN_KEYS = frozenset({"names"})
_CHANNEL_KEYS = frozenset({"index", "name", "secret"})
_ROOT_OPTIONAL_KEYS = frozenset({
    "log_level",
    "packet_max_chars",
    "announce_on_start",
    "command_queue_size",
    "rate_limit_seconds",
})


@dataclass
class AdminConfig:
    names: list[str] = field(default_factory=list)


@dataclass
class BotConfig:
    name: str = "zorkbot"
    channel: ChannelConfig = field(default_factory=lambda: ChannelConfig(index=1))
    game_url: str = "http://game:8080"
    admin_token: str | None = None
    admin: AdminConfig = field(default_factory=AdminConfig)
    log_level: str | None = None
    packet_max_chars: int = 100
    announce_on_start: bool = False
    command_queue_size: int = 8
    rate_limit_seconds: float = 3.0

    @property
    def admin_names(self) -> frozenset[str]:
        return frozenset(name.lower() for name in self.admin.names)


def load_config(path: str | Path | None) -> BotConfig:
    config = BotConfig()
    if path is not None:
        with Path(path).open("rb") as handle:
            data = tomllib.load(handle)
        _apply_toml(config, data)

    if game_url := os.getenv("GAME_URL"):
        config.game_url = game_url
    if admin_token := os.getenv("ADMIN_TOKEN"):
        config.admin_token = admin_token

    return config


def _apply_toml(config: BotConfig, data: dict) -> None:
    admin = data.get("admin", {})
    channel = data.get("channel", {})
    _warn_misplaced_section_keys("admin", admin, _ADMIN_KEYS)
    _warn_misplaced_section_keys("channel", channel, _CHANNEL_KEYS)

    if name := data.get("name"):
        config.name = str(name)
    if game_url := data.get("game_url"):
        config.game_url = str(game_url)
    if admin_token := data.get("admin_token"):
        config.admin_token = str(admin_token)
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

    if channel:
        config.channel = ChannelConfig(
            index=int(channel.get("index", config.channel.index)),
            name=str(channel.get("name", ZORK_CHANNEL_NAME)),
        )

    if names := admin.get("names"):
        config.admin = AdminConfig(names=[str(name) for name in names])


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
