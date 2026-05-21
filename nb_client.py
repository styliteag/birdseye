"""Shared NetBird API client builder used by every operator script.

Two flavours of token live in this project:
  NB_API_KEY        — read-only or write-on-own-account, scope: forwarder + most one-shots
  NB_ADMIN_API_KEY  — admin scope, needed to read users/setup-keys and to delete peers

Selecting the right one used to be duplicated in eight files; pick it
explicitly here with `key="user"|"admin"` and the SystemExit message
will name the env var the caller actually relies on.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

from dotenv import load_dotenv
from netbird import APIClient

_TOKEN_ENV: dict[str, str] = {
    "user": "NB_API_KEY",
    "admin": "NB_ADMIN_API_KEY",
}


def host_from_url(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if not parsed.netloc:
        raise ValueError(f"Cannot parse host from NB_URL={url!r}")
    return parsed.netloc


def client_from_env(*, key: str = "user", fallback_to_user: bool = False) -> APIClient:
    """Build an APIClient from `.env`.

    `key="user"`  reads NB_API_KEY.
    `key="admin"` reads NB_ADMIN_API_KEY.
    `fallback_to_user=True` only matters when `key="admin"` — used by
    `setup_keys.py`, which historically accepted either token.
    """
    if key not in _TOKEN_ENV:
        raise ValueError(f"Unknown key type {key!r}; expected one of {sorted(_TOKEN_ENV)}")

    load_dotenv()
    url = (os.environ.get("NB_URL") or "").strip()
    if not url:
        raise SystemExit("NB_URL must be set in .env")

    token_var = _TOKEN_ENV[key]
    token = (os.environ.get(token_var) or "").strip()
    if not token and fallback_to_user and key == "admin":
        token = (os.environ.get(_TOKEN_ENV["user"]) or "").strip()
        if token:
            token_var = _TOKEN_ENV["user"]
        else:
            token_var = f"{_TOKEN_ENV['admin']} (or {_TOKEN_ENV['user']})"

    if not token:
        raise SystemExit(f"{token_var} must be set in .env")

    return APIClient(host=host_from_url(url), api_token=token)
