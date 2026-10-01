"""Walk every page as non-admin users and report what they get.

    uv run --project .. --with playwright python roles_check.py [--view-unblocked] [user ...]

Default users: ben (role user), frank (role auditor). `--view-unblocked`
turns off the account's "regular users may not view" setting for the run
(and back on afterwards), so regular users see their own devices. Screenshots go to
screenshots/roles/<user>/. Prints one line per page: HTTP status, errors,
how many table rows / matrix cells, and which write buttons are offered.
"""

from __future__ import annotations

import sys

from playwright.sync_api import Page, sync_playwright
from seed import SECRETS, Api, read_env
from shoot import BASE, OUT

ENV = read_env(SECRETS)
api = Api(ENV["NB_API_KEY"])
G = {g["name"]: g["id"] for g in api.get("groups")}
P = {p["name"]: p["id"] for p in api.get("peers")}
POL = {p["name"]: p["id"] for p in api.get("policies")}
U = {u.get("email") or u["name"]: u["id"] for u in api.get("users")}
K = {k["name"]: k["id"] for k in api.get("setup-keys")}
NET = {n["name"]: n["id"] for n in api.get("networks")}

PAGES = [
    ("my-access", "/my-access"),
    ("matrix", "/matrix"),
    ("matrix-peers", "/matrix?view=peers"),
    ("reach", f"/reach?src=p:{P['ben-laptop']}&dst=p:{P['web-01']}&proto=tcp&port=22"),
    ("groups", "/groups"),
    ("group", f"/groups/{G['Servers']}"),
    ("peers", "/peers"),
    ("peer-own", f"/peers/{P['ben-laptop']}"),
    ("peer-other", f"/peers/{P['web-01']}"),
    ("users", "/users"),
    ("user-self", f"/users/{U['ben@acme.test']}"),
    ("networks", "/networks"),
    ("network", f"/networks/{NET['Office']}"),
    ("resources", "/resources"),
    ("policies", "/policies"),
    ("policy", f"/policies/{POL['Dev to servers']}"),
    ("setup-keys", "/setup-keys"),
    ("anomalies", "/anomalies"),
    ("audit", "/audit"),
    ("history", "/history"),
    ("jobs", "/jobs"),
]

WRITE_BUTTONS = "button.primary, button.danger, a.button.primary, form[hx-post*='allow'] button"


def login(page: Page, user: str) -> str:
    page.goto(f"{BASE}/login")
    page.fill("input[name=login]", f"{user}@acme.test")
    page.fill("input[name=password]", ENV[f"DEMO_PW_{user.upper()}"])
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")
    return page.url


def describe(page: Page) -> str:
    rows = page.locator("table.list tbody tr:not(:has(td.empty))").count()
    cells = page.locator("table.matrix td.hit").count()
    buttons = sorted(
        {t.strip() for t in page.locator(WRITE_BUTTONS).all_inner_texts() if t.strip()}
    )
    errors = [
        t.strip()[:90]
        for t in page.locator(
            ".flash.err, .card.narrow-card h1, p.empty, td.empty"
        ).all_inner_texts()
        if t.strip()
    ]
    parts = []
    if cells:
        parts.append(f"{cells} cells")
    if rows:
        parts.append(f"{rows} rows")
    if buttons:
        parts.append("buttons: " + ", ".join(buttons))
    if errors:
        parts.append("ERR: " + " | ".join(errors))
    return "; ".join(parts) or "(empty)"


def check(browser, user: str) -> None:
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="light")
    page = ctx.new_page()
    landed = login(page, user)
    print(f"\n== {user}: landed on {landed.replace(BASE, '')}")
    out = OUT / "roles" / user
    out.mkdir(parents=True, exist_ok=True)
    for name, path in PAGES:
        resp = page.goto(BASE + path)
        page.wait_for_load_state("networkidle")
        status = resp.status if resp else "?"
        print(f"  {status} {name:13} {describe(page)}")
        page.screenshot(path=str(out / f"{name}.png"), full_page=True)
    ctx.close()


def set_view_blocked(blocked: bool) -> None:
    acc = api.get("accounts")[0]
    settings = {**acc["settings"], "regular_users_view_blocked": blocked}
    api.c.put(f"accounts/{acc['id']}", json={"settings": settings}).raise_for_status()


def main() -> None:
    args = sys.argv[1:]
    unblock = "--view-unblocked" in args
    users = [a for a in args if not a.startswith("--")] or ["ben", "frank"]
    if unblock:
        set_view_blocked(False)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for user in users:
                check(browser, user)
            browser.close()
    finally:
        if unblock:
            set_view_blocked(True)


if __name__ == "__main__":
    main()
