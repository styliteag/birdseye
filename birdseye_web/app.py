"""FastAPI app: `uv run uvicorn --factory birdseye_web.app:build`."""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from birdseye_web import (
    routes_auth,
    routes_groups,
    routes_matrix,
    routes_policies,
    routes_quickedit,
)
from birdseye_web.config import Settings, load_settings
from birdseye_web.context import TEMPLATES, AppContext, CSRFError, LoginRequired
from birdseye_web.nbapi import NetBirdAPI, NetBirdError
from birdseye_web.oidc import OIDCClient
from birdseye_web.sessions import SessionStore
from birdseye_web.snapshot import SnapshotCache

log = logging.getLogger(__name__)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
    ),
}


def create_app(
    settings: Settings,
    *,
    oidc: OIDCClient | None = None,
    api_factory=None,
) -> FastAPI:
    app = FastAPI(title="birdseye", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = AppContext(
        settings=settings,
        store=SessionStore(max_age=settings.session_hours * 3600),
        cache=SnapshotCache(ttl=settings.cache_ttl),
        oidc=oidc
        or OIDCClient(settings.oidc_issuer, settings.oidc_client_id, settings.redirect_uri),
        api_factory=api_factory or (lambda token: NetBirdAPI(settings.nb_url, token)),
    )
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

    # The IdP only allows fixed redirect URIs; register the callback at whatever
    # path is configured. On "/" the matrix redirect below yields to it.
    if settings.redirect_path == "/":

        @app.get("/", include_in_schema=False)
        async def root_or_callback(request: Request) -> Response:
            if "code" in request.query_params or "error" in request.query_params:
                return await routes_auth.callback(request)
            return RedirectResponse("/matrix", status_code=303)
    else:
        app.add_api_route(settings.redirect_path, routes_auth.callback, methods=["GET"])

    app.include_router(routes_auth.router)
    app.include_router(routes_matrix.router)
    app.include_router(routes_quickedit.router)
    app.include_router(routes_groups.router)
    app.include_router(routes_policies.router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        if settings.secure_cookies:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        if request.url.path not in ("/healthz",) and not request.url.path.startswith("/static"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.exception_handler(LoginRequired)
    async def _login(request: Request, exc: LoginRequired) -> Response:
        target = "/login?next=" + quote(exc.next_url, safe="/")
        if request.headers.get("HX-Request"):
            return Response(status_code=204, headers={"HX-Redirect": target})
        return RedirectResponse(target, status_code=303)

    @app.exception_handler(CSRFError)
    async def _csrf(request: Request, exc: CSRFError) -> Response:
        return Response("Invalid CSRF token. Reload the page.", status_code=403)

    @app.exception_handler(NetBirdError)
    async def _nb(request: Request, exc: NetBirdError) -> Response:
        log.warning("NetBird API error: %s", exc)
        return TEMPLATES.TemplateResponse(
            request,
            "login_error.html",
            {"message": f"NetBird API: {exc.status} {exc.message}"},
            status_code=502 if exc.status >= 500 else exc.status,
        )

    return app


class _DropQuery(logging.Filter):
    """Keep OAuth `code`/`state` out of the uvicorn access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], args[2].split("?", 1)[0], *args[3:])
        return True


def build() -> FastAPI:
    """Entry point for uvicorn --factory; reads `.env` and WEB_* settings."""
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("uvicorn.access").addFilter(_DropQuery())
    return create_app(load_settings())
