"""List all NetBird policies."""

from __future__ import annotations

import os
import sys
from urllib.parse import urlparse

from dotenv import load_dotenv
from netbird import APIClient


def _host_from_url(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if not parsed.netloc:
        raise ValueError(f"Cannot parse host from NB_URL={url!r}")
    return parsed.netloc


def main() -> int:
    load_dotenv()

    url = os.environ.get("NB_URL")
    token = os.environ.get("NB_API_KEY")
    if not url or not token:
        print("NB_URL and NB_API_KEY must be set in .env", file=sys.stderr)
        return 1

    client = APIClient(host=_host_from_url(url), api_token=token)
    policies = client.policies.list()

    if not policies:
        print("No policies found.")
        return 0

    for p in policies:
        pid = p.get("id", "?")
        name = p.get("name", "?")
        enabled = p.get("enabled", "?")
        rule_count = len(p.get("rules", []) or [])
        print(f"{pid}  {name!r:30}  enabled={enabled}  rules={rule_count}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
