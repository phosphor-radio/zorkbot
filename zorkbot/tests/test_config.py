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
names = ["alice", "bob"]
""".strip()
    )
    config = load_config(config_path)
    assert config.name == "testbot"
    assert config.game_url == "http://localhost:18080"
    assert config.channel.index == 2
    assert config.admin_names == frozenset({"alice", "bob"})
