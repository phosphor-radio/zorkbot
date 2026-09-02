"""OAuth2-shaped authentication for the single admin account.

Resource Owner Password Credentials grant (RFC 6749 §4.3) plus refresh-token
rotation (§6). Access tokens are opaque and held in memory only; refresh
tokens are opaque and persisted (hashed) in SQLite so they survive a bot
restart and can be revoked. Passwords are hashed with `hashlib.scrypt`
(stdlib — no extra dependency), parameters stored per-row as JSON so they
can be raised later without invalidating existing hashes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass

from zorkbot.admin.store import Store

logger = logging.getLogger(__name__)

DEFAULT_PASSWORD = "zorkbot-admin!"
MIN_PASSWORD_LENGTH = 12

_DEFAULT_KDF_PARAMS = {"n": 16384, "r": 8, "p": 1, "dklen": 32}

SCOPE_ADMIN = "admin"
SCOPE_PASSWORD_CHANGE = "password:change"

_THROTTLE_THRESHOLD = 5
_THROTTLE_BASE_SECONDS = 60
_THROTTLE_MAX_SECONDS = 900


class AuthError(Exception):
    def __init__(self, error: str, description: str, status: int = 400) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status


@dataclass
class TokenResult:
    access_token: str
    refresh_token: str
    expires_in: int
    scope: str
    must_change_password: bool


def _hash_password(password: str, salt: bytes, params: dict) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=params["n"],
        r=params["r"],
        p=params["p"],
        dklen=params["dklen"],
    )


def _sha256(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


class _LoginThrottle:
    """Per-source-IP + global exponential backoff on failed password grants."""

    def __init__(self) -> None:
        self._by_ip: dict[str, dict] = {}
        self._global: dict = {"failures": 0, "locked_until": 0.0}

    def _locked_for(self, state: dict) -> float:
        return max(0.0, state["locked_until"] - time.monotonic())

    def check(self, source_ip: str) -> None:
        for state in (self._by_ip.get(source_ip), self._global):
            if state and self._locked_for(state) > 0:
                raise AuthError(
                    "invalid_grant", "invalid username or password", status=400
                )

    def record_failure(self, source_ip: str) -> None:
        for bucket in (self._by_ip.setdefault(source_ip, {"failures": 0, "locked_until": 0.0}),
                       self._global):
            bucket["failures"] += 1
            if bucket["failures"] >= _THROTTLE_THRESHOLD:
                backoff = min(
                    _THROTTLE_BASE_SECONDS * (2 ** (bucket["failures"] - _THROTTLE_THRESHOLD)),
                    _THROTTLE_MAX_SECONDS,
                )
                bucket["locked_until"] = time.monotonic() + backoff
        logger.warning("admin-ui: failed login from %s", source_ip)

    def record_success(self, source_ip: str) -> None:
        self._by_ip.pop(source_ip, None)
        self._global["failures"] = 0
        self._global["locked_until"] = 0.0


class AuthService:
    def __init__(
        self,
        store: Store,
        *,
        access_token_ttl_seconds: int = 1800,
        refresh_token_ttl_seconds: int = 2592000,
    ) -> None:
        self._store = store
        self._access_ttl = access_token_ttl_seconds
        self._refresh_ttl = refresh_token_ttl_seconds
        self._throttle = _LoginThrottle()
        # sha256(token) -> {"expires_at": float, "scope": str}
        self._access_tokens: dict[bytes, dict] = {}

    async def ensure_admin_user(self) -> None:
        row = await self._store.query_one("SELECT id FROM admin_user WHERE id = 1")
        if row is not None:
            return
        salt = secrets.token_bytes(16)
        params = dict(_DEFAULT_KDF_PARAMS)
        pw_hash = _hash_password(DEFAULT_PASSWORD, salt, params)
        await self._store.run(
            "INSERT INTO admin_user"
            "(id, username, password_hash, password_salt, kdf, kdf_params,"
            " must_change_password, password_changed_at, created_at)"
            " VALUES (1, 'admin', ?, ?, 'scrypt', ?, 1, NULL, ?)",
            (pw_hash, salt, json.dumps(params), int(time.time())),
        )
        logger.warning(
            "admin-ui: no admin password set — seeded the default password; "
            "change it at first login"
        )

    async def _get_user(self) -> dict:
        row = await self._store.query_one("SELECT * FROM admin_user WHERE id = 1")
        assert row is not None, "admin_user not bootstrapped"
        return dict(row)

    async def get_user_public(self) -> dict:
        user = await self._get_user()
        return {
            "username": user["username"],
            "must_change_password": bool(user["must_change_password"]),
            "password_changed_at": user["password_changed_at"],
        }

    def _scope_for(self, user: dict) -> str:
        return SCOPE_PASSWORD_CHANGE if user["must_change_password"] else SCOPE_ADMIN

    async def _issue_tokens(self, user: dict) -> TokenResult:
        scope = self._scope_for(user)
        access_token = secrets.token_urlsafe(32)
        self._access_tokens[_sha256(access_token)] = {
            "expires_at": time.time() + self._access_ttl,
            "scope": scope,
        }
        refresh_token = secrets.token_urlsafe(32)
        now = int(time.time())
        await self._store.run(
            "INSERT INTO refresh_tokens(token_hash, issued_at, expires_at) VALUES (?, ?, ?)",
            (_sha256(refresh_token), now, now + self._refresh_ttl),
        )
        return TokenResult(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=self._access_ttl,
            scope=scope,
            must_change_password=bool(user["must_change_password"]),
        )

    async def password_grant(self, username: str, password: str, source_ip: str) -> TokenResult:
        self._throttle.check(source_ip)
        user = await self._get_user()
        ok = username == user["username"] and hmac.compare_digest(
            _hash_password(password, user["password_salt"], json.loads(user["kdf_params"])),
            user["password_hash"],
        )
        if not ok:
            self._throttle.record_failure(source_ip)
            raise AuthError("invalid_grant", "invalid username or password")
        self._throttle.record_success(source_ip)
        return await self._issue_tokens(user)

    async def refresh_grant(self, refresh_token: str) -> TokenResult:
        token_hash = _sha256(refresh_token)
        row = await self._store.query_one(
            "SELECT * FROM refresh_tokens WHERE token_hash = ?", (token_hash,)
        )
        if row is None:
            raise AuthError("invalid_grant", "unknown refresh token")
        now = int(time.time())
        if row["revoked_at"] is not None:
            # Reuse of an already-rotated/revoked token: treat as theft and
            # revoke the whole chain plus every live access token.
            logger.warning("admin-ui: refresh token reuse detected — revoking all sessions")
            await self._revoke_all()
            raise AuthError("invalid_grant", "refresh token already used")
        if row["expires_at"] < now:
            raise AuthError("invalid_grant", "refresh token expired")

        user = await self._get_user()
        result = await self._issue_tokens(user)
        new_hash = _sha256(result.refresh_token)
        await self._store.run(
            "UPDATE refresh_tokens SET revoked_at = ?, replaced_by = ? WHERE token_hash = ?",
            (now, new_hash, token_hash),
        )
        return result

    async def _revoke_all(self) -> None:
        now = int(time.time())
        await self._store.run(
            "UPDATE refresh_tokens SET revoked_at = ? WHERE revoked_at IS NULL", (now,)
        )
        self._access_tokens.clear()

    async def revoke_refresh_token(self, refresh_token: str) -> None:
        await self._store.run(
            "UPDATE refresh_tokens SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
            (int(time.time()), _sha256(refresh_token)),
        )

    def verify_access_token(self, access_token: str) -> dict | None:
        entry = self._access_tokens.get(_sha256(access_token))
        if entry is None:
            return None
        if entry["expires_at"] < time.time():
            self._access_tokens.pop(_sha256(access_token), None)
            return None
        return entry

    async def change_password(self, current_password: str, new_password: str) -> None:
        user = await self._get_user()
        current_ok = hmac.compare_digest(
            _hash_password(current_password, user["password_salt"], json.loads(user["kdf_params"])),
            user["password_hash"],
        )
        if not current_ok:
            raise AuthError("invalid_grant", "current password is incorrect", status=400)
        if len(new_password) < MIN_PASSWORD_LENGTH:
            raise AuthError(
                "invalid_request",
                f"new password must be at least {MIN_PASSWORD_LENGTH} characters",
                status=400,
            )
        if new_password in (current_password, DEFAULT_PASSWORD):
            raise AuthError(
                "invalid_request", "new password must differ from the current one", status=400
            )

        salt = secrets.token_bytes(16)
        params = dict(_DEFAULT_KDF_PARAMS)
        new_hash = _hash_password(new_password, salt, params)
        await self._store.run(
            "UPDATE admin_user SET password_hash = ?, password_salt = ?, kdf_params = ?,"
            " must_change_password = 0, password_changed_at = ? WHERE id = 1",
            (new_hash, salt, json.dumps(params), int(time.time())),
        )
        await self._revoke_all()
        logger.info("admin-ui: admin password changed")
