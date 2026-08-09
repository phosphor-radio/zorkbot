"""Tests for !zork command parsing."""

import pytest

from zorkbot.addressing import parse_zork_command


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("!zork", ""),
        ("!zork look", "look"),
        ("!zork take lamp", "take lamp"),
        ("!help", None),
        ("!zorkathon", None),
        ("@[zorkbot] !zork look", None),
    ],
)
def test_parse_zork_command(text: str, expected: str | None) -> None:
    assert parse_zork_command(text) == expected
