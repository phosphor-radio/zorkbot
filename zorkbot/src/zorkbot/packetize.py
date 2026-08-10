"""Word-boundary packetizer for mesh radio replies.

Greedy word-boundary packing is inspired by ottobot's !help chunker
(https://github.com/tahnok/ottobot, MIT License, Copyright (c) Wesley Ellis).
"""

from __future__ import annotations

import re

DEFAULT_MAX_CHARS = 100

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_OSC_ESCAPE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")


def packetize(
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    prefix: str | None = None,
    numbered: bool = True,
) -> list[str]:
    """Split game output into mesh-sized packets."""
    normalized = _prepare_text(text)
    if not normalized:
        return []

    mention = prefix or ""
    chunks = _pack_with_sequence_budget(normalized, max_chars - len(mention), numbered)
    packets: list[str] = []
    total = len(chunks)

    for index, chunk in enumerate(chunks, start=1):
        body = chunk
        if numbered and total > 1:
            body = f"({index}/{total}) {chunk}"
        packets.append(f"{mention}{body}")

    return packets


def strip_ansi(text: str) -> str:
    text = _OSC_ESCAPE.sub("", text)
    return _ANSI_ESCAPE.sub("", text)


def _prepare_text(text: str) -> str:
    text = strip_ansi(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in text.split("\n")]

    collapsed: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if collapsed and not blank:
                collapsed.append("")
                blank = True
            continue
        blank = False
        collapsed.append(line)

    return " ".join(collapsed).strip()


def _sequence_prefix_len(total: int) -> int:
    return len(f"({total}/{total}) ")


def _pack_with_sequence_budget(text: str, limit: int, numbered: bool) -> list[str]:
    if limit <= 0:
        raise ValueError("max_chars must be greater than prefix length")

    reserve = _sequence_prefix_len(99) if numbered else 0
    chunks = _pack_words(text, max(1, limit - reserve))

    if not numbered or len(chunks) <= 1:
        return chunks

    while True:
        total = len(chunks)
        seq_len = _sequence_prefix_len(total)
        next_chunks = _pack_words(text, max(1, limit - seq_len))
        if len(next_chunks) == total:
            return next_chunks
        chunks = next_chunks


def _pack_words(text: str, limit: int) -> list[str]:
    words = text.split()
    if not words:
        return []

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for word in words:
        addition = len(word) if not current else len(word) + 1
        if current and current_len + addition > limit:
            chunks.append(" ".join(current))
            current = [word]
            current_len = len(word)
            continue
        current.append(word)
        current_len += addition

    if current:
        chunks.append(" ".join(current))

    return chunks
