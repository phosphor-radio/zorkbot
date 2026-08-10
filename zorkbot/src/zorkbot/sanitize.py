"""Input sanitizer mirroring the Go game service rules."""

from __future__ import annotations

MAX_COMMAND_LENGTH = 80

BLOCKED_DEBUG_COMMANDS = frozenset({
    "$help", "$quit", "$undo", "$redo", "$dump",
    "$dict", "$tree", "$room", "$you", "$find",
    "$object", "$parent", "$attrs", "$props", "$simple",
    "$header", "$history", "$have_attr", "$have_prop",
    "$steal", "$teleport",
})


class NotAllowedError(ValueError):
    """Raised when a command must not be forwarded to encrusted."""

    def __init__(self, message: str = "that command isn't allowed") -> None:
        super().__init__(message)


def validate(text: str, *, admin: bool = False) -> None:
    """Raise NotAllowedError if text must not be forwarded to encrusted."""
    text = text.strip()
    if not text:
        raise NotAllowedError()
    if len(text) > MAX_COMMAND_LENGTH:
        raise NotAllowedError()
    if "\n" in text or "\r" in text:
        raise NotAllowedError()
    if text.startswith("$"):
        raise NotAllowedError()
    if text.lower() in BLOCKED_DEBUG_COMMANDS:
        raise NotAllowedError()
    if _has_control_or_ansi(text):
        raise NotAllowedError()
    lower = text.lower()
    if not admin and lower in {"save", "restore", "quit"}:
        raise NotAllowedError()


def _has_control_or_ansi(text: str) -> bool:
    for char in text:
        code = ord(char)
        if char == "\ufffd":
            continue
        if code < 0x20 or code == 0x7F:
            return True
        if char == "\x1b":
            return True
    return False
