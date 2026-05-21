"""Add or remove a posture check on one or all NetBird policies.

Examples:
    uv run manage_posture.py --all --add-posture European
    uv run manage_posture.py --rule "SSH Access" --remove-posture European
    uv run manage_posture.py --all --remove-posture European --dry-run

Policies whose description contains POSTURE_IGNORE are skipped when adding posture checks.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from netbird import APIClient

from nb_client import client_from_env

Json = dict[str, Any]
POSTURE_IGNORE_MARKER = "POSTURE_IGNORE"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )

    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--all", action="store_true", help="apply to every policy")
    target.add_argument(
        "--rule",
        "--policy",
        dest="rule",
        metavar="NAME",
        help="apply only to the policy with this name",
    )

    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--add-posture", metavar="NAME", help="posture check name to attach")
    action.add_argument("--remove-posture", metavar="NAME", help="posture check name to detach")

    parser.add_argument(
        "--dry-run", action="store_true", help="print changes without calling the API"
    )
    return parser.parse_args()


def _resolve_posture_id(name: str, posture_checks: list[Json]) -> str:
    matches = [c for c in posture_checks if c["name"] == name]
    if not matches:
        available = ", ".join(repr(c["name"]) for c in posture_checks) or "<none>"
        raise SystemExit(f"Posture check {name!r} not found. Available: {available}")
    if len(matches) > 1:
        raise SystemExit(f"Multiple posture checks named {name!r}; refusing to guess.")
    return matches[0]["id"]


def _select_policies(policies: list[Json], rule_name: str | None) -> list[Json]:
    if rule_name is None:
        return policies
    matches = [p for p in policies if p["name"] == rule_name]
    if not matches:
        available = ", ".join(repr(p["name"]) for p in policies)
        raise SystemExit(f"Policy {rule_name!r} not found. Available: {available}")
    return matches


def _skips_posture_add(policy: Json) -> bool:
    return POSTURE_IGNORE_MARKER in (policy.get("description") or "")


def _resource_for_put(ref: Json | None) -> Json | None:
    if not ref:
        return None
    return {"id": ref["id"], "type": ref["type"]}


def _rule_for_put(rule: Json) -> Json:
    """Reduce a GET rule to the shape the PUT endpoint expects.

    Sources/destinations come back as embedded group objects; the API expects
    group IDs on write, so we flatten them. Resource targets use
    sourceResource/destinationResource (mutually exclusive with groups).
    We avoid the library's pydantic models because their Protocol enum rejects
    valid values like `netbird-ssh`.
    """

    def _group_ids(refs: list[Json] | None) -> list[str]:
        return [r["id"] for r in (refs or [])]

    out: Json = {
        "name": rule["name"],
        "description": rule.get("description") or "",
        "enabled": rule.get("enabled", True),
        "action": rule["action"],
        "protocol": rule["protocol"],
        "bidirectional": rule.get("bidirectional", True),
    }
    if rule.get("id"):
        out["id"] = rule["id"]

    source_resource = _resource_for_put(rule.get("sourceResource") or rule.get("source_resource"))
    if source_resource:
        out["sourceResource"] = source_resource
    else:
        out["sources"] = _group_ids(rule.get("sources"))

    destination_resource = _resource_for_put(
        rule.get("destinationResource") or rule.get("destination_resource")
    )
    if destination_resource:
        out["destinationResource"] = destination_resource
    else:
        out["destinations"] = _group_ids(rule.get("destinations"))

    if rule.get("ports"):
        out["ports"] = rule["ports"]
    if rule.get("port_ranges"):
        out["port_ranges"] = rule["port_ranges"]
    if rule.get("authorized_groups"):
        out["authorized_groups"] = rule["authorized_groups"]
    return out


def _new_posture_list(policy: Json, posture_id: str, add: bool) -> list[str] | None:
    """Return the new posture-check list, or None if no change is needed."""
    current = list(policy.get("source_posture_checks") or [])
    if add:
        if posture_id in current:
            return None
        return current + [posture_id]
    if posture_id not in current:
        return None
    return [pid for pid in current if pid != posture_id]


def _apply_change(
    client: APIClient,
    policy: Json,
    new_posture: list[str],
    dry_run: bool,
) -> None:
    before = policy.get("source_posture_checks") or []
    print(f"  {policy['name']!r} ({policy['id']}): {before} -> {new_posture}")
    if dry_run:
        return

    payload: Json = {
        "name": policy["name"],
        "description": policy.get("description") or "",
        "enabled": policy.get("enabled", True),
        "source_posture_checks": new_posture,
        "rules": [_rule_for_put(r) for r in (policy.get("rules") or [])],
    }
    client.put(f"policies/{policy['id']}", data=payload)


def main() -> int:
    args = _parse_args()
    add = args.add_posture is not None
    posture_name = args.add_posture if add else args.remove_posture
    verb = "add" if add else "remove"

    client = client_from_env(key="user")
    policies = client.policies.list()
    posture_checks = client.posture_checks.list()

    posture_id = _resolve_posture_id(posture_name, posture_checks)
    targets = _select_policies(policies, args.rule)

    print(
        f"{'[dry-run] ' if args.dry_run else ''}"
        f"{verb} posture {posture_name!r} ({posture_id}) on {len(targets)} policy(ies):"
    )

    changed = skipped = ignored = 0
    for policy in targets:
        if add and _skips_posture_add(policy):
            print(
                f"  {policy['name']!r} ({policy['id']}): "
                f"skipped ({POSTURE_IGNORE_MARKER} in description)"
            )
            ignored += 1
            continue
        new_list = _new_posture_list(policy, posture_id, add=add)
        if new_list is None:
            print(f"  {policy['name']!r} ({policy['id']}): no change")
            skipped += 1
            continue
        _apply_change(client, policy, new_list, dry_run=args.dry_run)
        changed += 1

    print(f"\nDone. changed={changed} skipped={skipped} ignored={ignored} dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
