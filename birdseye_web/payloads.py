"""Raw-dict write payloads for NetBird groups, users and policies.

The SDK's pydantic models reject valid protocols such as `netbird-ssh`, so
every write goes out as a plain dict. GET responses embed group objects;
writes expect IDs, so `*_for_put` flattens them. Nothing here mutates its
input.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

Json = dict[str, Any]

PROTOCOLS = ("all", "tcp", "udp", "icmp", "netbird-ssh")
PORTED = frozenset({"tcp", "udp"})
ACTIONS = ("accept", "drop")


class PayloadError(ValueError):
    """Input cannot become a valid NetBird payload."""


def _ids(items: Iterable[Any] | None) -> list[str]:
    return [str(i["id"]) if isinstance(i, Mapping) else str(i) for i in items or ()]


def _resource(ref: Mapping[str, Any] | None) -> Json | None:
    if not ref or not ref.get("id"):
        return None
    return {"id": str(ref["id"]), "type": str(ref["type"])}


def rule_for_put(rule: Mapping[str, Any]) -> Json:
    """Reduce a GET rule to the shape the PUT endpoint expects."""
    out: Json = {
        "name": rule["name"],
        "description": rule.get("description") or "",
        "enabled": rule.get("enabled", True),
        "action": rule["action"],
        "protocol": rule["protocol"],
        "bidirectional": rule.get("bidirectional", False),
    }
    if rule.get("id"):
        out["id"] = rule["id"]
    for side in ("source", "destination"):
        ref = _resource(rule.get(f"{side}Resource") or rule.get(f"{side}_resource"))
        if ref:
            out[f"{side}Resource"] = ref
        else:
            out[f"{side}s"] = _ids(rule.get(f"{side}s"))
    if rule.get("ports"):
        out["ports"] = [str(p) for p in rule["ports"]]
    if rule.get("port_ranges"):
        out["port_ranges"] = [
            {"start": int(r["start"]), "end": int(r["end"])} for r in rule["port_ranges"]
        ]
    if rule.get("authorized_groups"):
        out["authorized_groups"] = rule["authorized_groups"]
    return out


def policy_for_put(policy: Mapping[str, Any], **overrides: Any) -> Json:
    """Full PUT body for an existing policy, with optional field overrides."""
    body: Json = {
        "name": policy["name"],
        "description": policy.get("description") or "",
        "enabled": policy.get("enabled", True),
        "source_posture_checks": _ids(policy.get("source_posture_checks")),
        "rules": [rule_for_put(r) for r in policy.get("rules") or ()],
    }
    return {**body, **overrides}


_TOKEN = re.compile(r"^(\d+)(?:-(\d+))?$")


def _port(value: str, text: str) -> int:
    n = int(value)
    if not 1 <= n <= 65535:
        raise PayloadError(f"port out of range in {text!r}: {n}")
    return n


def parse_ports(text: str) -> tuple[list[str], list[Json]]:
    """`"22, 443, 8000-8099"` -> (["22", "443"], [{"start": 8000, "end": 8099}])."""
    ports: list[str] = []
    ranges: list[Json] = []
    for token in re.split(r"[\s,]+", text.strip()):
        if not token:
            continue
        m = _TOKEN.match(token)
        if not m:
            raise PayloadError(f"invalid port {token!r}")
        start = _port(m.group(1), token)
        if m.group(2) is None:
            ports.append(str(start))
            continue
        end = _port(m.group(2), token)
        if end < start:
            raise PayloadError(f"port range {token!r} ends before it starts")
        ranges.append({"start": start, "end": end})
    return ports, ranges


def build_rule(
    *,
    name: str,
    sources: Iterable[str] = (),
    destinations: Iterable[str] = (),
    destination_resource: Mapping[str, Any] | None = None,
    protocol: str = "all",
    ports: str = "",
    bidirectional: bool = False,
    action: str = "accept",
    enabled: bool = True,
    description: str = "",
    rule_id: str | None = None,
) -> Json:
    """Validated rule dict from editor input."""
    if not name.strip():
        raise PayloadError("rule name is required")
    if protocol not in PROTOCOLS:
        raise PayloadError(f"unknown protocol {protocol!r}")
    if action not in ACTIONS:
        raise PayloadError(f"unknown action {action!r}")
    src = list(dict.fromkeys(s for s in sources if s))
    if not src:
        raise PayloadError("at least one source group is required")
    dst_ref = _resource(destination_resource)
    dst = list(dict.fromkeys(d for d in destinations if d))
    if not dst and dst_ref is None:
        raise PayloadError("a destination group or resource is required")
    port_list, ranges = parse_ports(ports)
    if (port_list or ranges) and protocol not in PORTED:
        raise PayloadError(f"ports only apply to tcp/udp, not {protocol}")

    rule: Json = {
        "name": name.strip(),
        "description": description,
        "enabled": enabled,
        "action": action,
        "protocol": protocol,
        "bidirectional": bidirectional,
        "sources": src,
    }
    if rule_id:
        rule["id"] = rule_id
    if dst_ref is not None:
        rule["destinationResource"] = dst_ref
    else:
        rule["destinations"] = dst
    if port_list:
        rule["ports"] = port_list
    if ranges:
        rule["port_ranges"] = ranges
    return rule


def group_payload(
    name: str, peer_ids: Iterable[str], resources: Iterable[Mapping[str, Any]] = ()
) -> Json:
    """Full group body. PUT replaces membership, so callers pass everything."""
    if not name.strip():
        raise PayloadError("group name is required")
    return {
        "name": name.strip(),
        "peers": sorted(set(peer_ids)),
        "resources": [r for r in (_resource(x) for x in resources) if r],
    }


# Roles a user can be given here. Ownership is transferred in NetBird itself.
ROLES = ("admin", "network_admin", "user", "auditor", "billing_admin")


def user_payload(
    fresh: Mapping[str, Any],
    auto_groups: Iterable[str],
    *,
    role: str | None = None,
    is_blocked: bool | None = None,
) -> Json:
    """Full user PUT body. NetBird requires role and block state on every
    update; whatever is not given is carried over from a fresh GET."""
    current = str(fresh.get("role") or "")
    if not current:
        raise PayloadError(f"user {fresh.get('id')!r} has no role")
    new_role = current if role is None else role
    if new_role != current and (new_role not in ROLES or current not in ROLES):
        raise PayloadError(f"role {current!r} cannot be changed to {new_role!r} here")
    blocked = bool(fresh.get("is_blocked", False)) if is_blocked is None else is_blocked
    return {"role": new_role, "auto_groups": sorted(set(auto_groups)), "is_blocked": blocked}


def peer_payload(
    fresh: Mapping[str, Any],
    *,
    name: str | None = None,
    ssh_enabled: bool | None = None,
    login_expiration_enabled: bool | None = None,
    approved: bool | None = None,
) -> Json:
    """Full peer PUT body. NetBird requires every setting on each update;
    whatever is not given is carried over from a fresh GET. `approved=True`
    clears `approval_required` (only meaningful where approval is on)."""

    def keep(field: str, value: bool | None) -> bool:
        return bool(fresh.get(field, False)) if value is None else value

    new_name = str(fresh.get("name") or "") if name is None else name.strip()
    if not new_name:
        raise PayloadError("peer name is required")
    body: Json = {
        "name": new_name,
        "ssh_enabled": keep("ssh_enabled", ssh_enabled),
        "login_expiration_enabled": keep("login_expiration_enabled", login_expiration_enabled),
        "inactivity_expiration_enabled": keep("inactivity_expiration_enabled", None),
    }
    if approved:
        body["approval_required"] = False
    return body


SETUP_KEY_TYPES = ("reusable", "one-off")
MAX_KEY_DAYS = 365


def setup_key_create_payload(
    *,
    name: str,
    type: str,
    expires_days: int,
    usage_limit: int,
    auto_groups: Iterable[str],
    ephemeral: bool = False,
) -> Json:
    """`POST /setup-keys` body. Keys always expire: no "never" from this UI."""
    if not name.strip():
        raise PayloadError("setup key name is required")
    if type not in SETUP_KEY_TYPES:
        raise PayloadError(f"unknown setup key type {type!r}")
    if not 1 <= expires_days <= MAX_KEY_DAYS:
        raise PayloadError(f"expiry must be 1 to {MAX_KEY_DAYS} days")
    if usage_limit < 0:
        raise PayloadError("usage limit cannot be negative")
    return {
        "name": name.strip(),
        "type": type,
        "expires_in": expires_days * 86400,
        "usage_limit": usage_limit,
        "auto_groups": list(dict.fromkeys(g for g in auto_groups if g)),
        "ephemeral": ephemeral,
        "allow_extra_dns_labels": False,
    }


def setup_key_update_payload(
    fresh: Mapping[str, Any],
    *,
    auto_groups: Iterable[str] | None = None,
    revoked: bool | None = None,
) -> Json:
    """`PUT /setup-keys/{id}` body: only auto-groups and revoked can change."""
    was = bool(fresh.get("revoked", False))
    if was and revoked is False:
        raise PayloadError("a revoked setup key cannot be reactivated")
    groups = _ids(fresh.get("auto_groups")) if auto_groups is None else list(auto_groups)
    return {
        "auto_groups": sorted(set(groups)),
        "revoked": was if revoked is None else revoked,
    }
