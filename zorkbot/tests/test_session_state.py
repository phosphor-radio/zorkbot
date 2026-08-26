"""Tests for per-player session state."""

import pytest

from zorkbot.session_state import SessionState


def test_add_and_get_session() -> None:
    state = SessionState()
    record = state.add_session("aabbccddeeff", "Alice")
    assert record.num == 1
    assert record.player_id == "aabbccddeeff"
    assert record.player_name == "Alice"
    assert state.get_session("aabbccddeeff") is record
    assert state.get_session_by_num(1) is record


def test_session_numbers_increment() -> None:
    state = SessionState()
    r1 = state.add_session("aabbccddeeff", "Alice")
    r2 = state.add_session("112233445566", "Bob")
    assert r1.num == 1
    assert r2.num == 2


def test_remove_session() -> None:
    state = SessionState()
    state.add_session("aabbccddeeff", "Alice")
    removed = state.remove_session("aabbccddeeff")
    assert removed is not None
    assert state.get_session("aabbccddeeff") is None
    assert state.get_session_by_num(removed.num) is None


def test_remove_session_clears_watchers() -> None:
    state = SessionState(max_watchers_per_session=2)
    state.add_session("aabbccddeeff", "Alice")
    state.add_watcher("112233445566", 1)
    state.remove_session("aabbccddeeff")
    # Watcher should be evicted.
    assert state.watching_session("112233445566") is None


def test_active_state_playing() -> None:
    state = SessionState()
    state.add_session("aabbccddeeff", "Alice")
    assert state.active_state("aabbccddeeff") == "playing"


def test_active_state_none() -> None:
    state = SessionState()
    assert state.active_state("aabbccddeeff") == "none"


def test_active_state_watching() -> None:
    state = SessionState()
    state.add_session("aabbccddeeff", "Alice")
    state.add_watcher("112233445566", 1)
    assert state.active_state("112233445566") == "watching"


def test_add_watcher_succeeds() -> None:
    state = SessionState(max_watchers_per_session=2)
    state.add_session("aabbccddeeff", "Alice")
    error = state.add_watcher("112233445566", 1)
    assert error is None
    assert "112233445566" in state.watchers_for_session(1)


def test_add_watcher_nonexistent_session() -> None:
    state = SessionState()
    error = state.add_watcher("112233445566", 99)
    assert "not active" in error


def test_add_watcher_exceeds_cap() -> None:
    state = SessionState(max_watchers_per_session=1)
    state.add_session("aabbccddeeff", "Alice")
    state.add_watcher("112233445566", 1)
    error = state.add_watcher("334455667788", 1)
    assert "maximum" in error


def test_remove_watcher() -> None:
    state = SessionState()
    state.add_session("aabbccddeeff", "Alice")
    state.add_watcher("112233445566", 1)
    num = state.remove_watcher("112233445566")
    assert num == 1
    assert state.watching_session("112233445566") is None
    assert "112233445566" not in state.watchers_for_session(1)


def test_all_sessions_returns_sorted() -> None:
    state = SessionState()
    state.add_session("aabbccddeeff", "Alice")
    state.add_session("112233445566", "Bob")
    sessions = state.all_sessions()
    assert len(sessions) == 2


def test_all_watchers() -> None:
    state = SessionState()
    state.add_session("aabbccddeeff", "Alice")
    state.add_watcher("112233445566", 1)
    pairs = state.all_watchers()
    assert ("112233445566", 1) in pairs
