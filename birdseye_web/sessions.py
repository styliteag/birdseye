"""Server-side session store. The browser only ever sees a random ID.

Tokens stay in process memory; a restart logs everyone out, which is fine
for an admin tool and keeps secrets off disk.

Pending logins (between /login and the IdP callback) are anonymous, so they
get a short TTL and a hard cap: an unauthenticated client must not be able
to grow memory without bound.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

PENDING_TTL = 600.0
MAX_PENDING = 1000


@dataclass(frozen=True)
class Session:
    sid: str
    created: float
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    # pending login
    state: str = ""
    verifier: str = ""
    next_url: str = "/"
    # logged in
    access_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0
    user_id: str = ""
    user_name: str = ""
    role: str = ""
    permissions: dict = field(default_factory=dict)

    @property
    def logged_in(self) -> bool:
        return bool(self.access_token)

    @property
    def is_admin(self) -> bool:
        return self.role in ("owner", "admin")

    def can(self, module: str, op: str) -> bool:
        """UI hint only; NetBird enforces the real permission on every call."""
        return bool(self.permissions.get("modules", {}).get(module, {}).get(op, False))


class SessionStore:
    def __init__(
        self,
        max_age: float,
        clock: Callable[[], float] = time.time,
        max_pending: int = MAX_PENDING,
    ) -> None:
        self._max_age = max_age
        self._clock = clock
        self._max_pending = max_pending
        self._items: dict[str, Session] = {}

    def _alive(self, s: Session, now: float) -> bool:
        ttl = self._max_age if s.logged_in else PENDING_TTL
        return now - s.created <= ttl

    def create(self) -> Session:
        self._purge()
        s = Session(sid=secrets.token_urlsafe(32), created=self._clock())
        self._items = {**self._items, s.sid: s}
        self._cap_pending()
        return s

    def get(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        s = self._items.get(sid)
        if s is None or not self._alive(s, self._clock()):
            return None
        return s

    def update(self, session: Session, **changes: object) -> Session:
        """Replace a live session; a dropped one (logout) stays dropped."""
        new = replace(session, **changes)  # type: ignore[arg-type]
        if new.sid in self._items:
            self._items = {**self._items, new.sid: new}
        return new

    def rotate(self, session: Session, **changes: object) -> Session:
        """New ID after login (session fixation), same data plus changes."""
        new = replace(
            session,
            sid=secrets.token_urlsafe(32),
            csrf=secrets.token_urlsafe(32),
            created=self._clock(),
            **changes,  # type: ignore[arg-type]
        )
        self._items = {k: v for k, v in self._items.items() if k != session.sid} | {new.sid: new}
        return new

    def drop(self, sid: str) -> None:
        self._items = {k: v for k, v in self._items.items() if k != sid}

    def pending_count(self) -> int:
        return sum(1 for s in self._items.values() if not s.logged_in)

    def _purge(self) -> None:
        now = self._clock()
        self._items = {k: v for k, v in self._items.items() if self._alive(v, now)}

    def _cap_pending(self) -> None:
        pending = sorted(
            (s for s in self._items.values() if not s.logged_in), key=lambda s: s.created
        )
        excess = {s.sid for s in pending[: max(0, len(pending) - self._max_pending)]}
        if excess:
            self._items = {k: v for k, v in self._items.items() if k not in excess}
