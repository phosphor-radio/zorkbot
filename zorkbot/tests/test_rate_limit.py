"""Tests for per-sender rate limiting."""

import pytest

from zorkbot.rate_limit import RateLimiter


def test_rate_limiter_allows_first_command() -> None:
    limiter = RateLimiter(3.0)
    assert limiter.allow("aabbccddeeff")


def test_rate_limiter_blocks_rapid_commands() -> None:
    limiter = RateLimiter(3.0)
    assert limiter.allow("aabbccddeeff")
    assert not limiter.allow("aabbccddeeff")


def test_rate_limiter_tracks_senders_independently() -> None:
    limiter = RateLimiter(3.0)
    assert limiter.allow("aabbccddeeff")
    assert limiter.allow("112233445566")


def test_rate_limiter_disabled_when_zero() -> None:
    limiter = RateLimiter(0)
    assert limiter.allow("aabbccddeeff")
    assert limiter.allow("aabbccddeeff")


def test_rate_limiter_exempt_bypasses_limit() -> None:
    limiter = RateLimiter(3.0)
    assert limiter.allow("aabbccddeeff")
    assert limiter.allow("aabbccddeeff", exempt=True)


def test_rate_limiter_allows_none_key() -> None:
    limiter = RateLimiter(3.0)
    assert limiter.allow(None)
