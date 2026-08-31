"""Mesh channel configuration for zorkbot.

Channel index gating follows the command-channel pattern used by ottobot
(https://github.com/tahnok/ottobot, MIT License, Copyright (c) Wesley Ellis).
"""

from __future__ import annotations

from dataclasses import dataclass

ZORK_CHANNEL_NAME = "#zork"
BOTS_CHANNEL_NAME = "#bots"


@dataclass(frozen=True)
class ChannelConfig:
    index: int
    name: str = ZORK_CHANNEL_NAME
    secret: bytes | None = None


def channel_matches(channel_idx: int, channel: ChannelConfig) -> bool:
    return channel_idx == channel.index
