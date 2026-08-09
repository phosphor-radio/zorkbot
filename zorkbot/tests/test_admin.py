import pytest

from zorkbot.commands.zork import is_admin
from zorkbot.config import AdminConfig, BotConfig


def test_is_admin_by_name() -> None:
    config = BotConfig(admin=AdminConfig(names=["Alice"]))
    assert is_admin("Alice", None, config)
    assert not is_admin("Bob", None, config)


def test_is_admin_by_token() -> None:
    config = BotConfig(admin_token="secret")
    assert is_admin(None, "secret", config)
    assert not is_admin(None, "wrong", config)
