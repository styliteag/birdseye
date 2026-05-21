"""Keep ICMP "ping companion" policies in sync with their originals.

For every NetBird policy whose rules are not already protocol=all or
protocol=icmp, ensure a parallel 'ZPING: <original>' policy exists with an
ICMP rule using the same groups, posture checks, direction, and enabled state.

Default run is a full reconciliation:
  + create  ZPING companions for new originals
  ~ update  ZPING companions when the original drifted (enabled,
            posture checks, sources, destinations, action, direction, rule name)
  - delete  ZPING companions whose original was removed, or whose original
            now allows ICMP via protocol=all/icmp, or whose original is
            marked PING_IGNORE in its description

Policies whose description contains PING_IGNORE are skipped — no companion
will be created, and any existing companion will be deleted on sync.

Examples:
    uv run allow_ping.py --dry-run
    uv run allow_ping.py
    uv run allow_ping.py --remove-all --dry-run
    uv run allow_ping.py --remove-all
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from netbird import APIClient

from nb_client import client_from_env

Json = dict[str, Any]
DEFAULT_PREFIX = "ZPING: "
SKIP_PROTOCOLS = {"all", "icmp"}
PING_IGNORE_MARKER = "PING_IGNORE"


def _skips_ping(policy: Json) -> bool:
    return PING_IGNORE_MARKER in (policy.get("description") or "")


# --- setup -----------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dry-run", action="store_true", help="preview changes without writing")
    parser.add_argument(
        "--remove-all",
        action="store_true",
        help=f"delete every policy whose name starts with --prefix (default {DEFAULT_PREFIX!r})",
    )
    parser.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        help=f"prefix prepended to the original policy name (default {DEFAULT_PREFIX!r})",
    )
    return parser.parse_args()


# --- payload construction --------------------------------------------------


def _group_ids(refs: list[Json] | None) -> list[str]:
    return [r["id"] for r in (refs or [])]


def _resource_for_put(ref: Json | None) -> Json | None:
    if not ref:
        return None
    return {"id": ref["id"], "type": ref["type"]}


def _endpoint_key(rule: Json, side: str) -> tuple:
    """Dedup key for sources or destinations, including network resources."""
    resource = rule.get(f"{side}Resource") or rule.get(f"{side}_resource")
    if resource:
        return ("resource", resource["id"])
    return ("groups", tuple(sorted(_group_ids(rule.get(f"{side}s")))))


def _rule_endpoints_for_put(rule: Json) -> Json:
    out: Json = {}
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
    return out


def _qualifying_rules(policy: Json) -> list[Json]:
    """Rules whose protocol isn't already 'all' or 'icmp', deduped by
    (sources, destinations) — multiple TCP/UDP rules with the same groups
    collapse to one ICMP rule."""
    seen: set[tuple] = set()
    out: list[Json] = []
    for rule in policy.get("rules") or []:
        proto = (rule.get("protocol") or "").lower()
        if proto in SKIP_PROTOCOLS:
            continue
        key = (_endpoint_key(rule, "source"), _endpoint_key(rule, "destination"))
        if key in seen:
            continue
        seen.add(key)
        out.append(rule)
    return out


def _build_ping_payload(policy: Json, prefix: str) -> Json | None:
    qualifying = _qualifying_rules(policy)
    if not qualifying:
        return None

    icmp_rules = [
        {
            "name": f"{prefix}{rule['name']}",
            "description": "Auto-generated ICMP companion",
            "enabled": True,
            "action": rule.get("action", "accept"),
            "protocol": "icmp",
            "bidirectional": rule.get("bidirectional", True),
            **_rule_endpoints_for_put(rule),
        }
        for rule in qualifying
    ]

    return {
        "name": f"{prefix}{policy['name']}",
        "description": f"Auto-generated ICMP companion for {policy['name']!r}",
        "enabled": policy.get("enabled", True),
        "source_posture_checks": list(policy.get("source_posture_checks") or []),
        "rules": icmp_rules,
    }


# --- actions ---------------------------------------------------------------
#
# Safety invariant: this script only ever WRITES policies whose name starts
# with `prefix` (default "ZPING: "). Every POST/PUT/DELETE is gated by
# `_assert_prefixed` — if that ever fires, the filtering above is broken
# and we'd rather crash than touch an unrelated policy.


def _assert_prefixed(policy_name: str, prefix: str, op: str) -> None:
    if not policy_name.startswith(prefix):
        raise SystemExit(f"refusing to {op} {policy_name!r}: name does not start with {prefix!r}")


def _rule_summary(rule: Json) -> str:
    proto = rule.get("protocol") or "?"
    ports = ",".join(rule.get("ports") or []) or "—"
    return f"{rule.get('name')!r} ({proto}/{ports})"


def _signature(policy_or_payload: Json) -> tuple:
    """Return a comparable signature so we can detect drift between a
    desired payload and the current ZPING policy. Handles both API shapes:
    GET returns rules with embedded group objects, our payload uses ID strings.
    """

    def _ids(refs) -> tuple[str, ...]:
        return tuple(sorted(r["id"] if isinstance(r, dict) else r for r in (refs or [])))

    rule_sigs = sorted(
        (
            r.get("name", ""),
            r.get("action", "accept"),
            (r.get("protocol") or "").lower(),
            _ids(r.get("sources")),
            _ids(r.get("destinations")),
            bool(r.get("bidirectional", True)),
            bool(r.get("enabled", True)),
        )
        for r in (policy_or_payload.get("rules") or [])
    )
    return (
        bool(policy_or_payload.get("enabled", True)),
        tuple(sorted(policy_or_payload.get("source_posture_checks") or [])),
        tuple(rule_sigs),
    )


def _plan_creations(
    originals: list[Json], existing_zping: dict[str, Json], prefix: str
) -> tuple[list[tuple[Json, Json]], list[Json]]:
    planned: list[tuple[Json, Json]] = []
    ignored: list[Json] = []
    for policy in originals:
        if f"{prefix}{policy['name']}" in existing_zping:
            continue
        if _skips_ping(policy):
            ignored.append(policy)
            continue
        payload = _build_ping_payload(policy, prefix)
        if payload is not None:
            planned.append((policy, payload))
    return planned, ignored


def _report_ignored(ignored: list[Json]) -> int:
    for policy in ignored:
        print(
            f"  · {policy['name']!r} ({policy['id']})  "
            f"(skipped, {PING_IGNORE_MARKER} in description)"
        )
    return len(ignored)


def _do_create(
    client: APIClient, planned: list[tuple[Json, Json]], prefix: str, dry_run: bool
) -> int:
    for original, payload in planned:
        src_rules = ", ".join(_rule_summary(r) for r in (original.get("rules") or []))
        print(
            f"  + {payload['name']!r}  "
            f"(from {original['name']!r}: {src_rules}; "
            f"{len(payload['rules'])} icmp rule(s))"
        )
        if not dry_run:
            _assert_prefixed(payload["name"], prefix, "create")
            client.post("policies", data=payload)
    return len(planned)


def _do_sync(
    client: APIClient,
    existing_zping: dict[str, Json],
    originals_by_name: dict[str, Json],
    prefix: str,
    dry_run: bool,
) -> tuple[int, int]:
    """Update drifted ZPING policies and delete orphans. Returns (updated, deleted)."""
    updated = deleted = 0
    for zping_name, zping in existing_zping.items():
        original_name = zping_name[len(prefix) :]
        original = originals_by_name.get(original_name)

        if original is None:
            print(f"  - {zping_name!r} ({zping['id']})  (original missing)")
            if not dry_run:
                _assert_prefixed(zping["name"], prefix, "delete")
                client.policies.delete(zping["id"])
            deleted += 1
            continue

        if _skips_ping(original):
            print(
                f"  - {zping_name!r} ({zping['id']})  "
                f"(original marked {PING_IGNORE_MARKER})"
            )
            if not dry_run:
                _assert_prefixed(zping["name"], prefix, "delete")
                client.policies.delete(zping["id"])
            deleted += 1
            continue

        desired = _build_ping_payload(original, prefix)
        if desired is None:
            print(f"  - {zping_name!r} ({zping['id']})  (original now allows icmp)")
            if not dry_run:
                _assert_prefixed(zping["name"], prefix, "delete")
                client.policies.delete(zping["id"])
            deleted += 1
            continue

        if _signature(zping) == _signature(desired):
            continue

        print(f"  ~ {zping_name!r} ({zping['id']})  (drift from {original['name']!r})")
        if not dry_run:
            _assert_prefixed(zping["name"], prefix, "update")
            _assert_prefixed(desired["name"], prefix, "update")
            client.put(f"policies/{zping['id']}", data=desired)
        updated += 1
    return updated, deleted


def _do_remove(client: APIClient, policies: list[Json], prefix: str, dry_run: bool) -> int:
    targets = [p for p in policies if p["name"].startswith(prefix)]
    if not targets:
        print(f"No policies starting with {prefix!r} found.")
        return 0

    print(f"{'[dry-run] ' if dry_run else ''}Deleting {len(targets)} companion policy(ies):")
    for policy in targets:
        print(f"  - {policy['name']!r} ({policy['id']})")
        if not dry_run:
            _assert_prefixed(policy["name"], prefix, "delete")
            client.policies.delete(policy["id"])
    return 0


# --- entrypoint ------------------------------------------------------------


def main() -> int:
    args = _parse_args()
    client = client_from_env(key="user")
    policies = client.policies.list()

    if args.remove_all:
        return _do_remove(client, policies, args.prefix, args.dry_run)

    existing_zping = {p["name"]: p for p in policies if p["name"].startswith(args.prefix)}
    originals = [p for p in policies if not p["name"].startswith(args.prefix)]
    originals_by_name = {p["name"]: p for p in originals}

    planned, ignored = _plan_creations(originals, existing_zping, args.prefix)

    print(
        f"{'[dry-run] ' if args.dry_run else ''}"
        f"ZPING sync ({len(originals)} originals, {len(existing_zping)} companions):"
    )
    created = _do_create(client, planned, args.prefix, args.dry_run)
    ignored_count = _report_ignored(ignored)
    updated, deleted = _do_sync(
        client, existing_zping, originals_by_name, args.prefix, args.dry_run
    )

    unchanged = len(existing_zping) - updated - deleted
    print(
        f"\nSummary: created={created} updated={updated} "
        f"deleted={deleted} unchanged={unchanged} ignored={ignored_count} "
        f"dry_run={args.dry_run}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
