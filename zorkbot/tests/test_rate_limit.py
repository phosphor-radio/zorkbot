"""Tests for per-sender rate limiting."""

import time

from zorkbot.rate_limit import RateLimiter


def test_rate_limiter_allows_first_command() -> None:
    limiter = RateLimiter(3.0)
    assert limiter.check("aabbccddeeff").allowed


def test_rate_limiter_blocks_rapid_commands() -> None:
    limiter = RateLimiter(3.0)
    assert limiter.check("aabbccddeeff").allowed
    assert not limiter.check("aabbccddeeff").allowed


def test_rate_limiter_tracks_senders_independently() -> None:
    limiter = RateLimiter(3.0)
    assert limiter.check("aabbccddeeff").allowed
    assert limiter.check("112233445566").allowed


def test_rate_limiter_disabled_when_zero() -> None:
    limiter = RateLimiter(0)
    assert limiter.check("aabbccddeeff").allowed
    assert limiter.check("aabbccddeeff").allowed


def test_rate_limiter_exempt_bypasses_limit() -> None:
    limiter = RateLimiter(3.0)
    assert limiter.check("aabbccddeeff").allowed
    assert limiter.check("aabbccddeeff", exempt=True).allowed


def test_rate_limiter_allows_none_key() -> None:
    limiter = RateLimiter(3.0)
    assert limiter.check(None).allowed


# --- Burst behaviour: one notice per episode, not one per message ------------


def test_burst_notifies_only_once() -> None:
    """A 10-message burst must cost one reply packet, not ten.

    Every reply occupies a send_spacing_seconds transmit slot, so replying to
    each message would let one sender monopolise the radio.
    """
    limiter = RateLimiter(3.0)
    assert limiter.check("aabbccddeeff").allowed

    decisions = [limiter.check("aabbccddeeff") for _ in range(10)]
    assert not any(d.allowed for d in decisions)
    assert sum(d.notify for d in decisions) == 1
    assert decisions[0].notify


def test_allowed_command_carries_no_notice() -> None:
    limiter = RateLimiter(3.0)
    assert not limiter.check("aabbccddeeff").notify


def test_notice_rearms_after_sender_complies() -> None:
    """Complying ends the episode, so a later burst is announced again."""
    limiter = RateLimiter(0.05)
    limiter.check("aabbccddeeff")
    assert limiter.check("aabbccddeeff").notify is True
    assert limiter.check("aabbccddeeff").notify is False

    time.sleep(0.06)  # sender waits the interval out
    assert limiter.check("aabbccddeeff").allowed

    assert limiter.check("aabbccddeeff").notify is True


def test_burst_notices_are_per_sender() -> None:
    limiter = RateLimiter(3.0)
    limiter.check("aabbccddeeff")
    limiter.check("112233445566")

    assert limiter.check("aabbccddeeff").notify is True
    assert limiter.check("112233445566").notify is True
    assert limiter.check("aabbccddeeff").notify is False


def test_exempt_sender_never_notified() -> None:
    limiter = RateLimiter(3.0)
    limiter.check("aabbccddeeff")
    for _ in range(5):
        d = limiter.check("aabbccddeeff", exempt=True)
        assert d.allowed and not d.notify
