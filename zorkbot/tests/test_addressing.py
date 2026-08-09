"""Tests for mention and !zork parsing."""

import pytest

from zorkbot.addressing import parse_zork_command, strip_address


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("@[zorkbot] !zork look", ("!zork look", True)),
        ("@zorkbot !zork look", ("!zork look", True)),
        ("zorkbot !zork look", ("!zork look", True)),
        ("!zork look", ("!zork look", False)),
    ],
)
def test_strip_address(text: str, expected: tuple[str, bool]) -> None:
    assert strip_address(text, "zorkbot") == expected


def test_strip_address_rejects_partial_name_match() -> None:
    text, addressed = strip_address("zorkbotanist !zork look", "zorkbot")
    assert addressed is False
    assert text == "zorkbotanist !zork look"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("!zork", ""),
        ("!zork look", "look"),
        ("!zork take lamp", "take lamp"),
        ("!help", None),
        ("!zorkathon", None),
    ],
)
def test_parse_zork_command(text: str, expected: str | None) -> None:
    assert parse_zork_command(text) == expected
