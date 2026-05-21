"""List all NetBird policies."""

from __future__ import annotations

from nb_client import client_from_env


def main() -> int:
    client = client_from_env(key="user")
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
