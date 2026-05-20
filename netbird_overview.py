"""Inventory NetBird policies, groups, and posture checks, then print a
detailed per-policy breakdown showing referenced groups and posture checks."""

from __future__ import annotations

import os
import sys
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from netbird import APIClient

Json = dict[str, Any]


def _host_from_url(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if not parsed.netloc:
        raise ValueError(f"Cannot parse host from NB_URL={url!r}")
    return parsed.netloc


def _client_from_env() -> APIClient:
    load_dotenv()
    url = os.environ.get("NB_URL")
    token = os.environ.get("NB_API_KEY")
    if not url or not token:
        raise SystemExit("NB_URL and NB_API_KEY must be set in .env")
    return APIClient(host=_host_from_url(url), api_token=token)


def _section(title: str) -> None:
    print()
    print(title)
    print("=" * len(title))


def _print_policies_summary(policies: list[Json]) -> None:
    _section(f"Policies ({len(policies)})")
    for p in policies:
        rule_count = len(p.get("rules") or [])
        pc_count = len(p.get("source_posture_checks") or [])
        print(
            f"  {p['id']}  {p['name']!r:35}  "
            f"enabled={p.get('enabled')}  rules={rule_count}  posture_checks={pc_count}"
        )


def _print_groups_summary(groups: list[Json]) -> None:
    _section(f"Groups ({len(groups)})")
    for g in groups:
        print(
            f"  {g['id']}  {g['name']!r:35}  "
            f"peers={g.get('peers_count', 0)}  resources={g.get('resources_count', 0)}"
        )


def _print_posture_checks_summary(checks: list[Json]) -> None:
    _section(f"Posture Checks ({len(checks)})")
    for c in checks:
        kinds = ", ".join(sorted((c.get("checks") or {}).keys())) or "—"
        print(f"  {c['id']}  {c['name']!r:25}  checks=[{kinds}]")


def _format_group_refs(refs: list[Json] | None) -> str:
    if not refs:
        return "—"
    return ", ".join(f"{r['name']} ({r['id']})" for r in refs)


def _format_posture_refs(ids: list[str] | None, by_id: dict[str, Json]) -> str:
    if not ids:
        return "—"
    parts = []
    for pid in ids:
        check = by_id.get(pid)
        parts.append(f"{check['name']} ({pid})" if check else f"<unknown> ({pid})")
    return ", ".join(parts)


def _print_rule_detail(rule: Json) -> None:
    ports = ",".join(rule.get("ports") or []) or "—"
    print(f"    Rule: {rule.get('name')!r}  ({rule.get('id')})")
    print(f"      action       : {rule.get('action')}")
    print(f"      protocol     : {rule.get('protocol')}   ports: {ports}")
    print(f"      bidirectional: {rule.get('bidirectional')}")
    print(f"      enabled      : {rule.get('enabled')}")
    print(f"      sources      : {_format_group_refs(rule.get('sources'))}")
    print(f"      destinations : {_format_group_refs(rule.get('destinations'))}")


def _print_policy_detail(policy: Json, posture_by_id: dict[str, Json]) -> None:
    print()
    print(f"Policy: {policy['name']!r}  ({policy['id']})")
    print("-" * 72)
    if policy.get("description"):
        print(f"  description    : {policy['description']}")
    print(f"  enabled        : {policy.get('enabled')}")
    print(
        f"  posture checks : "
        f"{_format_posture_refs(policy.get('source_posture_checks'), posture_by_id)}"
    )
    rules = policy.get("rules") or []
    print(f"  rules ({len(rules)}):")
    for rule in rules:
        _print_rule_detail(rule)


def main() -> int:
    client = _client_from_env()

    policies = client.policies.list()
    groups = client.groups.list()
    posture_checks = client.posture_checks.list()

    _print_policies_summary(policies)
    _print_groups_summary(groups)
    _print_posture_checks_summary(posture_checks)

    posture_by_id = {c["id"]: c for c in posture_checks}

    _section("Policy Details")
    for policy in policies:
        _print_policy_detail(policy, posture_by_id)

    return 0


if __name__ == "__main__":
    sys.exit(main())
