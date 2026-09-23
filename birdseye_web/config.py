"""Web UI settings, read from `WEB_*` env vars (NB_URL as fallback)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse


class ConfigError(SystemExit):
    pass


@dataclass(frozen=True)
class Settings:
    nb_url: str
    base_url: str
    session_secret: str
    oidc_issuer: str
    oidc_client_id: str = "netbird-dashboard"
    redirect_path: str = "/auth/callback"
    cache_ttl: float = 30.0
    session_hours: float = 12.0

    @property
    def redirect_uri(self) -> str:
        return self.base_url.rstrip("/") + self.redirect_path

    @property
    def secure_cookies(self) -> bool:
        return urlparse(self.base_url).scheme == "https"

    @property
    def cookie_name(self) -> str:
        """`__Host-` pins the cookie to this exact origin (no Domain, Secure, Path=/),
        so sibling subdomains cannot plant or read it. Browsers only accept it
        over HTTPS, hence the plain name for local http development."""
        return "__Host-birdseye_sid" if self.secure_cookies else "birdseye_sid"


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env

    def get(name: str, default: str = "") -> str:
        return (env.get(name) or default).strip()

    nb_url = get("WEB_NB_URL", get("NB_URL")).rstrip("/")
    required = {
        "WEB_NB_URL (or NB_URL)": nb_url,
        "WEB_BASE_URL": get("WEB_BASE_URL"),
        "WEB_SESSION_SECRET": get("WEB_SESSION_SECRET"),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise ConfigError("birdseye-web: missing configuration: " + ", ".join(missing))
    if len(get("WEB_SESSION_SECRET")) < 32:
        raise ConfigError("birdseye-web: WEB_SESSION_SECRET must be at least 32 characters")
    redirect_path = get("WEB_REDIRECT_PATH", "/auth/callback")
    if not redirect_path.startswith("/"):
        raise ConfigError("birdseye-web: WEB_REDIRECT_PATH must start with '/'")
    return Settings(
        nb_url=nb_url,
        base_url=get("WEB_BASE_URL").rstrip("/"),
        session_secret=get("WEB_SESSION_SECRET"),
        oidc_issuer=get("WEB_OIDC_ISSUER", f"{nb_url}/oauth2").rstrip("/"),
        oidc_client_id=get("WEB_OIDC_CLIENT_ID", "netbird-dashboard"),
        redirect_path=redirect_path,
        cache_ttl=float(get("WEB_CACHE_TTL", "30")),
        session_hours=float(get("WEB_SESSION_HOURS", "12")),
    )
