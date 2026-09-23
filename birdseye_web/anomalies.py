"""Configuration smells: things that work, but not the way the model intends.

NetBird's model is "peers belong to groups, policies connect groups". The
checks below flag where a setup leaves that model (direct peer targets,
devices whose groups drifted from their user's default) or where a policy
cannot do what it says (empty or missing groups, resources nobody routes).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from birdseye_web.access import group_members
from birdseye_web.models import Snapshot

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


@dataclass(frozen=True)
class Finding:
    check: str
    severity: str  # "error" | "warning" | "info"
    title: str
    detail: str = ""
    link: str = ""  # page to fix it
    group_id: str = ""  # the group the finding is about, if any


@dataclass(frozen=True)
class Check:
    key: str
    label: str
    explain: str


CHECKS = (
    Check(
        "missing-group",
        "Policy references a missing group",
        "The rule names a group ID that no longer exists; that side matches nothing.",
    ),
    Check(
        "direct-peer",
        "Rule targets a single peer",
        "Source or destination is one peer instead of a group, so access does not follow "
        "group membership. Replace it with a (one-member) group.",
    ),
    Check(
        "direct-resource",
        "Rule targets a network resource directly",
        "The usual NetBird Networks pattern; listed so you can see which policies bypass "
        "resource groups.",
    ),
    Check(
        "device-deviation",
        "Device groups differ from the user's default",
        "The peer's groups are not the auto-groups of its user: groups were added or "
        "removed by hand, or the user's defaults changed after the device joined.",
    ),
    Check(
        "empty-group",
        "Policy uses an empty group",
        "A group in the rule has no peers or resources, so that part of the rule does nothing.",
    ),
    Check(
        "no-router",
        "Resource without an active router",
        "No enabled routing peer serves this network, so policies to it cannot work.",
    ),
    Check(
        "unreachable-resource",
        "Resource no policy reaches",
        "The resource is routed, but no enabled policy targets it – neither directly nor "
        "through one of its groups – so nobody can use it.",
    ),
    Check(
        "only-all",
        "Peer only in “All”",
        "The peer is in no group of its own, so only rules on “All” apply to it.",
    ),
    Check(
        "unused-group",
        "Unused group",
        "The group is in no policy, no user's or setup key's auto-groups, and routes nothing.",
    ),
    Check("disabled", "Disabled policy or rule", "Kept in the configuration, but grants nothing."),
)


def _group_name(snap: Snapshot, gid: str) -> str:
    g = snap.groups.get(gid)
    return g.name if g else gid


def _policy_group_refs(snap: Snapshot):
    for pol in snap.policies.values():
        for r in pol.rules:
            for gid in (*r.source_group_ids, *r.destination_group_ids):
                yield pol, r, gid


def _missing_groups(snap: Snapshot) -> Iterator[Finding]:
    seen = set()
    for pol, _, gid in _policy_group_refs(snap):
        if gid not in snap.groups and (pol.id, gid) not in seen:
            seen.add((pol.id, gid))
            yield Finding(
                "missing-group",
                "error",
                f"{pol.name}: group {gid} does not exist",
                link=f"/policies/{pol.id}",
            )


def _direct_targets(snap: Snapshot) -> Iterator[Finding]:
    for pol in snap.policies.values():
        for r in pol.rules:
            for side, ref in (("source", r.source_ref), ("destination", r.destination_ref)):
                if ref is None:
                    continue
                if ref.kind == "peer":
                    check, severity, what = "direct-peer", "warning", "peer"
                else:
                    check, severity, what = "direct-resource", "info", "resource"
                yield Finding(
                    check,
                    severity,
                    f"{pol.name}: {side} is {what} {snap.endpoint_name(ref)}",
                    link=f"/policies/{pol.id}",
                )


def _device_deviation(snap: Snapshot) -> Iterator[Finding]:
    # Router peer groups are infrastructure, not something a user's default decides.
    routers = {gid for n in snap.networks.values() for gid in n.router_group_ids}
    managed = {
        g.id
        for g in snap.groups.values()
        if g.issued == "api" and not g.is_all and g.id not in routers
    }
    for p in sorted(snap.peers.values(), key=lambda p: p.name.lower()):
        u = snap.users.get(p.user_id)
        if u is None or u.is_service_user:
            continue
        have = p.group_ids & managed
        want = u.auto_groups & managed
        missing, extra = want - have, have - want
        if not missing and not extra:
            continue
        parts = []
        if missing:
            parts.append("missing: " + ", ".join(sorted(_group_name(snap, g) for g in missing)))
        if extra:
            parts.append("extra: " + ", ".join(sorted(_group_name(snap, g) for g in extra)))
        yield Finding(
            "device-deviation",
            "warning",
            f"{p.name} (user {u.label})",
            detail="; ".join(parts),
            link=f"/reach?src=p:{p.id}",
        )


def _empty_groups(snap: Snapshot) -> Iterator[Finding]:
    seen = set()
    for pol, _, gid in _policy_group_refs(snap):
        if gid in snap.groups and not group_members(snap, gid) and (pol.id, gid) not in seen:
            seen.add((pol.id, gid))
            yield Finding(
                "empty-group",
                "warning",
                f"{_group_name(snap, gid)} is empty (used in {pol.name})",
                link=f"/groups/{gid}",
                group_id=gid,
            )


def _no_router(snap: Snapshot) -> Iterator[Finding]:
    for res in sorted(snap.resources.values(), key=lambda r: r.name.lower()):
        net = snap.networks.get(res.network_id)
        if res.enabled and net is not None and not (net.router_peer_ids or net.router_group_ids):
            yield Finding(
                "no-router", "warning", f"{res.name} ({res.address})", detail=f"network {net.name}"
            )


def _only_all(snap: Snapshot) -> Iterator[Finding]:
    all_ids = {g.id for g in snap.groups.values() if g.is_all}
    for p in sorted(snap.peers.values(), key=lambda p: p.name.lower()):
        if not (p.group_ids - all_ids):
            yield Finding("only-all", "info", f"{p.name} is only in “All”")


def _targeted_resources(snap: Snapshot) -> frozenset[str]:
    """Resources some enabled rule can reach, directly or through a group."""
    hit: set[str] = set()
    for pol in snap.policies.values():
        if not pol.enabled:
            continue
        for r in pol.rules:
            if not r.enabled:
                continue
            refs = [x for x in (r.destination_ref, r.source_ref) if x and x.kind == "resource"]
            hit |= {x.id for x in refs}
            for gid in (*r.destination_group_ids, *r.source_group_ids):
                hit |= {m.id for m in group_members(snap, gid) if m.kind == "resource"}
    return frozenset(hit)


def _unreachable_resources(snap: Snapshot) -> Iterator[Finding]:
    targeted = _targeted_resources(snap)
    for res in sorted(snap.resources.values(), key=lambda r: r.name.lower()):
        if res.enabled and res.id not in targeted:
            yield Finding("unreachable-resource", "warning", f"{res.name} ({res.address})")


def _unused_groups(snap: Snapshot) -> Iterator[Finding]:
    used = {gid for _, _, gid in _policy_group_refs(snap)}
    used |= {g for u in snap.users.values() for g in u.auto_groups}
    used |= snap.setup_key_group_ids
    used |= {g for n in snap.networks.values() for g in n.router_group_ids}
    targeted = _targeted_resources(snap)
    for g in sorted(snap.groups.values(), key=lambda g: g.name.lower()):
        if g.id in used or g.is_all:
            continue
        # A Networks resource group whose resources policies already reach
        # directly is bookkeeping, not dead weight (NetBird wants every resource
        # in a group). Resources nobody reaches get their own finding.
        res = {m.id for m in group_members(snap, g.id) if m.kind == "resource"}
        if res and res <= targeted and not g.peer_ids:
            continue
        n = len(g.peer_ids) + len(g.resource_ids)
        yield Finding(
            "unused-group", "info", f"{g.name} ({n} members)", link=f"/groups/{g.id}", group_id=g.id
        )


def _disabled(snap: Snapshot) -> Iterator[Finding]:
    for pol in sorted(snap.policies.values(), key=lambda p: p.name.lower()):
        if not pol.enabled:
            yield Finding("disabled", "info", f"{pol.name} (policy)", link=f"/policies/{pol.id}")
            continue
        for r in pol.rules:
            if not r.enabled:
                yield Finding(
                    "disabled", "info", f"{pol.name} / {r.name} (rule)", link=f"/policies/{pol.id}"
                )


def find_anomalies(snap: Snapshot, ignore: re.Pattern[str] | None = None) -> tuple[Finding, ...]:
    """All findings, most severe first. `ignore` matches group names to skip."""
    ignored = {g.id for g in snap.groups.values() if ignore and ignore.search(g.name)}
    found = [
        *_missing_groups(snap),
        *_direct_targets(snap),
        *_device_deviation(snap),
        *_empty_groups(snap),
        *_no_router(snap),
        *_unreachable_resources(snap),
        *_only_all(snap),
        *_unused_groups(snap),
        *_disabled(snap),
    ]
    kept = [f for f in found if f.group_id not in ignored]
    return tuple(sorted(kept, key=lambda f: SEVERITY_ORDER[f.severity]))
