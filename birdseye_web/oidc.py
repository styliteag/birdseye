"""OIDC authorization code + PKCE against NetBird's embedded IdP.

We log in as the public `netbird-dashboard` client: the management API only
accepts that audience (and `netbird-cli`), so a token from any other client
would be rejected. The access token is never validated here; NetBird does
that on every API call we forward it to.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx


class OIDCError(Exception):
    pass


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float


@dataclass(frozen=True)
class Endpoints:
    authorization: str
    token: str
    end_session: str = ""


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


class OIDCClient:
    def __init__(
        self,
        issuer: str,
        client_id: str,
        redirect_uri: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._issuer = issuer
        self._client_id = client_id
        self._redirect_uri = redirect_uri
        self._transport = transport
        self._timeout = timeout
        self._endpoints: Endpoints | None = None

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=self._timeout)

    async def endpoints(self) -> Endpoints:
        if self._endpoints is None:
            async with self._http() as http:
                resp = await http.get(f"{self._issuer}/.well-known/openid-configuration")
            if resp.status_code != 200:
                raise OIDCError(f"discovery failed: HTTP {resp.status_code}")
            d = resp.json()
            self._endpoints = Endpoints(
                authorization=d["authorization_endpoint"],
                token=d["token_endpoint"],
                end_session=d.get("end_session_endpoint", ""),
            )
        return self._endpoints

    async def authorization_url(self, state: str, challenge: str) -> str:
        ep = await self.endpoints()
        query = urlencode(
            {
                "client_id": self._client_id,
                "response_type": "code",
                "redirect_uri": self._redirect_uri,
                "scope": "openid profile email offline_access",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{ep.authorization}?{query}"

    async def _token(self, form: dict[str, str]) -> Tokens:
        ep = await self.endpoints()
        try:
            async with self._http() as http:
                resp = await http.post(ep.token, data={**form, "client_id": self._client_id})
        except httpx.HTTPError as exc:
            raise OIDCError(f"token endpoint unreachable: {exc.__class__.__name__}") from exc
        if resp.status_code != 200:
            raise OIDCError(f"token endpoint: HTTP {resp.status_code}")
        body: dict[str, Any] = resp.json()
        if not body.get("access_token"):
            raise OIDCError("token endpoint returned no access_token")
        return Tokens(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token", ""),
            expires_at=time.time() + float(body.get("expires_in", 3600)),
        )

    async def exchange(self, code: str, verifier: str) -> Tokens:
        return await self._token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._redirect_uri,
                "code_verifier": verifier,
            }
        )

    async def refresh(self, refresh_token: str) -> Tokens:
        return await self._token({"grant_type": "refresh_token", "refresh_token": refresh_token})
