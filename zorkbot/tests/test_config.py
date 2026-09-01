"""Tests for config loading."""

from pathlib import Path

from zorkbot.config import load_config


def test_load_config_from_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "zorkbot.toml"
    config_path.write_text(
        """
name = "testbot"
game_url = "http://localhost:18080"

[channel]
index = 2
name = "#zork"

[admin]
pubkeys = ["aabbccddeeff", "112233445566"]
""".strip()
    )
    config = load_config(config_path)
    assert config.name == "testbot"
    assert config.game_url == "http://localhost:18080"
    assert config.channel.index == 2
    assert config.admin_pubkeys == frozenset({"aabbccddeeff", "112233445566"})
    assert config.announce_on_start is False


def test_announce_on_start_defaults_false_when_absent() -> None:
    assert load_config(None).announce_on_start is False


def test_announce_on_start_can_be_enabled(tmp_path: Path) -> None:
    config_path = tmp_path / "zorkbot.toml"
    config_path.write_text("announce_on_start = true\n")
    assert load_config(config_path).announce_on_start is True


def test_session_defaults() -> None:
    config = load_config(None)
    assert config.max_watchers_per_session == 2
    assert config.advert_interval_seconds == 300
    assert config.advert_cooldown_seconds == 300
    assert config.send_spacing_seconds == 2.0
    assert config.max_send_queue_depth == 64
    assert config.session_poll_seconds == 30


def test_session_poll_seconds_from_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "zorkbot.toml"
    config_path.write_text("session_poll_seconds = 15\n")
    assert load_config(config_path).session_poll_seconds == 15


def test_session_poll_seconds_can_be_disabled(tmp_path: Path) -> None:
    config_path = tmp_path / "zorkbot.toml"
    config_path.write_text("session_poll_seconds = 0\n")
    assert load_config(config_path).session_poll_seconds == 0


def test_session_config_from_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "zorkbot.toml"
    config_path.write_text(
        """
max_watchers_per_session = 3
send_spacing_seconds = 1.5
""".strip()
    )
    config = load_config(config_path)
    assert config.max_watchers_per_session == 3
    assert config.send_spacing_seconds == 1.5


def test_load_config_accepts_misplaced_root_keys_under_admin(tmp_path: Path) -> None:
    config_path = tmp_path / "zorkbot.toml"
    config_path.write_text(
        """
[admin]
pubkeys = ["aabbccddeeff"]
announce_on_start = false
packet_max_chars = 120
""".strip()
    )
    config = load_config(config_path)
    assert config.announce_on_start is False
    assert config.packet_max_chars == 120
    assert config.admin_pubkeys == frozenset({"aabbccddeeff"})


def test_load_config_accepts_misplaced_root_keys_under_channel(tmp_path: Path) -> None:
    config_path = tmp_path / "zorkbot.toml"
    config_path.write_text(
        """
[channel]
index = 1
announce_on_start = false
packet_max_chars = 120
""".strip()
    )
    config = load_config(config_path)
    assert config.announce_on_start is False
    assert config.packet_max_chars == 120


def test_bots_channel_disabled_by_default() -> None:
    config = load_config(None)
    assert config.bots_enabled is False
    assert config.bots_channel is None


def test_bots_channel_from_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "zorkbot.toml"
    config_path.write_text(
        """
bots_enabled = true

[bots_channel]
index = 3
name = "#bots"
""".strip()
    )
    config = load_config(config_path)
    assert config.bots_enabled is True
    assert config.bots_channel.index == 3
    assert config.bots_channel.name == "#bots"


def test_bots_channel_configured_but_disabled(tmp_path: Path) -> None:
    """A [bots_channel] section alone doesn't turn the feature on."""
    config_path = tmp_path / "zorkbot.toml"
    config_path.write_text(
        """
[bots_channel]
index = 3
""".strip()
    )
    config = load_config(config_path)
    assert config.bots_enabled is False
    assert config.bots_channel.index == 3
