"""TOML configuration for zorkbot."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from zorkbot.channels import ChannelConfig, ZORK_CHANNEL_NAME


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
    announce_on_start: bool = True

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
    if name := data.get("name"):
        config.name = str(name)
    if game_url := data.get("game_url"):
        config.game_url = str(game_url)
    if admin_token := data.get("admin_token"):
        config.admin_token = str(admin_token)
    if log_level := data.get("log_level"):
        config.log_level = str(log_level)
    if packet_max_chars := data.get("packet_max_chars"):
        config.packet_max_chars = int(packet_max_chars)
    if "announce_on_start" in data:
        config.announce_on_start = bool(data["announce_on_start"])

    channel = data.get("channel", {})
    if channel:
        config.channel = ChannelConfig(
            index=int(channel.get("index", config.channel.index)),
            name=str(channel.get("name", ZORK_CHANNEL_NAME)),
        )

    admin = data.get("admin", {})
    if names := admin.get("names"):
        config.admin = AdminConfig(names=[str(name) for name in names])
