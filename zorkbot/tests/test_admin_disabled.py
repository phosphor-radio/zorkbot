"""Verifies the admin UI is fully inert when [admin_ui] enabled = false (the default)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from zorkbot.admin.events import NullEventSink
from zorkbot.advertiser import Advertiser
from zorkbot.config import BotConfig
from zorkbot.game_client import GameClient
from zorkbot.session_state import SessionState


def test_admin_ui_disabled_by_default() -> None:
    assert BotConfig().admin_ui.enabled is False


def test_bot_defaults_to_null_event_sink() -> None:
    from zorkbot.bot import ZorkBot

    config = BotConfig()
    game = GameClient(config.game_url)
    advertiser = Advertiser()
    meshcore = MagicMock()

    bot = ZorkBot(config, game, advertiser, meshcore)
    assert isinstance(bot.event_sink, NullEventSink)


def test_null_event_sink_is_a_true_no_op() -> None:
    sink = NullEventSink()
    # None of these should raise or require a running event loop.
    sink.message_rx(transport="dm", channel_idx=None, pubkey_prefix="a", chars=1)
    sink.message_tx(transport="dm", channel_idx=None, pubkey_prefix="a", chars=1)
    sink.command(pubkey_prefix="a", command="look", transport="dm", channel_idx=None, accepted=True)
    sink.player_seen(pubkey_prefix="a", name="Alice")
    sink.transcript(session_num=1, player_name="Alice", command="look", output="You see...")


def test_session_state_defaults_to_null_event_sink() -> None:
    state = SessionState()
    assert isinstance(state.event_sink, NullEventSink)
    # Exercising the full lifecycle must not raise with no sink configured.
    record = state.add_session("aabbccddeeff", "Alice")
    state.add_watcher("112233445566", record.num)
    state.remove_watcher("112233445566")
    state.remove_session("aabbccddeeff", reason="player_end")
