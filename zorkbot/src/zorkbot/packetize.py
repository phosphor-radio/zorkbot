"""Word-boundary packetizer for mesh radio replies.

Greedy word-boundary packing is inspired by ottobot's !help chunker
(https://github.com/tahnok/ottobot, MIT License, Copyright (c) Wesley Ellis).
"""

from __future__ import annotations

import re
import string

DEFAULT_MAX_CHARS = 120

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_OSC_ESCAPE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")

# Room titles like "West of House" or "Behind the House": short lines in
# Title Case, save for minor connector words, with no sentence-ending
# period — unlike a one-line response such as "Taken."
_TITLE_MINOR_WORDS = frozenset({
    "a", "an", "and", "as", "at", "but", "by", "for", "from", "in",
    "nor", "of", "on", "or", "the", "to", "with",
})
# Zork's room names top out at a few words; capping length avoids misreading
# a long, period-less line (e.g. a truncated sentence) as a title.
_MAX_TITLE_WORDS = 6


def packetize(
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    prefix: str | None = None,
    first_line: str | None = None,
    numbered: bool = True,
) -> list[str]:
    """Split game output into mesh-sized packets.

    `first_line`, if given, is shown as its own line ahead of any detected
    title and the body — for context that isn't part of the game's own
    output, e.g. a watcher's "[Alice] > north" echo of the command that
    produced this text. Unlike `prefix` (repeated on every split packet),
    it appears once, only on the first packet, so a room title detected in
    `text` still gets its own line right after it rather than being folded
    into the echo line.
    """
    title, body = _prepare_text(text)
    if not title and not body and not first_line:
        return []

    mention = prefix or ""
    lead_lines = [line for line in (first_line, title) if line]
    lead = "\n".join(lead_lines)

    if not body:
        return [f"{mention}{lead}"] if lead else []

    # The lead lines (if any) ride along on packet 1 only, so their length
    # is budgeted out of every packet uniformly rather than tracking a
    # per-packet limit — a small, constant waste of space on packets 2+
    # given how short these lines are, in exchange for reusing the same
    # single-limit packing logic below unchanged.
    lead_prefix = f"{lead}\n" if lead else ""
    chunks = _pack_with_sequence_budget(
        body, max_chars - len(mention) - len(lead_prefix), numbered
    )
    packets: list[str] = []
    total = len(chunks)

    for index, chunk in enumerate(chunks, start=1):
        body_chunk = chunk
        if numbered and total > 1:
            body_chunk = f"({index}/{total}) {chunk}"
        this_lead = lead_prefix if index == 1 else ""
        packets.append(f"{mention}{this_lead}{body_chunk}")

    return packets


def strip_ansi(text: str) -> str:
    text = _OSC_ESCAPE.sub("", text)
    return _ANSI_ESCAPE.sub("", text)


def _prepare_text(text: str) -> tuple[str | None, str]:
    text = strip_ansi(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in text.split("\n")]

    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()

    if not lines:
        return None, ""

    title = None
    body_lines = lines
    if len(lines) > 1 and _looks_like_title(lines[0]):
        title = lines[0]
        body_lines = lines[1:]

    return title, _collapse_lines(body_lines)


def _looks_like_title(line: str) -> bool:
    if not line or line.endswith("."):
        return False
    words = line.split()
    if not words or len(words) > _MAX_TITLE_WORDS:
        return False
    for index, word in enumerate(words):
        core = word.strip(string.punctuation)
        if not core:
            return False
        if index == 0:
            if not core[0].isupper():
                return False
            continue
        if core.lower() in _TITLE_MINOR_WORDS:
            continue
        if not core[0].isupper():
            return False
    return True


def _collapse_lines(lines: list[str]) -> str:
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
