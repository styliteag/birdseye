"""Thin async NetBird API client that acts as the logged-in user.

The `netbird` SDK hardcodes `Authorization: Token ...` (PATs). The web UI
forwards the user's OIDC access token instead, which needs `Bearer`, so
NetBird enforces that user's role and records them in the audit log.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class NetBirdError(Exception):
    def __init__(self, status: int, message: str, path: str = "") -> None:
        super().__init__(f"{status} {path}: {message}")
        self.status = status
        self.message = message
        self.path = path

    @property
    def unauthorized(self) -> bool:
        return self.status == 401

    @property
    def forbidden(self) -> bool:
        return self.status == 403


def _message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(body, dict):
        return str(body.get("message") or body.get("error") or body)[:300]
    return str(body)[:300]


class NetBirdAPI:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        scheme: str = "Bearer",
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/api/",
            headers={"Authorization": f"{scheme} {token}", "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    async def __aenter__(self) -> NetBirdAPI:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, data: Any = None) -> Any:
        resp = await self._client.request(method, path.lstrip("/"), json=data)
        if resp.status_code >= 400:
            raise NetBirdError(resp.status_code, _message(resp), path)
        if not resp.content:
            return None
        return resp.json()

    async def get(self, path: str) -> Any:
        return await self._request("GET", path)

    async def post(self, path: str, data: Any) -> Any:
        return await self._request("POST", path, data)

    async def put(self, path: str, data: Any) -> Any:
        return await self._request("PUT", path, data)

    async def delete(self, path: str) -> Any:
        return await self._request("DELETE", path)
