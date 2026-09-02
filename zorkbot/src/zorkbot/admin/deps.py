"""FastAPI dependencies shared across admin API routes."""

from __future__ import annotations

from fastapi import HTTPException, Request

from zorkbot.admin.auth import SCOPE_ADMIN
from zorkbot.admin.context import AdminContext

_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": "Bearer"}


def get_ctx(request: Request) -> AdminContext:
    return request.app.state.ctx


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid_request", "error_description": "missing bearer token"},
            headers=_UNAUTHORIZED_HEADERS,
        )
    token = header[len("bearer "):].strip()
    if not token:
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid_request", "error_description": "missing bearer token"},
            headers=_UNAUTHORIZED_HEADERS,
        )
    return token


def require_scope(required: str = SCOPE_ADMIN):
    """Dependency factory: verifies the bearer token and its scope.

    `required="admin"` rejects a still-must-change-password token with
    403 insufficient_scope. Pass a lesser scope (e.g. password:change) for
    endpoints reachable during the forced first-login flow.
    """

    def _dependency(request: Request) -> dict:
        ctx = get_ctx(request)
        token = _bearer_token(request)
        entry = ctx.auth.verify_access_token(token)
        if entry is None:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "invalid_token",
                    "error_description": "access token is missing, expired, or unknown",
                },
                headers=_UNAUTHORIZED_HEADERS,
            )
        if required == SCOPE_ADMIN and entry["scope"] != SCOPE_ADMIN:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "insufficient_scope",
                    "error_description": "password change required",
                },
            )
        return entry

    return _dependency
