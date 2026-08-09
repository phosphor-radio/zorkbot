"""Async HTTP client for the zorkd game service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    output: str = ""
    error: str = ""


@dataclass(frozen=True)
class GameStatus:
    uptime: str
    busy: bool


class GameServiceError(Exception):
    """Raised when the game service returns an unexpected error."""


class GameClient:
    def __init__(
        self,
        base_url: str,
        *,
        admin_token: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.admin_token = admin_token
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> GameClient:
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def command(self, text: str, *, admin: bool = False) -> CommandResult:
        response = await self._request(
            "POST",
            "/command",
            json={"text": text, "admin": admin},
        )
        payload = response.json()
        return CommandResult(
            ok=payload.get("ok", False),
            output=payload.get("output", ""),
            error=payload.get("error", ""),
        )

    async def health(self) -> bool:
        response = await self._request("GET", "/health")
        return response.status_code == 200

    async def status(self) -> GameStatus:
        response = await self._request("GET", "/status")
        payload = response.json()
        return GameStatus(
            uptime=payload.get("uptime", "0s"),
            busy=bool(payload.get("busy", False)),
        )

    async def reset(self) -> None:
        if not self.admin_token:
            raise GameServiceError("admin token not configured")
        response = await self._request(
            "POST",
            "/reset",
            headers={"X-Admin-Token": self.admin_token},
        )
        payload: dict[str, Any] = response.json()
        if not payload.get("ok", False):
            raise GameServiceError(payload.get("error", "reset failed"))

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        client = self._client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
        )
        try:
            response = await client.request(method, path, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            if exc.response.headers.get("content-type", "").startswith("application/json"):
                payload = exc.response.json()
                message = payload.get("error", str(exc))
            else:
                message = str(exc)
            raise GameServiceError(message) from exc
        finally:
            if self._client is None:
                await client.aclose()
