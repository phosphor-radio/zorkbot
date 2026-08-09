"""Bot mention and command parsing."""

from __future__ import annotations

COMMAND = "!zork"
HELP_COMMANDS = frozenset({"!help", "!commands"})


def strip_address(text: str, bot_name: str) -> tuple[str, bool]:
    """Return (remaining text, whether the bot was addressed by name)."""
    text = text.strip()
    name = bot_name.lower()
    mention = f"@[{name}]"
    if text.lower().startswith(mention):
        return text[len(mention) :].lstrip(" :,"), True

    body = text[1:] if text.startswith("@") else text
    if body.lower().startswith(name):
        rest = body[len(bot_name) :]
        if not rest or rest[0] in " :,":
            return rest.lstrip(" :,"), True
    return text, False


def parse_zork_command(text: str) -> str | None:
    """Return argument string after !zork, or None if not a zork command."""
    text = text.strip()
    if text == COMMAND:
        return ""
    if text.startswith(f"{COMMAND} "):
        return text[len(COMMAND) :].strip()
    return None


def parse_command(text: str) -> str | None:
    """Return handler args for a recognized bot command, or None."""
    text = text.strip()
    if text.lower() in HELP_COMMANDS:
        return "help"
    return parse_zork_command(text)
