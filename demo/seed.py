"""Seed the demo NetBird with a small fictional company ("Acme") and write
peers.yml, which enrolls real NetBird clients with the created setup keys.

Idempotent enough to re-run: objects are looked up by name first.
Run from demo/:  uv run --project .. python seed.py
"""

from __future__ import annotations

import secrets
import sys
import time
from pathlib import Path

import httpx

URL = "http://netbird.localhost:58080/api/"
HERE = Path(__file__).parent
SECRETS = HERE / "secrets.env"
OWNER = "olivia@acme.test"


def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    pairs = (line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
    return {k.strip(): v.strip().strip("'") for k, v in pairs}


def wait_for_server() -> dict:
    for _ in range(60):
        try:
            return httpx.get(URL + "instance", timeout=3).json()
        except (httpx.HTTPError, ValueError):
            time.sleep(2)
    sys.exit("NetBird server did not come up")


def setup_owner() -> str:
    env = read_env(SECRETS)
    if env.get("NB_API_KEY"):
        return env["NB_API_KEY"]
    if not wait_for_server().get("setup_required"):
        sys.exit("NetBird is set up already but secrets.env has no token: run ./down.sh first")
    password = "Demo-" + secrets.token_urlsafe(9)
    r = httpx.post(
        URL + "setup",
        json={
            "email": OWNER,
            "password": password,
            "name": "Olivia Owner",
            "create_pat": True,
            "pat_expire_in": 365,
        },
        timeout=30,
    )
    r.raise_for_status()
    token = r.json().get("personal_access_token")
    if not token:
        sys.exit(f"setup gave no token (NB_SETUP_PAT_ENABLED?): {r.text}")
    SECRETS.write_text(
        f"NB_API_KEY='{token}'\nNB_ADMIN_API_KEY='{token}'\n"
        f"DEMO_OWNER_EMAIL='{OWNER}'\nDEMO_OWNER_PASSWORD='{password}'\n"
    )
    print(f"owner {OWNER} created")
    return token


class Api:
    def __init__(self, token: str) -> None:
        self.c = httpx.Client(base_url=URL, headers={"Authorization": f"Token {token}"}, timeout=30)

    def get(self, path: str):
        return self.c.get(path).raise_for_status().json()

    def post(self, path: str, body: dict):
        r = self.c.post(path, json=body)
        if r.status_code >= 400:
            print(f"  ! POST {path}: {r.status_code} {r.text[:200]}")
            return None
        return r.json()

    def ensure(self, path: str, name: str, body: dict, key: str = "name") -> dict | None:
        for item in self.get(path) or []:
            if item.get(key) == name:
                return item
        made = self.post(path, body)
        if made:
            print(f"  + {path} {name}")
        return made


GROUPS = [
    "Admins",
    "Developers",
    "Sales",
    "Support",
    "Servers",
    "Databases",
    "Office-Router",
    "Office-Printers-RG",
    "Office-NAS-RG",
    "Office-LAN-RG",
    "Lab-Wiki-RG",
    "Contractors",
    "Z002 Short forms: -RG=resource group, -Router=routing peers",
]

# hostname -> setup key name (infrastructure, no user)
PEERS = {
    "web-01": "servers",
    "web-02": "servers",
    "db-01": "databases",
    "office-gw": "office-router",
}
# hostname -> user: laptops enroll through the user's own login (login_users.py)
USER_PEERS = {
    "anna-mbp": "anna",
    "ben-laptop": "ben",
    "clara-laptop": "clara",
    "clara-desktop": "clara",
    "dan-laptop": "dan",
    "eve-laptop": "eve",
}
USERS = (
    ("anna@acme.test", "Anna Admin", "admin", ["Admins"]),
    ("ben@acme.test", "Ben Builder", "user", ["Developers"]),
    ("clara@acme.test", "Clara Code", "user", ["Developers"]),
    ("dan@acme.test", "Dan Deals", "user", ["Sales"]),
    ("eve@acme.test", "Eve Support", "user", ["Support"]),
    ("frank@acme.test", "Frank Audit", "auditor", []),
    ("nina@acme.test", "Nina Netadmin", "network_admin", []),
)


DEFAULT_POLICIES = ("Default", "Users to My Resource", "Users to Routing Peers")
DEFAULT_NETWORKS = ("My First Network",)
DEFAULT_GROUPS = ("Users", "Routing Peers")


def remove_defaults(api: Api) -> None:
    """NetBird's new-account defaults: an allow-all policy and an onboarding
    network. They would make every matrix cell green."""
    for p in api.get("policies") or []:
        if p["name"] in DEFAULT_POLICIES:
            api.c.delete(f"policies/{p['id']}")
            print("  - policy", p["name"])
    for n in api.get("networks") or []:
        if n["name"] in DEFAULT_NETWORKS:
            api.c.delete(f"networks/{n['id']}")
            print("  - network", n["name"])
    for k in api.get("setup-keys") or []:
        if "(My First Network)" in k["name"]:
            api.c.put(f"setup-keys/{k['id']}", json={"revoked": True, "auto_groups": []})
            api.c.delete(f"setup-keys/{k['id']}")
            print("  - setup key", k["name"])
    for g in api.get("groups") or []:
        if g["name"] in DEFAULT_GROUPS:
            r = api.c.delete(f"groups/{g['id']}")
            print("  - group", g["name"], r.status_code)


def seed(api: Api) -> dict[str, str]:
    remove_defaults(api)
    g = {name: api.ensure("groups", name, {"name": name})["id"] for name in GROUPS}

    pc = api.ensure(
        "posture-checks",
        "Client 0.30+",
        {
            "name": "Client 0.30+",
            "description": "NetBird client at least 0.30",
            "checks": {"nb_version_check": {"min_version": "0.30.0"}},
        },
    )
    api.ensure(
        "posture-checks",
        "Germany only",
        {
            "name": "Germany only",
            "description": "Connections from Germany",
            "checks": {
                "geo_location_check": {"locations": [{"country_code": "DE"}], "action": "allow"}
            },
        },
    )

    office = api.ensure("networks", "Office", {"name": "Office", "description": "HQ office LAN"})
    lab = api.ensure("networks", "Lab", {"name": "Lab", "description": "Test lab (no router yet)"})
    res = {}
    for net, name, addr, group in (
        (office, "printer-1", "10.10.0.20/32", "Office-Printers-RG"),
        (office, "nas", "10.10.0.5/32", "Office-NAS-RG"),
        (office, "office-lan", "10.10.0.0/24", "Office-LAN-RG"),
        (lab, "wiki", "wiki.lab.internal", "Lab-Wiki-RG"),
    ):
        r = api.ensure(
            f"networks/{net['id']}/resources",
            name,
            {
                "name": name,
                "address": addr,
                "enabled": True,
                "groups": [g[group]],
                "description": "",
            },
        )
        res[name] = r
    routers = api.get(f"networks/{office['id']}/routers") or []
    if not routers:
        api.post(
            f"networks/{office['id']}/routers",
            {
                "peer_groups": [g["Office-Router"]],
                "metric": 100,
                "masquerade": True,
                "enabled": True,
            },
        )

    keys = {}
    for name, groups in (
        ("developers", ["Developers"]),  # never used: shows as an anomaly
        ("servers", ["Servers"]),
        ("databases", ["Databases"]),
        ("office-router", ["Office-Router"]),
    ):
        k = api.post(
            "setup-keys",
            {
                "name": f"demo-{name}",
                "type": "reusable",
                "expires_in": 30 * 86400,
                "auto_groups": [g[x] for x in groups],
                "usage_limit": 0,
                "ephemeral": False,
            },
        )
        keys[name] = k["key"]
    # keys that show up on the setup key page / anomalies
    for body in (
        {
            "name": "contractor-onboarding",
            "type": "one-off",
            "expires_in": 7 * 86400,
            "auto_groups": [g["Contractors"]],
            "usage_limit": 0,
            "ephemeral": False,
        },
        {
            "name": "ci-runners",
            "type": "reusable",
            "expires_in": 90 * 86400,
            "auto_groups": [g["Servers"]],
            "usage_limit": 0,
            "ephemeral": True,
        },
    ):
        api.ensure("setup-keys", body["name"], body)

    def rule(name, src, dst=(), proto="all", ports=None, bidi=False, dst_res=None):
        r = {
            "name": name,
            "description": "",
            "enabled": True,
            "action": "accept",
            "protocol": proto,
            "bidirectional": bidi,
            "sources": [g[s] for s in src],
        }
        if dst_res:
            r["destinationResource"] = {"id": dst_res["id"], "type": dst_res.get("type") or "host"}
        else:
            r["destinations"] = [g[d] for d in dst]
        if ports:
            r["ports"] = ports
        return r

    all_id = next(x["id"] for x in api.get("groups") if x["name"] == "All")
    g["All"] = all_id
    policies = [
        (
            "Admin full access",
            "Admins can reach everything",
            [rule("Admins -> All", ["Admins"], ["All"])],
            [],
        ),
        (
            "Dev to servers",
            "",
            [rule("ssh/web", ["Developers"], ["Servers"], "tcp", ["22", "443", "8080"])],
            [],
        ),
        (
            "Dev to databases",
            "Postgres and Redis, posture-gated",
            [rule("postgres", ["Developers"], ["Databases"], "tcp", ["5432", "6379"])],
            [pc["id"]],
        ),
        (
            "App to database",
            "",
            [rule("postgres", ["Servers"], ["Databases"], "tcp", ["5432"])],
            [],
        ),
        (
            "Printing",
            "",
            [
                rule(
                    "ipp/raw",
                    ["Sales", "Support"],
                    ["Office-Printers-RG"],
                    "tcp",
                    ["515", "631", "9100"],
                )
            ],
            [],
        ),
        (
            "Office NAS",
            "",
            [rule("smb", ["Admins", "Support"], ["Office-NAS-RG"], "tcp", ["445"])],
            [],
        ),
        (
            "Ping servers",
            "Paused during network migration",
            [rule("icmp", ["All"], ["Servers"], "icmp")],
            [],
        ),
        (
            "QA staging",
            "Developers test on web-02",
            [rule("staging", ["Developers"], ["Servers"], "tcp", ["3000"])],
            [],
        ),
        (
            "Admin SSH",
            "",
            [rule("netbird ssh", ["Admins"], ["Servers", "Databases"], "netbird-ssh")],
            [],
        ),
        ("Sales CRM", "Direct to web-01 (should be a group)", None, []),
        (
            "Legacy VPN",
            "Kept for reference",
            [rule("legacy", ["Contractors"], ["Office-LAN-RG"])],
            [],
        ),
    ]
    for name, desc, rules, posture in policies:
        if rules is None:
            continue
        body = {
            "name": name,
            "description": desc,
            "enabled": name not in ("Legacy VPN", "Ping servers"),
            "rules": rules,
            "source_posture_checks": posture,
        }
        api.ensure("policies", name, body)

    known = {u.get("email") for u in api.get("users") or []}
    for email, name, role, groups in USERS:
        if email in known:
            continue
        made = api.post(
            "users",
            {
                "email": email,
                "name": name,
                "role": role,
                "auto_groups": [g[x] for x in groups],
                "is_service_user": False,
            },
        )
        if made and made.get("password"):
            with SECRETS.open("a") as f:
                f.write(f"DEMO_PW_{email.split('@')[0].upper()}='{made['password']}'\n")
        print("  + user", email)
    api.ensure(
        "users",
        "CI Bot",
        {"name": "CI Bot", "role": "user", "auto_groups": [], "is_service_user": True},
    )
    return keys


def write_peers(keys: dict[str, str]) -> None:
    lines = [
        "# Generated by seed.py: demo peers, real NetBird clients.",
        "x-peer: &peer",
        "  image: netbirdio/netbird:latest",
        "  networks: [demo]",
        "  restart: unless-stopped",
        "  cap_add: [NET_ADMIN, SYS_ADMIN, SYS_RESOURCE]",
        '  extra_hosts: ["netbird.localhost:host-gateway"]',
        "  logging: { driver: json-file, options: { max-size: 10m, max-file: '2' } }",
        "services:",
    ]
    for host, user in USER_PEERS.items():
        lines += [
            f"  peer-{host}:",
            "    <<: *peer",
            f"    container_name: demo-peer-{host}",
            f"    hostname: {host}",
            "    environment:",
            "      NB_MANAGEMENT_URL: http://netbird.localhost:58080",
            f"      NB_HOSTNAME: {host}",
            "      NB_LOG_LEVEL: warn",
            f"    labels: {{ demo.user: {user} }}",
            f"    volumes: ['./data/peers/{host}:/var/lib/netbird']",
        ]
    for host, key in PEERS.items():
        lines += [
            f"  peer-{host}:",
            "    <<: *peer",
            f"    container_name: demo-peer-{host}",
            f"    hostname: {host}",
            "    environment:",
            "      NB_MANAGEMENT_URL: http://netbird.localhost:58080",
            f"      NB_SETUP_KEY: {keys[key]}",
            f"      NB_HOSTNAME: {host}",
            "      NB_LOG_LEVEL: warn",
            f"    volumes: ['./data/peers/{host}:/var/lib/netbird']",
        ]
    (HERE / "peers.yml").write_text("\n".join(lines) + "\n")
    print(f"peers.yml: {len(PEERS)} setup-key peers, {len(USER_PEERS)} user peers")


def drift(api: Api) -> None:
    """Two devices whose groups differ from their user's auto-groups."""
    peers = {p["name"]: p["id"] for p in api.get("peers") or []}
    groups = {x["name"]: x for x in api.get("groups")}
    for gname, host, add in (
        ("Servers", "ben-laptop", True),
        ("Developers", "clara-desktop", False),
    ):
        if host not in peers:
            continue
        grp = api.get(f"groups/{groups[gname]['id']}")
        members = {x["id"] for x in grp.get("peers") or []}
        new = members | {peers[host]} if add else members - {peers[host]}
        if new != members:
            api.c.put(
                f"groups/{grp['id']}",
                json={
                    "name": grp["name"],
                    "peers": sorted(new),
                    "resources": [
                        {"id": r["id"], "type": r["type"]} for r in grp.get("resources") or []
                    ],
                },
            ).raise_for_status()
            print("  ~ drift", host, gname)


def late_policies(api: Api) -> None:
    """Rules that need peers: a direct-peer rule (shows up as an anomaly)."""
    peers = {p["name"]: p for p in api.get("peers") or []}
    web = peers.get("web-01")
    if web is None:
        print("  (web-01 not enrolled yet, skipping Sales CRM)")
        return
    sales = next(x["id"] for x in api.get("groups") if x["name"] == "Sales")
    api.ensure(
        "policies",
        "Sales CRM",
        {
            "name": "Sales CRM",
            "description": "Direct to web-01 (should be a group)",
            "enabled": True,
            "source_posture_checks": [],
            "rules": [
                {
                    "name": "crm",
                    "description": "",
                    "enabled": True,
                    "action": "accept",
                    "protocol": "tcp",
                    "ports": ["443"],
                    "bidirectional": False,
                    "sources": [sales],
                    "destinationResource": {"id": web["id"], "type": "peer"},
                }
            ],
        },
    )


def main() -> None:
    token = setup_owner()
    api = Api(token)
    if sys.argv[1:] == ["late"]:
        remove_defaults(api)  # onboarding objects can appear after the first login
        late_policies(api)
        drift(api)
        return
    write_peers(seed(api))


if __name__ == "__main__":
    main()
