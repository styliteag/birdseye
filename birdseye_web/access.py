"""Access resolver: turn policies into who-can-reach-what.

Two layers, so every view agrees with every other:

* `grants()`  one directed permission per rule (plus its reverse when the
              rule is bidirectional), still expressed in groups/refs.
* `edges()`   grants expanded to concrete endpoints (peer or resource).

Group-level views read grants; peer, resource and user views read edges.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator
from dataclasses import dataclass

from birdseye_web.models import Ref, Rule, Service, Snapshot


@dataclass(frozen=True)
class Grant:
    policy_id: str
    rule_id: str
    src_groups: tuple[str, ...]
    src_ref: Ref | None
    dst_groups: tuple[str, ...]
    dst_ref: Ref | None
    service: Service
    action: str = "accept"
    conditional: tuple[str, ...] = ()  # posture-check IDs gating the sources
    reverse: bool = False  # generated from a bidirectional rule
    enabled: bool = True


@dataclass(frozen=True)
class Edge:
    src: Ref
    dst: Ref
    grant: Grant
    src_group: str | None  # group through which src matched, None for a direct ref
    dst_group: str | None


def _rule_grants(policy_id: str, rule: Rule, conditional: tuple[str, ...], enabled: bool):
    forward = Grant(
        policy_id=policy_id,
        rule_id=rule.id,
        src_groups=rule.source_group_ids,
        src_ref=rule.source_ref,
        dst_groups=rule.destination_group_ids,
        dst_ref=rule.destination_ref,
        service=rule.service,
        action=rule.action,
        conditional=conditional,
        enabled=enabled,
    )
    yield forward
    if rule.bidirectional:
        yield Grant(
            policy_id=policy_id,
            rule_id=rule.id,
            src_groups=rule.destination_group_ids,
            src_ref=rule.destination_ref,
            dst_groups=rule.source_group_ids,
            dst_ref=rule.source_ref,
            service=rule.service,
            action=rule.action,
            conditional=conditional,
            reverse=True,
            enabled=enabled,
        )


def grants(snap: Snapshot, *, include_disabled: bool = False) -> tuple[Grant, ...]:
    out: list[Grant] = []
    for policy in snap.policies.values():
        for rule in policy.rules:
            enabled = policy.enabled and rule.enabled
            if not enabled and not include_disabled:
                continue
            out.extend(_rule_grants(policy.id, rule, policy.posture_check_ids, enabled))
    return tuple(out)


def group_members(snap: Snapshot, group_id: str) -> frozenset[Ref]:
    """Peers and resources in a group, from both sides of the relation."""
    group = snap.groups.get(group_id)
    peer_ids = set(group.peer_ids) if group else set()
    peer_ids |= {p.id for p in snap.peers.values() if group_id in p.group_ids}
    res_ids = set(group.resource_ids) if group else set()
    res_ids |= {r.id for r in snap.resources.values() if group_id in r.group_ids}
    return frozenset({Ref("peer", i) for i in peer_ids} | {Ref("resource", i) for i in res_ids})


class _Members:
    """Memoised `group_members` for one snapshot."""

    def __init__(self, snap: Snapshot) -> None:
        self._snap = snap
        self._cache: dict[str, frozenset[Ref]] = {}

    def __call__(self, group_id: str) -> frozenset[Ref]:
        if group_id not in self._cache:
            self._cache[group_id] = group_members(self._snap, group_id)
        return self._cache[group_id]


def _side(groups: tuple[str, ...], ref: Ref | None, members: _Members):
    if ref is not None:
        yield ref, None
    for gid in groups:
        for member in members(gid):
            yield member, gid


def edges(
    snap: Snapshot,
    grant_list: tuple[Grant, ...],
    *,
    sources: Collection[Ref] | None = None,
    destinations: Collection[Ref] | None = None,
) -> Iterator[Edge]:
    """Expand grants to endpoint pairs.

    Resources never initiate traffic, and a peer never needs a rule to reach
    itself; both are skipped. `sources`/`destinations` narrow the expansion
    early, which keeps big peer matrices cheap.
    """
    members = _Members(snap)
    for grant in grant_list:
        dst_side = [
            (d, dg)
            for d, dg in _side(grant.dst_groups, grant.dst_ref, members)
            if destinations is None or d in destinations
        ]
        if not dst_side:
            continue
        for src, sg in _side(grant.src_groups, grant.src_ref, members):
            if src.kind != "peer" or (sources is not None and src not in sources):
                continue
            for dst, dg in dst_side:
                if dst != src:
                    yield Edge(src, dst, grant, sg, dg)


_PORTLESS = frozenset({"icmp"})
_FIXED_PORTS = {"netbird-ssh": 22}


def service_matches(service: Service, protocol: str | None, port: int | None) -> bool:
    """Does `service` allow `protocol`/`port`? `None` means "any"."""
    if service.protocol == "all":
        return True
    if protocol is not None and service.protocol != protocol:
        return False
    if port is None:
        return True
    if service.protocol in _PORTLESS:
        return False
    if service.protocol in _FIXED_PORTS:
        return port == _FIXED_PORTS[service.protocol]
    return not service.ports or any(r.start <= port <= r.end for r in service.ports)


def reach(
    snap: Snapshot,
    src: Ref,
    dst: Ref,
    *,
    protocol: str | None = None,
    port: int | None = None,
    include_disabled: bool = False,
) -> tuple[Edge, ...]:
    """All edges that let `src` reach `dst`, optionally for one protocol/port."""
    found = edges(
        snap, grants(snap, include_disabled=include_disabled), sources={src}, destinations={dst}
    )
    return tuple(e for e in found if service_matches(e.grant.service, protocol, port))
