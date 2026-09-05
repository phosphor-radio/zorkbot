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
_ADMIN_UI_KEYS = frozenset({
    "enabled", "bind", "port", "db_path",
    "access_token_ttl_seconds", "refresh_token_ttl_seconds",
    "event_retention_days", "event_queue_size",
    "live_buffer_events", "max_live_streams",
})
_CHANNEL_KEYS = frozenset({"index", "name", "secret"})
_ROOT_OPTIONAL_KEYS = frozenset({
    "log_level",
    "packet_max_chars",
    "announce_on_start",
    "bots_cooldown_seconds",
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
class AdminUIConfig:
    # Master switch: false = no HTTP server, no event logging, no DB file.
    enabled: bool = False
    bind: str = "0.0.0.0"
    port: int = 8081
    db_path: str = "/data/admin.db"
    access_token_ttl_seconds: int = 1800
    refresh_token_ttl_seconds: int = 2592000
    event_retention_days: int = 90
    event_queue_size: int = 1024
    live_buffer_events: int = 50
    max_live_streams: int = 4


@dataclass
class BotConfig:
    name: str = "zorkbot"
    channel: ChannelConfig = field(default_factory=lambda: ChannelConfig(index=1))
    game_url: str = "http://game:8080"
    admin: AdminConfig = field(default_factory=AdminConfig)
    admin_ui: AdminUIConfig = field(default_factory=AdminUIConfig)
    log_level: str | None = None
    packet_max_chars: int = 120
    announce_on_start: bool = False

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
    # Minimum gap between !bots roll-call replies. Global, not per-sender: the
    # reply is a broadcast, so several players asking at once should draw one
    # answer for the channel. Must exceed handle_bots' collision-avoidance delay
    # (REPLY_DELAY_BASE + JITTER = 10s), otherwise a second roll call is admitted
    # while the first reply is still waiting to transmit.
    bots_cooldown_seconds: float = 12.0

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

    if admin_ui_enabled := os.getenv("ADMIN_UI_ENABLED"):
        config.admin_ui.enabled = admin_ui_enabled.strip().lower() in ("1", "true", "yes", "on")
    if admin_ui_bind := os.getenv("ADMIN_UI_BIND"):
        config.admin_ui.bind = admin_ui_bind
    if admin_ui_port := os.getenv("ADMIN_UI_PORT"):
        config.admin_ui.port = int(admin_ui_port)
    if admin_ui_db := os.getenv("ADMIN_UI_DB"):
        config.admin_ui.db_path = admin_ui_db

    return config


def _apply_toml(config: BotConfig, data: dict) -> None:
    admin = data.get("admin", {})
    admin_ui = data.get("admin_ui", {})
    channel = data.get("channel", {})
    bots_channel = data.get("bots_channel", {})
    _warn_misplaced_section_keys("admin", admin, _ADMIN_KEYS)
    _warn_misplaced_section_keys("admin_ui", admin_ui, _ADMIN_UI_KEYS)
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
    bots_cooldown_seconds = _root_value(data, channel, admin, "bots_cooldown_seconds")
    if bots_cooldown_seconds is None:
        bots_cooldown_seconds = bots_channel.get("bots_cooldown_seconds")
    if bots_cooldown_seconds is not None:
        config.bots_cooldown_seconds = float(bots_cooldown_seconds)

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

    if admin_ui:
        ui = config.admin_ui
        if "enabled" in admin_ui:
            ui.enabled = bool(admin_ui["enabled"])
        if "bind" in admin_ui:
            ui.bind = str(admin_ui["bind"])
        if "port" in admin_ui:
            ui.port = int(admin_ui["port"])
        if "db_path" in admin_ui:
            ui.db_path = str(admin_ui["db_path"])
        if "access_token_ttl_seconds" in admin_ui:
            ui.access_token_ttl_seconds = int(admin_ui["access_token_ttl_seconds"])
        if "refresh_token_ttl_seconds" in admin_ui:
            ui.refresh_token_ttl_seconds = int(admin_ui["refresh_token_ttl_seconds"])
        if "event_retention_days" in admin_ui:
            ui.event_retention_days = int(admin_ui["event_retention_days"])
        if "event_queue_size" in admin_ui:
            ui.event_queue_size = int(admin_ui["event_queue_size"])
        if "live_buffer_events" in admin_ui:
            ui.live_buffer_events = int(admin_ui["live_buffer_events"])
        if "max_live_streams" in admin_ui:
            ui.max_live_streams = int(admin_ui["max_live_streams"])


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
