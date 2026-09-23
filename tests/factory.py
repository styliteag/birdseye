"""Tiny builders for raw API dicts, so tests read like the NetBird payloads."""

from __future__ import annotations

from typing import Any

from birdseye_web.models import Snapshot, build_snapshot


def group(gid: str, name: str | None = None, peers=(), resources=()) -> dict[str, Any]:
    return {
        "id": gid,
        "name": name or gid,
        "peers": [{"id": p, "name": p} for p in peers] or None,
        "resources": [{"id": r, "type": "host"} for r in resources] or None,
    }


def peer(pid: str, groups=(), user: str = "") -> dict[str, Any]:
    return {
        "id": pid,
        "name": pid,
        "ip": "100.64.0.1",
        "user_id": user,
        "groups": [{"id": g, "name": g} for g in groups],
    }


def rule(
    src=(),
    dst=(),
    *,
    protocol: str = "all",
    ports=None,
    port_ranges=None,
    bidirectional: bool = False,
    action: str = "accept",
    enabled: bool = True,
    dst_resource: dict[str, str] | None = None,
    src_resource: dict[str, str] | None = None,
    rid: str = "r1",
) -> dict[str, Any]:
    return {
        "id": rid,
        "name": rid,
        "enabled": enabled,
        "action": action,
        "bidirectional": bidirectional,
        "protocol": protocol,
        "ports": ports,
        "port_ranges": port_ranges,
        "sources": [{"id": g, "name": g} for g in src] or None,
        "destinations": [{"id": g, "name": g} for g in dst] or None,
        "sourceResource": src_resource,
        "destinationResource": dst_resource,
    }


def policy(pid: str, *rules, enabled: bool = True, posture=()) -> dict[str, Any]:
    return {
        "id": pid,
        "name": pid,
        "enabled": enabled,
        "rules": list(rules),
        "source_posture_checks": list(posture),
    }


def user(uid: str, name: str = "", auto_groups=()) -> dict[str, Any]:
    return {
        "id": uid,
        "name": name or uid,
        "email": f"{uid}@x",
        "role": "user",
        "auto_groups": list(auto_groups),
    }


def resource(rid: str, groups=(), rtype: str = "subnet") -> dict[str, Any]:
    return {
        "id": rid,
        "name": rid,
        "address": "10.0.0.0/24",
        "type": rtype,
        "groups": [{"id": g, "name": g} for g in groups],
        "enabled": True,
    }


def snap(*, peers=(), groups=(), users=(), policies=(), resources=()) -> Snapshot:
    nets = [
        ({"id": "net1", "name": "net1"}, list(resources), [{"peer": "router", "enabled": True}])
    ]
    return build_snapshot(
        peers=peers,
        groups=groups,
        users=users,
        policies=policies,
        networks=nets if resources else (),
    )
