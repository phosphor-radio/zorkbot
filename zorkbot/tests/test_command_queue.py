"""Tests for serial command queue."""

import pytest

from zorkbot.command_queue import CommandQueue, QueuedCommand
from zorkbot.config import BotConfig
from zorkbot.context import Context, IncomingMessage


def _context(sender: str = "player") -> Context:
    replies: list[str] = []

    async def reply(text: str) -> None:
        replies.append(text)

    return Context(
        message=IncomingMessage(
            text="!zork look",
            sender_name=sender,
            channel_idx=1,
        ),
        args="look",
        _reply=reply,
        config=BotConfig(),
    )


def test_command_queue_accepts_until_full() -> None:
    queue = CommandQueue(max_size=2)
    assert queue.try_enqueue(QueuedCommand(_context("a")))
    assert queue.try_enqueue(QueuedCommand(_context("b")))
    assert not queue.try_enqueue(QueuedCommand(_context("c")))


@pytest.mark.asyncio
async def test_command_queue_join_waits_for_worker() -> None:
    queue = CommandQueue(max_size=2)
    queue.try_enqueue(QueuedCommand(_context()))
    command = await queue.dequeue()
    queue.mark_done()
    await queue.join()

    assert command.ctx.args == "look"
