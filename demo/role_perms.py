"""Print what NetBird lets a demo user do: role, permission modules, and
what GET /peers returns with the user's own token.

    uv run --project .. --with playwright python role_perms.py nina [ben ...]

Signs in to the NetBird dashboard as the user and borrows the dashboard's
bearer token from its first API call.
"""

from __future__ import annotations

import sys

import httpx
from playwright.sync_api import sync_playwright
from seed import SECRETS, read_env

URL = "http://netbird.localhost:58080"
MODULES = (
    "accounts",
    "dns",
    "events",
    "groups",
    "nameservers",
    "networks",
    "peers",
    "policies",
    "routes",
    "setup_keys",
    "users",
    "pats",
)


def user_token(user: str) -> str:
    env = read_env(SECRETS)
    found: dict[str, str] = {}

    def grab(request) -> None:
        auth = request.headers.get("authorization")
        if "/api/" in request.url and auth:
            found.setdefault("token", auth)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.on("request", grab)
        page.goto(URL)
        page.wait_for_selector("input[name=login]", timeout=30000)
        page.fill("input[name=login]", f"{user}@acme.test")
        page.fill("input[name=password]", env[f"DEMO_PW_{user.upper()}"])
        page.click("button[type=submit]")
        for _ in range(60):
            if "token" in found:
                break
            page.wait_for_timeout(500)
        browser.close()
    if "token" not in found:
        sys.exit(f"no token captured for {user}")
    return found["token"]


def main() -> None:
    for user in sys.argv[1:] or ["nina"]:
        api = httpx.Client(base_url=URL + "/api/", headers={"Authorization": user_token(user)})
        me = api.get("users/current").json()
        mods = (me.get("permissions") or {}).get("modules", {})
        print(f"== {user}: role {me.get('role')}")
        for m in MODULES:
            ops = mods.get(m) or {}
            allowed = "".join(op[0] for op in ("create", "read", "update", "delete") if ops.get(op))
            print(f"   {m:12} {allowed or '-'}")
        peers = api.get("peers")
        names = [p["name"] for p in peers.json()] if peers.status_code == 200 else peers.text[:80]
        print(f"   GET peers -> {peers.status_code}: {names}")


if __name__ == "__main__":
    main()
