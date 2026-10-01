"""Resource list: every network resource with its groups, the policies that
reach it, routing state and how many peers can actually get there. Pure."""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass

from birdseye_web.access import edges, grants, group_members
from birdseye_web.models import Network, Policy, Ref, Resource, Snapshot

# A subnet wider than one host is flagged: least privilege prefers /32 resources.
BROAD_FROM = 2


def address_info(address: str) -> tuple[str, int | None]:
    """("host" | "subnet" | "domain" | "wildcard", address count or None)."""
    text = address.strip()
    try:
        net = ipaddress.ip_network(text, strict=False)
    except ValueError:
        return ("wildcard" if text.startswith("*") else "domain"), None
    return ("host" if net.num_addresses == 1 else "subnet"), net.num_addresses


@dataclass(frozen=True)
class ResourceQuery:
    q: str = ""
    network: str = ""
    unreached: bool = False
    broad: bool = False

    @classmethod
    def from_params(cls, params: Mapping[str, str]) -> ResourceQuery:
        flag = lambda k: params.get(k) in ("1", "on", "true")  # noqa: E731
        return cls(
            q=params.get("q", "").strip(),
            network=params.get("network", "").strip(),
            unreached=flag("unreached"),
            broad=flag("broad"),
        )


@dataclass(frozen=True)
class ResourceRow:
    resource: Resource
    network: Network | None
    kind: str
    size: int | None
    policies: tuple[Policy, ...]
    reached_by: int  # distinct peers that may reach it

    @property
    def routed(self) -> bool:
        n = self.network
        return n is not None and bool(n.router_peer_ids or n.router_group_ids)

    @property
    def broad(self) -> bool:
        return self.size is not None and self.size >= BROAD_FROM


def _policies(snap: Snapshot, rid: str) -> tuple[Policy, ...]:
    """Policies with a rule that names the resource, directly or via a group."""
    me = Ref("resource", rid)
    hits = []
    for pol in snap.policies.values():
        for r in pol.rules:
            refs = {r.source_ref, r.destination_ref}
            groups = (*r.source_group_ids, *r.destination_group_ids)
            if me in refs or any(me in group_members(snap, g) for g in groups):
                hits.append(pol)
                break
    return tuple(sorted(hits, key=lambda p: p.name.lower()))


def _reach_counts(snap: Snapshot) -> dict[str, int]:
    targets = {Ref("resource", rid) for rid in snap.resources}
    peers: dict[str, set[str]] = {}
    for e in edges(snap, grants(snap), destinations=targets):
        peers.setdefault(e.dst.id, set()).add(e.src.id)
    return {rid: len(p) for rid, p in peers.items()}


def _matches(row: ResourceRow, q: ResourceQuery) -> bool:
    r = row.resource
    net = row.network.name if row.network else ""
    if q.q and q.q.lower() not in f"{r.name} {r.address} {r.description} {net}".lower():
        return False
    if q.network and r.network_id != q.network:
        return False
    if q.unreached and row.reached_by:
        return False
    return not q.broad or row.broad


def resource_rows(snap: Snapshot, q: ResourceQuery) -> list[ResourceRow]:
    counts = _reach_counts(snap)
    rows = []
    for r in snap.resources.values():
        kind, size = address_info(r.address)
        rows.append(
            ResourceRow(
                resource=r,
                network=snap.networks.get(r.network_id),
                kind=kind,
                size=size,
                policies=_policies(snap, r.id),
                reached_by=counts.get(r.id, 0),
            )
        )
    hits = [row for row in rows if _matches(row, q)]
    return sorted(hits, key=lambda row: (row.resource.name.lower(), row.resource.id))
