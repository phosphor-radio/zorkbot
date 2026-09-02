"""RFC 6749 /token, /token/revoke, plus /me and the forced password change."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response

from zorkbot.admin.auth import SCOPE_ADMIN, SCOPE_PASSWORD_CHANGE, AuthError
from zorkbot.admin.deps import get_ctx, require_scope

router = APIRouter(tags=["auth"])


def _raise(exc: AuthError) -> None:
    raise HTTPException(
        status_code=exc.status, detail={"error": exc.error, "error_description": exc.description}
    )


@router.post("/token")
async def token(
    request: Request,
    grant_type: str = Form(...),
    username: str | None = Form(None),
    password: str | None = Form(None),
    refresh_token: str | None = Form(None),
) -> dict:
    ctx = get_ctx(request)
    source_ip = request.client.host if request.client else "unknown"

    if grant_type == "password":
        if not username or not password:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "invalid_request",
                    "error_description": "username and password are required",
                },
            )
        try:
            result = await ctx.auth.password_grant(username, password, source_ip)
        except AuthError as exc:
            _raise(exc)
    elif grant_type == "refresh_token":
        if not refresh_token:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_request", "error_description": "refresh_token is required"},
            )
        try:
            result = await ctx.auth.refresh_grant(refresh_token)
        except AuthError as exc:
            _raise(exc)
    else:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unsupported_grant_type",
                "error_description": f"unsupported grant_type: {grant_type!r}",
            },
        )

    return {
        "access_token": result.access_token,
        "token_type": "bearer",
        "expires_in": result.expires_in,
        "refresh_token": result.refresh_token,
        "scope": result.scope,
        "must_change_password": result.must_change_password,
    }


@router.post("/token/revoke", status_code=204)
async def revoke(
    request: Request,
    refresh_token: str = Form(...),
    _entry: dict = Depends(require_scope(SCOPE_ADMIN)),
) -> Response:
    ctx = get_ctx(request)
    await ctx.auth.revoke_refresh_token(refresh_token)
    return Response(status_code=204)


@router.get("/me")
async def me(request: Request, _entry: dict = Depends(require_scope(SCOPE_PASSWORD_CHANGE))) -> dict:
    ctx = get_ctx(request)
    return await ctx.auth.get_user_public()


@router.post("/auth/password", status_code=204)
async def change_password(
    request: Request,
    body: dict,
    _entry: dict = Depends(require_scope(SCOPE_PASSWORD_CHANGE)),
) -> Response:
    current_password = body.get("current_password")
    new_password = body.get("new_password")
    if not current_password or not new_password:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_request",
                "error_description": "current_password and new_password are required",
            },
        )
    ctx = get_ctx(request)
    try:
        await ctx.auth.change_password(current_password, new_password)
    except AuthError as exc:
        _raise(exc)
    return Response(status_code=204)
