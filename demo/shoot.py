"""Screenshots of every birdseye-web page, light and dark.

uv run --project .. --with playwright python shoot.py [light|dark ...]
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright
from seed import SECRETS, Api, read_env

BASE = "http://localhost:58090"
OUT = Path(__file__).parent / "screenshots"
ENV = read_env(SECRETS)
api = Api(ENV["NB_API_KEY"])
G = {g["name"]: g["id"] for g in api.get("groups")}
P = {p["name"]: p["id"] for p in api.get("peers")}
POL = {p["name"]: p["id"] for p in api.get("policies")}
U = {u.get("email") or u["name"]: u["id"] for u in api.get("users")}
K = {k["name"]: k["id"] for k in api.get("setup-keys")}
NET = {n["name"]: n["id"] for n in api.get("networks")}
RES = {r["name"]: r["id"] for r in api.get(f"networks/{NET['Office']}/resources")}
HIST_DIR = Path(__file__).parent / "data/birdseye/jobs/history"
HIST = sorted(
    p.name
    for p in (Path(__file__).parent / "data/birdseye/jobs/history").iterdir()
    if not p.name.startswith(".")
)


def login(page: Page) -> None:
    page.goto(f"{BASE}/login")
    page.fill("input[name=login]", ENV["DEMO_OWNER_EMAIL"])
    page.fill("input[name=password]", ENV["DEMO_OWNER_PASSWORD"])
    page.click("button[type=submit]")
    page.wait_for_url(f"{BASE}/**", timeout=20000)
    if "/matrix" not in page.url:
        approve = page.locator("button:has-text('Grant'), button[type=submit]")
        if approve.count():
            approve.first.click()
    page.wait_for_url(f"{BASE}/matrix**", timeout=20000)


def settle(page: Page) -> None:
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(400)


def shot(page: Page, theme: str, name: str, full: bool = True) -> None:
    settle(page)
    path = OUT / theme / f"{name}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(path), full_page=full)
    print("  ", path.relative_to(OUT))


def wait_delta(page: Page) -> None:
    page.wait_for_function(
        "() => /gained|lost|No change/.test(document.querySelector('#delta')?.innerText || '')",
        timeout=10000,
    )


def web_write(page: Page) -> None:
    """One change through birdseye-web: audit badge + instant history snapshot."""
    page.goto(f"{BASE}/peers/{P['web-02']}")
    box = page.locator("input[name=ssh_enabled]")
    if not box.is_checked():
        box.check()
        page.click("button.primary")
        page.wait_for_url("**saved=1")
        page.wait_for_timeout(8000)  # let the snapshot land


def run(page: Page, theme: str) -> None:
    go = lambda path: page.goto(BASE + path)  # noqa: E731
    go("/matrix")
    shot(page, theme, "01-matrix-groups", full=False)
    page.locator("table.matrix td.hit button").nth(3).click()
    page.wait_for_selector("#popover:not([hidden])")
    shot(page, theme, "02-matrix-cell", full=False)
    go("/matrix?view=peers")
    shot(page, theme, "03-matrix-peers", full=False)
    go("/matrix?view=resources")
    shot(page, theme, "04-matrix-group-resource", full=False)
    go("/matrix?view=users")
    shot(page, theme, "04b-matrix-users", full=False)
    go(f"/reach?src=p:{P['ben-laptop']}&dst=p:{P['db-01']}&proto=tcp&port=5432")
    shot(page, theme, "05-reachability")

    go("/groups")
    shot(page, theme, "06-groups")
    go(f"/groups/{G['Servers']}")
    page.locator(f"input[name=peers][value='{P['clara-laptop']}']").check()
    wait_delta(page)
    shot(page, theme, "07-group-editor-preview")

    go("/peers")
    shot(page, theme, "08-peers")
    go(f"/peers/{P['web-01']}")
    shot(page, theme, "09-peer-editor")

    go("/users")
    shot(page, theme, "10-users")
    go(f"/users/{U['ben@acme.test']}")
    shot(page, theme, "11-user-editor")

    go("/networks")
    shot(page, theme, "12-networks")
    go(f"/networks/{NET['Office']}/resources/{RES['office-lan']}")
    shot(page, theme, "13-resource-editor")
    go("/resources")
    shot(page, theme, "14-resources")

    go("/policies")
    shot(page, theme, "15-policies")
    go(f"/policies/{POL['Dev to databases']}")
    page.locator("input[name$=-bidirectional]").first.check()
    wait_delta(page)
    shot(page, theme, "16-policy-editor-preview")

    go("/setup-keys")
    shot(page, theme, "17-setup-keys")
    go(f"/setup-keys/{K['demo-developers']}")
    shot(page, theme, "18-setup-key-editor")

    go("/anomalies")
    shot(page, theme, "19-anomalies")
    go("/audit")
    shot(page, theme, "20-audit", full=False)

    go(f"/history?from={HIST[0]}&to={HIST[-1]}")
    shot(page, theme, "21-history-diff")
    go(f"/history/object/{POL['Office NAS']}")
    shot(page, theme, "22-history-object")
    go(f"/history/{HIST[0]}/{POL['Office NAS']}")
    page.wait_for_function(
        "() => /gained|lost|No change/.test(document.querySelector('.side')?.innerText || '')",
        timeout=10000,
    )
    shot(page, theme, "23-history-restore")

    go("/jobs")
    shot(page, theme, "24-jobs")


def main() -> None:
    themes = sys.argv[1:] or ["light", "dark"]
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for i, theme in enumerate(themes):
            ctx = browser.new_context(
                viewport={"width": 1440, "height": 900},
                device_scale_factor=2,
                color_scheme=theme,
                timezone_id="Europe/Berlin",
                locale="de-DE",
            )
            page = ctx.new_page()
            login(page)
            if i == 0:
                web_write(page)
            print(theme)
            run(page, theme)
            ctx.close()
        browser.close()


if __name__ == "__main__":
    main()
