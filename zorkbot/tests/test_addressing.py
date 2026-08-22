"""Tests for bot mention and command parsing."""

import pytest

from zorkbot.addressing import parse_command, parse_zork_command, strip_address


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("!zork", ""),
        ("!zork look", "look"),
        ("!zork take lamp", "take lamp"),
        ("!help", "help"),
        ("!commands", "help"),
        ("!author", "author"),
        ("!zorkathon", None),
    ],
)
def test_parse_command(text: str, expected: str | None) -> None:
    assert parse_command(text) == expected


def test_parse_zork_command_only() -> None:
    assert parse_zork_command("!help") is None
    assert parse_zork_command("!commands") is None
    assert parse_zork_command("!author") is None


@pytest.mark.parametrize(
    ("text", "expected_rest", "mentioned"),
    [
        ("@[zorkbot] !zork look", "!zork look", True),
        ("@zorkbot !help", "!help", True),
        ("zorkbot !commands", "!commands", True),
        ("!zork look", "!zork look", False),
    ],
)
def test_strip_address(text: str, expected_rest: str, mentioned: bool) -> None:
    rest, was_mentioned = strip_address(text, "zorkbot")
    assert rest == expected_rest
    assert was_mentioned is mentioned


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("@[zorkbot] !zork look", "look"),
        ("@[zorkbot] !help", "help"),
        ("@[zorkbot] !commands", "help"),
        ("@[zorkbot] !author", "author"),
        ("@zorkbot !zork", ""),
    ],
)
def test_parse_command_with_mention(text: str, expected: str) -> None:
    rest, _ = strip_address(text, "zorkbot")
    assert parse_command(rest) == expected
