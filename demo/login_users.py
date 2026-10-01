"""Log the user-owned demo peers in through the device-code flow, as their
user (credentials from secrets.env). Skips peers that are connected already.

    uv run --project .. --with playwright python login_users.py
"""

import re
import subprocess
import time

from playwright.sync_api import sync_playwright
from seed import SECRETS, USER_PEERS, read_env


def status_ok(container: str) -> bool:
    out = subprocess.run(
        ["docker", "exec", container, "netbird", "status"], capture_output=True, text=True
    ).stdout
    return "Management: Connected" in out


def device_url(container: str) -> str | None:
    for _ in range(30):
        logs = subprocess.run(["docker", "logs", container], capture_output=True, text=True)
        found = re.findall(r"http://\S+/oauth2/device\?user_code=\S+", logs.stdout + logs.stderr)
        if found:
            return found[-1]
        time.sleep(1)
    return None


def main() -> None:
    env = read_env(SECRETS)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for host, user in USER_PEERS.items():
            container = f"demo-peer-{host}"
            if status_ok(container):
                print(f"  = {host} connected")
                continue
            url = device_url(container)
            if url is None:
                print(f"  ! {host}: no login URL in the logs")
                continue
            ctx = browser.new_context()  # fresh cookies: no login carried over
            page = ctx.new_page()
            page.goto(url)
            if not page.locator("input[name=login]").count():
                page.locator("button[type=submit]").first.click()  # confirm the device code
                page.wait_for_load_state("networkidle")
            if not page.locator("input[name=login]").count():
                print(f"  ! {host}: code expired, restarting the client for a new one")
                subprocess.run(["docker", "restart", container], capture_output=True)
                ctx.close()
                continue
            page.fill("input[name=login]", f"{user}@acme.test")
            page.fill("input[name=password]", env[f"DEMO_PW_{user.upper()}"])
            page.click("button[type=submit]")
            page.wait_for_url("**/device/callback**", timeout=20000)
            ctx.close()
            for _ in range(20):
                if status_ok(container):
                    break
                time.sleep(1)
            print(
                f"  + {host} as {user}: {'connected' if status_ok(container) else 'NOT connected'}"
            )
        browser.close()


if __name__ == "__main__":
    main()
