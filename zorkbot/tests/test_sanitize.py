import pytest

from zorkbot.sanitize import NotAllowedError, validate


@pytest.mark.parametrize(
    "command",
    ["look", "take lamp", "go north", "open mailbox"],
)
def test_validate_allows_normal_commands(command: str) -> None:
    validate(command)


def test_validate_blocks_empty() -> None:
    with pytest.raises(NotAllowedError):
        validate("")
    with pytest.raises(NotAllowedError):
        validate("   ")


@pytest.mark.parametrize(
    "command",
    ["$help", "$quit", "$undo", "$dump", "$teleport"],
)
def test_validate_blocks_dollar_commands(command: str) -> None:
    with pytest.raises(NotAllowedError):
        validate(command)


def test_validate_blocks_any_dollar_prefix() -> None:
    with pytest.raises(NotAllowedError):
        validate("$custom")


@pytest.mark.parametrize("command", ["save", "restore", "SAVE", "Restore"])
def test_validate_blocks_save_restore_for_non_admin(command: str) -> None:
    with pytest.raises(NotAllowedError):
        validate(command)


@pytest.mark.parametrize("command", ["save", "restore"])
def test_validate_allows_save_restore_for_admin(command: str) -> None:
    validate(command, admin=True)


def test_validate_blocks_long_input() -> None:
    with pytest.raises(NotAllowedError):
        validate("a" * 81)


def test_validate_blocks_newlines() -> None:
    with pytest.raises(NotAllowedError):
        validate("look\nnorth")


def test_validate_blocks_control_characters() -> None:
    with pytest.raises(NotAllowedError):
        validate("look\x00")
    with pytest.raises(NotAllowedError):
        validate("look\x1b[31m")
