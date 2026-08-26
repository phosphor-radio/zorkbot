"""Tests for admin authorization via pubkey_prefix."""

import pytest

from zorkbot.config import AdminConfig, BotConfig
from zorkbot.context import Context, IncomingMessage


def _ctx(pubkey_prefix: str | None, config: BotConfig) -> Context:
    return Context(
        message=IncomingMessage(text="", pubkey_prefix=pubkey_prefix),
        args="",
        _reply=lambda _: None,
        config=config,
    )


def test_is_admin_by_pubkey() -> None:
    config = BotConfig(admin=AdminConfig(pubkeys=["aabbccddeeff"]))
    assert _ctx("aabbccddeeff", config).is_admin()
    assert not _ctx("112233445566", config).is_admin()


def test_is_admin_case_insensitive() -> None:
    config = BotConfig(admin=AdminConfig(pubkeys=["AABBCCDDEEFF"]))
    assert _ctx("aabbccddeeff", config).is_admin()


def test_is_admin_no_pubkey() -> None:
    config = BotConfig(admin=AdminConfig(pubkeys=["aabbccddeeff"]))
    assert not _ctx(None, config).is_admin()


def test_is_admin_empty_list() -> None:
    config = BotConfig()
    assert not _ctx("aabbccddeeff", config).is_admin()
