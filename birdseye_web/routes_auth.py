"""Login via NetBird's embedded IdP, logout."""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response

from birdseye_web.context import TEMPLATES, csrf_protect, ctx, safe_path
from birdseye_web.nbapi import NetBirdError
from birdseye_web.oidc import OIDCError, pkce_pair
from birdseye_web.sessions import Session

log = logging.getLogger(__name__)
router = APIRouter()

MAX_NEXT = 512


def _safe_next(url: str | None) -> str:
    """Only same-site relative paths, never `//host` or absolute URLs."""
    if not url or len(url) > MAX_NEXT or not url.startswith("/"):
        return "/"
    if url.startswith("//") or "\\" in url:
        return "/"
    return safe_path(url)


def set_cookie(request: Request, resp: Response, s: Session) -> Response:
    settings = ctx(request).settings
    resp.set_cookie(
        settings.cookie_name,
        s.sid,
        max_age=int(settings.session_hours * 3600),
        path="/",
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
    )
    return resp


def _error(request: Request, message: str, status: int = 400) -> Response:
    return TEMPLATES.TemplateResponse(
        request, "login_error.html", {"message": message}, status_code=status
    )


@router.get("/login")
async def login(request: Request, next: str = "/") -> Response:
    c = ctx(request)
    current = c.store.get(request.cookies.get(c.settings.cookie_name))
    if current is not None and current.logged_in:
        # Already signed in: a forged /login link must not end a working session.
        return RedirectResponse(_safe_next(next), status_code=303)
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    s = c.store.create()
    s = c.store.update(s, state=state, verifier=verifier, next_url=_safe_next(next))
    try:
        url = await c.oidc.authorization_url(state, challenge)
    except OIDCError as exc:
        return _error(request, f"Login not possible: {exc}")
    return set_cookie(request, RedirectResponse(url, status_code=303), s)


async def callback(request: Request) -> Response:
    c = ctx(request)
    q = request.query_params
    s = c.store.get(request.cookies.get(c.settings.cookie_name))
    # Check state before showing anything the query carries.
    if (
        s is None
        or not s.state
        or not secrets.compare_digest(q.get("state", "").encode(), s.state.encode())
    ):
        return _error(request, "Login session expired or invalid. Please sign in again.")
    if q.get("error"):
        c.store.drop(s.sid)
        return _error(
            request, f"Identity provider says: {q.get('error_description') or q.get('error')}"
        )
    try:
        tokens = await c.oidc.exchange(q.get("code", ""), s.verifier)
    except OIDCError as exc:
        return _error(request, f"Token exchange failed: {exc}", 502)

    api = c.api_factory(tokens.access_token)
    try:
        me = await api.get("users/current")
    except NetBirdError as exc:
        return _error(request, f"NetBird rejected the login: {exc.message}", 403)
    finally:
        await api.aclose()
    if not me.get("id"):
        # The per-user snapshot cache is keyed by this ID; never share an empty one.
        return _error(request, "NetBird returned no user ID for this login.", 403)

    s = c.store.rotate(
        s,
        state="",
        verifier="",
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        expires_at=tokens.expires_at,
        user_id=str(me["id"]),
        user_name=str(me.get("name") or me.get("email") or me["id"]),
        role=str(me.get("role", "")),
        permissions=me.get("permissions") or {},
    )
    log.info("login: %r (%r) role=%r", s.user_name, s.user_id, s.role)
    return set_cookie(request, RedirectResponse(s.next_url, status_code=303), s)


@router.post("/logout")
async def logout(request: Request, s: Session = Depends(csrf_protect)) -> Response:
    c = ctx(request)
    c.store.drop(s.sid)
    try:
        end_session = (await c.oidc.endpoints()).end_session
    except OIDCError:
        end_session = ""
    resp = TEMPLATES.TemplateResponse(request, "logged_out.html", {"end_session": end_session})
    resp.delete_cookie(
        c.settings.cookie_name,
        path="/",
        secure=c.settings.secure_cookies,
        httponly=True,
        samesite="lax",
    )
    return resp
