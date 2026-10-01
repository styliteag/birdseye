"""Restore one policy or group from a history snapshot. Pure: builds the
write and a simulated snapshot for the access preview; the route writes.

A changed object is PUT back; a deleted one is created again (it gets a new
ID). References that no longer exist block a policy restore — a policy
restored without one of its groups would grant something else than before.
A group restore drops vanished peers and resources and says so.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from birdseye_web.diff import with_group_members, with_policy
from birdseye_web.models import Group, Snapshot, parse_policy
from birdseye_web.payloads import PayloadError, group_payload, policy_for_put

RESTORABLE = ("policies", "groups")


@dataclass(frozen=True)
class RestorePlan:
    method: str = ""  # "PUT" | "POST"
    path: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    after: Snapshot | None = None
    problems: tuple[str, ...] = ()  # blocking
    notes: tuple[str, ...] = ()  # informational

    @property
    def blocked(self) -> bool:
        return bool(self.problems)


def _policy_problems(snap: Snapshot, raw: Mapping[str, Any]) -> list[str]:
    pol = parse_policy(raw)
    out: list[str] = []
    for r in pol.rules:
        for gid in (*r.source_group_ids, *r.destination_group_ids):
            if gid not in snap.groups:
                out.append(f"rule {r.name!r}: group {gid} no longer exists")
        for ref in (r.source_ref, r.destination_ref):
            table = snap.peers if ref and ref.kind == "peer" else snap.resources
            if ref is not None and ref.id not in table:
                out.append(f"rule {r.name!r}: {ref.kind} {ref.id} no longer exists")
    out += [
        f"posture check {c} no longer exists"
        for c in pol.posture_check_ids
        if c not in snap.posture_checks
    ]
    return out


def _plan_policy(snap: Snapshot, raw: Mapping[str, Any]) -> RestorePlan:
    pid = str(raw.get("id") or "")
    problems = _policy_problems(snap, raw)
    body = policy_for_put(raw)
    if pid in snap.policies:
        method, path = "PUT", f"policies/{pid}"
    else:
        body = {**body, "rules": [{k: v for k, v in r.items() if k != "id"} for r in body["rules"]]}
        method, path = "POST", "policies"
    # simulated under the old ID: it is free, the object is gone
    after = None if problems else with_policy(snap, {**body, "id": pid})
    return RestorePlan(method, path, body, after, tuple(problems))


def _ids(items: Any) -> list[str]:
    return [str(i["id"]) if isinstance(i, Mapping) else str(i) for i in items or ()]


def _plan_group(snap: Snapshot, raw: Mapping[str, Any]) -> RestorePlan:
    gid, name = str(raw.get("id") or ""), str(raw.get("name") or "")
    if name == "All":
        return RestorePlan(problems=("The “All” group is managed by NetBird.",))
    peers = _ids(raw.get("peers"))
    resources = [r for r in raw.get("resources") or () if isinstance(r, Mapping)]
    kept_peers = [p for p in peers if p in snap.peers]
    kept_res = [r for r in resources if str(r.get("id")) in snap.resources]
    notes = [f"peer {p} no longer exists and is left out" for p in peers if p not in snap.peers]
    notes += [
        f"resource {r.get('id')} no longer exists and is left out"
        for r in resources
        if r not in kept_res
    ]
    try:
        body = group_payload(name, kept_peers, kept_res)
    except PayloadError as exc:
        return RestorePlan(problems=(str(exc),))
    if gid in snap.groups:
        after = with_group_members(snap, gid, kept_peers)
        return RestorePlan("PUT", f"groups/{gid}", body, after, notes=tuple(notes))
    new = Group(gid, name, frozenset(), frozenset(str(r["id"]) for r in kept_res))
    after = with_group_members(replace(snap, groups={**snap.groups, gid: new}), gid, kept_peers)
    return RestorePlan("POST", "groups", body, after, notes=tuple(notes))


def plan_restore(snap: Snapshot, slug: str, raw: Mapping[str, Any]) -> RestorePlan:
    if slug == "policies":
        return _plan_policy(snap, raw)
    if slug == "groups":
        return _plan_group(snap, raw)
    return RestorePlan(problems=("Only policies and groups can be restored here.",))
