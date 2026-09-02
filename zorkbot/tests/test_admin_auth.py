"""Tests for the admin UI's OAuth2-shaped auth service."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from zorkbot.admin.auth import DEFAULT_PASSWORD, SCOPE_ADMIN, SCOPE_PASSWORD_CHANGE, AuthError, AuthService
from zorkbot.admin.store import Store


@pytest.fixture
async def auth():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "admin.db")
        await store.start()
        service = AuthService(store, access_token_ttl_seconds=1800, refresh_token_ttl_seconds=100)
        await service.ensure_admin_user()
        yield service
        store.close()


@pytest.mark.asyncio
async def test_default_password_scoped_to_password_change(auth) -> None:
    result = await auth.password_grant("admin", DEFAULT_PASSWORD, "1.2.3.4")
    assert result.scope == SCOPE_PASSWORD_CHANGE
    assert result.must_change_password is True


@pytest.mark.asyncio
async def test_wrong_password_rejected(auth) -> None:
    with pytest.raises(AuthError) as exc:
        await auth.password_grant("admin", "not the password", "1.2.3.4")
    assert exc.value.error == "invalid_grant"


@pytest.mark.asyncio
async def test_change_password_then_login(auth) -> None:
    await auth.password_grant("admin", DEFAULT_PASSWORD, "1.2.3.4")
    await auth.change_password(DEFAULT_PASSWORD, "a-brand-new-password")

    with pytest.raises(AuthError):
        await auth.password_grant("admin", DEFAULT_PASSWORD, "1.2.3.4")

    result = await auth.password_grant("admin", "a-brand-new-password", "1.2.3.4")
    assert result.scope == SCOPE_ADMIN
    assert result.must_change_password is False


@pytest.mark.asyncio
async def test_change_password_rejects_short_password(auth) -> None:
    with pytest.raises(AuthError):
        await auth.change_password(DEFAULT_PASSWORD, "short")


@pytest.mark.asyncio
async def test_change_password_revokes_existing_tokens(auth) -> None:
    result = await auth.password_grant("admin", DEFAULT_PASSWORD, "1.2.3.4")
    assert auth.verify_access_token(result.access_token) is not None

    await auth.change_password(DEFAULT_PASSWORD, "a-brand-new-password")

    assert auth.verify_access_token(result.access_token) is None
    with pytest.raises(AuthError):
        await auth.refresh_grant(result.refresh_token)


@pytest.mark.asyncio
async def test_refresh_rotation(auth) -> None:
    result = await auth.password_grant("admin", DEFAULT_PASSWORD, "1.2.3.4")
    await auth.change_password(DEFAULT_PASSWORD, "a-brand-new-password")
    login = await auth.password_grant("admin", "a-brand-new-password", "1.2.3.4")

    refreshed = await auth.refresh_grant(login.refresh_token)
    assert refreshed.access_token != login.access_token
    assert refreshed.refresh_token != login.refresh_token


@pytest.mark.asyncio
async def test_refresh_reuse_revokes_chain(auth) -> None:
    await auth.change_password(DEFAULT_PASSWORD, "a-brand-new-password")
    login = await auth.password_grant("admin", "a-brand-new-password", "1.2.3.4")
    refreshed = await auth.refresh_grant(login.refresh_token)

    # Reusing the already-rotated token is treated as theft.
    with pytest.raises(AuthError):
        await auth.refresh_grant(login.refresh_token)

    # The whole chain — including the token issued by the rotation — is dead.
    with pytest.raises(AuthError):
        await auth.refresh_grant(refreshed.refresh_token)
    assert auth.verify_access_token(refreshed.access_token) is None


@pytest.mark.asyncio
async def test_login_throttle_locks_after_repeated_failures(auth) -> None:
    for _ in range(5):
        with pytest.raises(AuthError):
            await auth.password_grant("admin", "wrong", "9.9.9.9")

    # A correct password is still rejected while locked out.
    with pytest.raises(AuthError):
        await auth.password_grant("admin", DEFAULT_PASSWORD, "9.9.9.9")
