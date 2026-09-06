"""Tests for config loading."""

from pathlib import Path

from zorkbot.config import BotConfig, load_config


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
    assert config.channel_rx_guard_seconds == 2.0
    assert config.session_poll_seconds == 30


def test_session_poll_seconds_from_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "zorkbot.toml"
    config_path.write_text("session_poll_seconds = 15\n")
    assert load_config(config_path).session_poll_seconds == 15


def test_session_poll_seconds_can_be_disabled(tmp_path: Path) -> None:
    config_path = tmp_path / "zorkbot.toml"
    config_path.write_text("session_poll_seconds = 0\n")
    assert load_config(config_path).session_poll_seconds == 0


def test_channel_rx_guard_seconds_from_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "zorkbot.toml"
    config_path.write_text("channel_rx_guard_seconds = 3.5\n")
    assert load_config(config_path).channel_rx_guard_seconds == 3.5


def test_channel_rx_guard_seconds_can_be_disabled(tmp_path: Path) -> None:
    """0 has to mean "off", not "unset" - it is how the guard is turned off."""
    config_path = tmp_path / "zorkbot.toml"
    config_path.write_text("channel_rx_guard_seconds = 0\n")
    assert load_config(config_path).channel_rx_guard_seconds == 0.0


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


def test_dm_ack_defaults() -> None:
    config = BotConfig()
    assert config.dm_ack_enabled is True
    assert config.dm_ack_max_attempts == 3
    assert config.dm_ack_max_flood_attempts == 2
    assert config.dm_ack_flood_after == 2
    assert config.dm_ack_timeout_seconds == 0.0


def test_dm_ack_config_from_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "zorkbot.toml"
    config_path.write_text(
        """
dm_ack_max_attempts = 1
dm_ack_max_flood_attempts = 1
dm_ack_flood_after = 3
dm_ack_timeout_seconds = 6.5
""".strip()
    )
    config = load_config(config_path)
    assert config.dm_ack_max_attempts == 1
    assert config.dm_ack_max_flood_attempts == 1
    assert config.dm_ack_flood_after == 3
    assert config.dm_ack_timeout_seconds == 6.5


def test_dm_ack_can_be_disabled(tmp_path: Path) -> None:
    """false has to mean "off", not "unset"."""
    config_path = tmp_path / "zorkbot.toml"
    config_path.write_text("dm_ack_enabled = false\n")
    assert load_config(config_path).dm_ack_enabled is False
