"""Shared app state and request dependencies."""

from __future__ import annotations

import hmac
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates

from birdseye_web.config import Settings
from birdseye_web.models import Snapshot
from birdseye_web.nbapi import NetBirdAPI, NetBirdError
from birdseye_web.oidc import OIDCClient, OIDCError
from birdseye_web.sessions import Session, SessionStore
from birdseye_web.snapshot import SnapshotCache, load_snapshot
from birdseye_web.ui import register as register_filters

log = logging.getLogger(__name__)
audit = logging.getLogger("birdseye_web.audit")

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
register_filters(TEMPLATES.env)

ApiFactory = Callable[[str], NetBirdAPI]

# NetBird object IDs are xid strings. Anything else (`..`, `?`, `#`, `/`)
# must never reach an upstream API path.
OBJECT_ID = r"^[A-Za-z0-9_-]{1,64}$"


@dataclass(frozen=True)
class AppContext:
    settings: Settings
    store: SessionStore
    cache: SnapshotCache
    oidc: OIDCClient
    api_factory: ApiFactory


class LoginRequired(Exception):
    def __init__(self, next_url: str = "/") -> None:
        super().__init__(next_url)
        self.next_url = next_url


class CSRFError(Exception):
    pass


def ctx(request: Request) -> AppContext:
    return request.app.state.ctx


async def _refresh_if_needed(c: AppContext, s: Session) -> Session:
    if s.expires_at - time.time() > 60:
        return s
    if not s.refresh_token:
        raise LoginRequired()
    try:
        t = await c.oidc.refresh(s.refresh_token)
    except OIDCError as exc:
        log.info("token refresh failed for %s: %s", s.user_name, exc)
        raise LoginRequired() from exc
    return c.store.update(
        s,
        access_token=t.access_token,
        refresh_token=t.refresh_token or s.refresh_token,
        expires_at=t.expires_at,
    )


async def current_session(request: Request) -> Session:
    c = ctx(request)
    s = c.store.get(request.cookies.get(c.settings.cookie_name))
    if s is None or not s.logged_in:
        raise LoginRequired(str(request.url.path))
    return await _refresh_if_needed(c, s)


async def csrf_protect(request: Request, s: Session = Depends(current_session)) -> Session:
    """Every state-changing request carries the session's CSRF token."""
    sent = request.headers.get("X-CSRF-Token", "")
    if not sent:
        form = await request.form()
        sent = str(form.get("csrf", ""))
    if not hmac.compare_digest(sent.encode(), s.csrf.encode()):
        raise CSRFError()
    return s


async def user_api(
    request: Request, s: Session = Depends(current_session)
) -> AsyncIterator[NetBirdAPI]:
    api = ctx(request).api_factory(s.access_token)
    try:
        yield api
    except NetBirdError as exc:
        if exc.unauthorized:
            ctx(request).store.drop(s.sid)
            raise LoginRequired(str(request.url.path)) from exc
        raise
    finally:
        await api.aclose()


async def snapshot(
    request: Request,
    s: Session = Depends(current_session),
    api: NetBirdAPI = Depends(user_api),
) -> Snapshot:
    return await ctx(request).cache.get(s.user_id, lambda: load_snapshot(api))


def log_change(s: Session, what: str, before: object, after: object) -> None:
    """Before/after line for every mutation (repo convention)."""
    # %r keeps user-supplied names on one line (no forged audit entries).
    audit.info("%r (%r) %r: %r -> %r", s.user_name, s.user_id, what, before, after)


def error_message(exc: Exception) -> str:
    """User-facing text for a failed write."""
    if isinstance(exc, NetBirdError):
        if exc.forbidden:
            return "NetBird: you are not allowed to make this change."
        return f"NetBird says: {exc.message}"
    return str(exc)


def safe_path(url: str | None, default: str = "/") -> str:
    """Path of a same-site URL; anything else becomes `default`."""
    from urllib.parse import urlparse

    path = urlparse(url or "").path
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return default
    return path
