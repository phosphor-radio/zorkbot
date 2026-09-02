"""Shared request-scoped context for the admin API routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zorkbot.admin.auth import AuthService
    from zorkbot.admin.bus import SessionBus
    from zorkbot.admin.events import SqliteEventSink
    from zorkbot.admin.store import Store
    from zorkbot.bot import ZorkBot
    from zorkbot.config import AdminUIConfig


@dataclass
class AdminContext:
    store: "Store"
    bus: "SessionBus"
    auth: "AuthService"
    sink: "SqliteEventSink"
    bot: "ZorkBot"
    config: "AdminUIConfig"
    process_started_at: float
