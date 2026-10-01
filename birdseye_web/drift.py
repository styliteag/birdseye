"""Device groups versus their user's auto-groups.

A peer's groups should match the auto-groups of the user who owns it.
Only "managed" groups count: created through the API, not "All", and not a
network router group (infrastructure, not a user default). The Anomalies
page and the user editor both use this module, so they report and fix the
same thing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from birdseye_web.models import Snapshot


@dataclass(frozen=True)
class Drift:
    peer_id: str
    missing: frozenset[str]
    extra: frozenset[str]

    @property
    def ok(self) -> bool:
        return not self.missing and not self.extra


def managed_groups(snap: Snapshot) -> frozenset[str]:
    routers = {gid for n in snap.networks.values() for gid in n.router_group_ids}
    return frozenset(
        g.id
        for g in snap.groups.values()
        if g.issued == "api" and not g.is_all and g.id not in routers
    )


def peer_drift(snap: Snapshot, peer_id: str, want: Iterable[str] | None = None) -> Drift:
    """Missing and extra managed groups of one peer. `want` defaults to its
    user's auto-groups."""
    p = snap.peers[peer_id]
    if want is None:
        u = snap.users.get(p.user_id)
        want = u.auto_groups if u else ()
    managed = managed_groups(snap)
    have, target = p.group_ids & managed, frozenset(want) & managed
    return Drift(peer_id, target - have, have - target)


def user_drift(
    snap: Snapshot, user_id: str, auto_groups: Iterable[str] | None = None
) -> tuple[Drift, ...]:
    """Drifting peers of one user, by peer name."""
    want = None if auto_groups is None else frozenset(auto_groups)
    peers = sorted(
        (p for p in snap.peers.values() if p.user_id == user_id), key=lambda p: p.name.lower()
    )
    drifts = (peer_drift(snap, p.id, want) for p in peers)
    return tuple(d for d in drifts if not d.ok)


def alignment(
    members: Mapping[str, frozenset[str]],
    managed: Iterable[str],
    peer_ids: Iterable[str],
    want: Iterable[str],
) -> dict[str, frozenset[str]]:
    """New peer set for each managed group whose membership must change so
    that `peer_ids` are in exactly the `want` groups. Other peers stay."""
    peers, target = frozenset(peer_ids), frozenset(want)
    out: dict[str, frozenset[str]] = {}
    for gid in sorted(managed):
        now = members.get(gid, frozenset())
        new = (now - peers) | (peers if gid in target else frozenset())
        if new != now:
            out[gid] = new
    return out
