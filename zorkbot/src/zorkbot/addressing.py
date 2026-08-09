"""!zork command parsing."""

from __future__ import annotations


def parse_zork_command(text: str) -> str | None:
    """Return argument string after !zork, or None if not a zork command."""
    text = text.strip()
    if text == "!zork":
        return ""
    if text.startswith("!zork "):
        return text[len("!zork") :].strip()
    return None
