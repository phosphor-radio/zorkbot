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
class SessionInfo:
    num: int
    player_id: str
    started_at: str
    last_command_at: str = ""


class GameServiceError(Exception):
    """Raised when the game service returns an unexpected error."""


class SessionFullError(GameServiceError):
    """Raised when the session pool is at capacity."""


class SessionNotFoundError(GameServiceError):
    """Raised when no active session exists for a player."""


class GameClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
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

    async def start_session(self, player_id: str) -> None:
        """Start or restore a session for player_id."""
        response = await self._request(
            "POST",
            "/sessions",
            json={"player_id": player_id},
        )
        payload = response.json()
        if not payload.get("ok", False):
            error = payload.get("error", "start failed")
            if response.status_code == 503:
                raise SessionFullError(error)
            raise GameServiceError(error)

    async def end_session(self, player_id: str) -> None:
        """Save and end the session for player_id."""
        response = await self._request("DELETE", f"/sessions/{player_id}")
        payload = response.json()
        if not payload.get("ok", False):
            raise GameServiceError(payload.get("error", "end failed"))

    async def reset_session(self, player_id: str) -> None:
        """Wipe save and restart a fresh session for player_id."""
        response = await self._request(
            "DELETE",
            f"/sessions/{player_id}/save",
        )
        payload = response.json()
        if not payload.get("ok", False):
            raise GameServiceError(payload.get("error", "reset failed"))

    async def list_sessions(self) -> list[SessionInfo]:
        """Return all active sessions."""
        response = await self._request("GET", "/sessions")
        payload = response.json()
        return [
            SessionInfo(
                num=s["num"],
                player_id=s["player_id"],
                started_at=s["started_at"],
                last_command_at=s.get("last_command_at", ""),
            )
            for s in payload.get("sessions", [])
        ]

    async def command(self, player_id: str, text: str) -> CommandResult:
        """Send a game command for player_id."""
        response = await self._request(
            "POST",
            f"/sessions/{player_id}/command",
            json={"text": text},
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
            if exc.response.status_code == 503:
                raise SessionFullError(message) from exc
            if exc.response.status_code == 404:
                raise SessionNotFoundError(message) from exc
            raise GameServiceError(message) from exc
        finally:
            if self._client is None:
                await client.aclose()
