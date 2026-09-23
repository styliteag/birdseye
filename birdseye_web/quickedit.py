"""One-click edits from a matrix cell: allow a pair, or revoke one grant.

Pure functions over raw API dicts; the routes do the HTTP. `revoke` always
starts from a fresh GET of the policy, so fields the matrix does not show
(posture checks, SSH `authorized_groups`, other rules) survive the PUT.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from birdseye_web.payloads import PayloadError, build_rule, policy_for_put

Json = dict[str, Any]
MODES = ("disable", "remove_source", "remove_destination", "delete")


@dataclass(frozen=True)
class Revoke:
    method: str  # "PUT" | "DELETE"
    body: Json | None


def _split(key: str) -> tuple[str, str]:
    kind, _, oid = key.partition(":")
    if not oid:
        raise PayloadError(f"invalid matrix key {key!r}")
    return kind, oid


def allow_policy(
    src_key: str,
    dst_key: str,
    *,
    names: Mapping[str, str],
    resource_types: Mapping[str, str] | None = None,
    protocol: str = "all",
    ports: str = "",
    bidirectional: bool = False,
    posture: Iterable[str] = (),
    name: str = "",
) -> Json:
    """New policy that lets group `src_key` reach `dst_key` (group, resource or peer)."""
    src_kind, src_id = _split(src_key)
    dst_kind, dst_id = _split(dst_key)
    if src_kind != "g":
        raise PayloadError("access can only be granted from a group")
    if dst_kind == "g":
        target: Json = {"destinations": [dst_id]}
    elif dst_kind == "r":
        rtype = (resource_types or {}).get(dst_id, "subnet")
        target = {"destination_resource": {"id": dst_id, "type": rtype}}
    elif dst_kind == "p":
        target = {"destination_resource": {"id": dst_id, "type": "peer"}}
    else:
        raise PayloadError(f"cannot grant access to {dst_key!r}")
    title = name.strip() or f"{names.get(src_id, src_id)} -> {names.get(dst_id, dst_id)}"
    rule = build_rule(
        name=title,
        sources=[src_id],
        protocol=protocol,
        ports=ports,
        bidirectional=bidirectional,
        **target,
    )
    return {
        "name": title,
        "description": "Created from the birdseye access matrix.",
        "enabled": True,
        "source_posture_checks": list(posture),
        "rules": [rule],
    }


def _without(rule: Json, side: str, target_id: str) -> Json | None:
    """Rule with `target_id` removed from one side, or None if that side is now empty."""
    ref = rule.get(f"{side}Resource")
    if ref:
        if ref["id"] != target_id:
            raise PayloadError(f"{target_id!r} is not the {side} of this rule")
        return None
    ids = rule.get(f"{side}s") or []
    if target_id not in ids:
        raise PayloadError(f"{target_id!r} is not a {side} of this rule")
    left = [i for i in ids if i != target_id]
    return {**rule, f"{side}s": left} if left else None


def revoke(
    policy: Mapping[str, Any], rule_id: str, mode: str, target_id: str | None = None
) -> Revoke:
    if mode not in MODES:
        raise PayloadError(f"unknown action {mode!r}")
    body = policy_for_put(policy)
    rules = body["rules"]
    if not any(r.get("id") == rule_id for r in rules):
        raise PayloadError("rule not found in policy (changed meanwhile?)")
    if mode == "delete":
        return Revoke("DELETE", None)
    if mode == "disable":
        return Revoke("PUT", {**body, "enabled": False})

    side = "source" if mode == "remove_source" else "destination"
    new_rules = []
    for r in rules:
        if r.get("id") != rule_id:
            new_rules.append(r)
            continue
        kept = _without(r, side, target_id or "")
        if kept is not None:
            new_rules.append(kept)
    if not new_rules:
        return Revoke("DELETE", None)
    return Revoke("PUT", {**body, "rules": new_rules})


EDITABLE_VIEWS = ("groups", "resources")


@dataclass(frozen=True)
class Action:
    mode: str
    target: str
    label: str


def cell_actions(
    grant: Any, row_key: str, col_key: str, names: Mapping[str, str]
) -> tuple[Action, ...]:
    """Ways to take away the access one grant gives row -> col.

    For the reverse half of a bidirectional rule the row is the rule's
    destination and the column its source, so the modes swap.
    """
    _, row_id = _split(row_key)
    _, col_id = _split(col_key)
    # Membership is checked from the grant's point of view (row = its source,
    # column = its destination); only the rule-side mode flips on reverse.
    src_side = ("remove_destination", row_id) if grant.reverse else ("remove_source", row_id)
    dst_side = ("remove_source", col_id) if grant.reverse else ("remove_destination", col_id)
    grant_src = set(grant.src_groups) | ({grant.src_ref.id} if grant.src_ref else set())
    grant_dst = set(grant.dst_groups) | ({grant.dst_ref.id} if grant.dst_ref else set())

    out = []
    if row_key.startswith("g:") and row_id in grant_src:
        out.append(Action(*src_side, f"Remove “{names.get(row_id, row_id)}” from this rule"))
    if col_id in grant_dst:
        out.append(Action(*dst_side, f"Remove “{names.get(col_id, col_id)}” from this rule"))
    out.append(Action("disable", "", "Disable the whole policy"))
    out.append(Action("delete", "", "Delete the whole policy"))
    return tuple(out)
