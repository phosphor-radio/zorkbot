"""Bot mention and command parsing.

strip_address() is adapted from ottobot's Ottobot.strip_address()
(https://github.com/tahnok/ottobot, MIT License, Copyright (c) Wesley Ellis).
"""

from __future__ import annotations

# All recognized bot commands (without the leading !).
BOT_COMMANDS = frozenset({
    "help", "commands", "author",
    "start", "end", "list", "watch", "watchers", "reset",
})

# Legacy !zork prefix — still supported for compatibility.
_ZORK_PREFIX = "!zork"


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


def parse_command(text: str) -> str | None:
    """Return handler args for a recognized bot command, or None.

    For a bare `!command` or `!command args`, returns `"command args"`.
    For the legacy `!zork <game text>` form, returns the game text.
    Returns None if the text is not a recognized command.
    """
    text = text.strip()
    lower = text.lower()

    # Legacy !zork form.
    if lower == _ZORK_PREFIX:
        return ""
    if lower.startswith(f"{_ZORK_PREFIX} "):
        return text[len(_ZORK_PREFIX) :].strip()

    # General !command form.
    if text.startswith("!"):
        parts = text[1:].split(None, 1)
        if parts:
            cmd = parts[0].lower()
            # Normalize aliases.
            if cmd == "commands":
                cmd = "help"
            if cmd in BOT_COMMANDS:
                rest = parts[1] if len(parts) > 1 else ""
                return f"{cmd} {rest}".strip() if rest else cmd

    return None


def parse_zork_command(text: str) -> str | None:
    """Return argument string after !zork, or None if not a zork command.

    Kept for compatibility.
    """
    text = text.strip()
    lower = text.lower()
    if lower == _ZORK_PREFIX:
        return ""
    if lower.startswith(f"{_ZORK_PREFIX} "):
        return text[len(_ZORK_PREFIX) :].strip()
    return None
