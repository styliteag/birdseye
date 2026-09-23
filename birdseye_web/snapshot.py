"""Load a `Snapshot` through the user's API client, with a short per-user cache.

Each user gets their own cache entry: NetBird may show a restricted user
less than an admin, so snapshots must never be shared across users.
Every write path calls `invalidate()` so the next page shows the change.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from birdseye_web.models import Snapshot, build_snapshot
from birdseye_web.nbapi import NetBirdAPI, NetBirdError


async def _get_or_empty(api: NetBirdAPI, path: str) -> list[Any]:
    """Missing permission on one module should not hide everything else."""
    try:
        return list(await api.get(path) or [])
    except NetBirdError as exc:
        if exc.forbidden or exc.status == 404:
            return []
        raise


async def load_snapshot(api: NetBirdAPI) -> Snapshot:
    peers, groups, users, policies, checks, networks = await asyncio.gather(
        _get_or_empty(api, "peers"),
        _get_or_empty(api, "groups"),
        _get_or_empty(api, "users"),
        _get_or_empty(api, "policies"),
        _get_or_empty(api, "posture-checks"),
        _get_or_empty(api, "networks"),
    )
    parts = await asyncio.gather(
        *(
            asyncio.gather(
                _get_or_empty(api, f"networks/{n['id']}/resources"),
                _get_or_empty(api, f"networks/{n['id']}/routers"),
            )
            for n in networks
        )
    )
    return build_snapshot(
        peers=peers,
        groups=groups,
        users=users,
        policies=policies,
        posture_checks=checks,
        networks=[(n, res, rt) for n, (res, rt) in zip(networks, parts, strict=True)],
    )


@dataclass(frozen=True)
class _Entry:
    snap: Snapshot
    at: float


class SnapshotCache:
    def __init__(self, ttl: float = 30.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl
        self._clock = clock
        self._entries: dict[str, _Entry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, user_key: str, loader: Callable[[], Awaitable[Snapshot]]) -> Snapshot:
        lock = self._locks.setdefault(user_key, asyncio.Lock())
        async with lock:
            entry = self._entries.get(user_key)
            if entry and self._clock() - entry.at < self._ttl:
                return entry.snap
            snap = await loader()
            self._entries = {**self._entries, user_key: _Entry(snap, self._clock())}
            return snap

    def invalidate(self, user_key: str | None = None) -> None:
        """Drop one user's entry, or all entries after a write (others see it too)."""
        if user_key is None:
            self._entries = {}
        else:
            self._entries = {k: v for k, v in self._entries.items() if k != user_key}
