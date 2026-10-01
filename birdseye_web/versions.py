"""Versions for the page footer: this web app, the birdseye container, NetBird.

- birdseye-web: `BIRDSEYE_VERSION` / `BIRDSEYE_REVISION`, set from the image
  build args (docker/web/Dockerfile); "dev" when run from a checkout.
- birdseye: the container writes its version into `registry.json` in the
  shared jobs dir (only known when the Jobs page is wired up).
- NetBird: `GET /instance/version`, cached for everyone for a few minutes —
  it is the same for every user and must not cost a call per page.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from birdseye_web.nbapi import NetBirdAPI, NetBirdError

CACHE_S = 600.0


@dataclass(frozen=True)
class NetBirdVersion:
    current: str
    available: str = ""
    update: bool = False


_cache: tuple[float, NetBirdVersion | None] | None = None


def reset_cache() -> None:
    global _cache
    _cache = None


def web_version() -> tuple[str, str]:
    """(version, short revision)."""
    rev = (os.environ.get("BIRDSEYE_REVISION") or "").strip()
    rev = "" if rev in ("", "unknown") else rev[:7]
    return (os.environ.get("BIRDSEYE_VERSION") or "").strip() or "dev", rev


def container_version(jobs_dir: str) -> str:
    if not jobs_dir:
        return ""
    try:
        data = json.loads((Path(jobs_dir) / "registry.json").read_text())
    except (OSError, ValueError):
        return ""
    return str(data.get("version") or "") if isinstance(data, dict) else ""


async def netbird_version(api: NetBirdAPI, clock=time.monotonic) -> NetBirdVersion | None:
    """None when the endpoint is missing (older NetBird) or not allowed."""
    global _cache
    if _cache is not None and clock() - _cache[0] < CACHE_S:
        return _cache[1]
    try:
        raw = await api.get("instance/version") or {}
        found: NetBirdVersion | None = NetBirdVersion(
            current=str(raw.get("management_current_version") or ""),
            available=str(raw.get("management_available_version") or ""),
            update=bool(raw.get("management_update_available")),
        )
    except NetBirdError:
        found = None
    if found is not None and not found.current:
        found = None
    # a failure is retried after a minute, a success kept for CACHE_S
    _cache = (clock() if found else clock() - CACHE_S + 60, found)
    return found
