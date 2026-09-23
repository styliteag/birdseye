"""In-memory NetBird + OIDC fakes for route tests."""

from __future__ import annotations

import copy
import time
from typing import Any

from birdseye_web.config import Settings
from birdseye_web.nbapi import NetBirdError
from birdseye_web.oidc import Endpoints, Tokens
from tests.factory import group, peer, policy, rule

SETTINGS = Settings(
    nb_url="https://nb.test",
    base_url="http://testserver",
    session_secret="x" * 40,
    oidc_issuer="https://nb.test/oauth2",
)

ME = {
    "id": "u1",
    "name": "Alice",
    "role": "admin",
    "permissions": {
        "modules": {
            "groups": {"create": True, "update": True},
            "policies": {"create": True, "update": True},
        }
    },
}


class FakeNetBird:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {
            "users/current": ME,
            "users": [{"id": "u1", "name": "Alice", "email": "a@x", "role": "admin"}],
            "peers": [peer("a1", ["A"], user="u1"), peer("b1", ["B"])],
            "groups": [
                group("ALL", "All", peers=["a1", "b1"]),
                group("A", "Admins", peers=["a1"]),
                {
                    **group("B", "Servers", peers=["b1"]),
                    "resources": [{"id": "r1", "type": "host"}],
                },
            ],
            "policies": [policy("p1", rule(["A"], ["B"], protocol="tcp", ports=["22"]))],
            "posture-checks": [],
            "networks": [],
        }
        self.writes: list[tuple[str, str, Any]] = []
        self.tokens: list[str] = []

    def api(self, token: str) -> FakeAPI:
        self.tokens.append(token)
        return FakeAPI(self)


class FakeAPI:
    def __init__(self, nb: FakeNetBird) -> None:
        self.nb = nb

    async def aclose(self) -> None:
        pass

    async def get(self, path: str) -> Any:
        if path in self.nb.data:
            return copy.deepcopy(self.nb.data[path])
        kind, _, oid = path.partition("/")
        for item in self.nb.data.get(kind, []):
            if item["id"] == oid:
                return copy.deepcopy(item)
        raise NetBirdError(404, "not found", path)

    async def post(self, path: str, data: Any) -> Any:
        self.nb.writes.append(("POST", path, data))
        new = {**data, "id": f"new-{len(self.nb.writes)}"}
        self.nb.data[path] = [*self.nb.data[path], new]
        return new

    async def put(self, path: str, data: Any) -> Any:
        self.nb.writes.append(("PUT", path, data))
        return data

    async def delete(self, path: str) -> Any:
        self.nb.writes.append(("DELETE", path, None))
        return None


class FakeOIDC:
    def __init__(self) -> None:
        self.last_state = ""

    async def authorization_url(self, state: str, challenge: str) -> str:
        self.last_state = state
        return f"https://nb.test/oauth2/auth?state={state}"

    async def exchange(self, code: str, verifier: str) -> Tokens:
        assert code == "good-code" and verifier
        return Tokens("access-jwt", "refresh", time.time() + 3600)

    async def endpoints(self) -> Endpoints:
        return Endpoints(
            "https://nb.test/oauth2/auth",
            "https://nb.test/oauth2/token",
            "https://nb.test/oauth2/logout",
        )

    async def refresh(self, refresh_token: str) -> Tokens:
        return Tokens("access-jwt-2", "refresh", time.time() + 3600)
