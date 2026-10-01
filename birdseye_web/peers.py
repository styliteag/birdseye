"""Peer list filters and a peer's effective access. Pure; routes do HTTP."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from birdseye_web.access import edges, grants
from birdseye_web.models import Peer, Ref, Snapshot, User

STATUSES = ("online", "offline")
NO_USER = "-"


@dataclass(frozen=True)
class PeerQuery:
    q: str = ""
    status: str = ""  # "" | "online" | "offline"
    user: str = ""  # user ID, NO_USER for peers without a user, "" for any
    group: str = ""
    os: str = ""
    expired: bool = False

    @classmethod
    def from_params(cls, params: Mapping[str, str]) -> PeerQuery:
        status = params.get("status", "")
        return cls(
            q=params.get("q", "").strip(),
            status=status if status in STATUSES else "",
            user=params.get("user", "").strip(),
            group=params.get("group", "").strip(),
            os=params.get("os", "").strip().lower(),
            expired=params.get("expired") in ("1", "on", "true"),
        )


def os_family(peer: Peer) -> str:
    """First word of the OS string, lower case: `linux`, `windows`, `darwin`, …"""
    return peer.os.split(" ", 1)[0].lower() if peer.os else ""


def _text(snap: Snapshot, p: Peer) -> str:
    u = snap.users.get(p.user_id)
    who = f"{u.name} {u.email}" if u else ""
    return f"{p.name} {p.hostname} {p.ip} {p.os} {p.dns_label} {p.version} {who}".lower()


def _matches(snap: Snapshot, p: Peer, q: PeerQuery) -> bool:
    if q.q and q.q.lower() not in _text(snap, p):
        return False
    if q.status and p.connected != (q.status == "online"):
        return False
    if q.user == NO_USER and p.user_id in snap.users:
        return False
    if q.user and q.user != NO_USER and p.user_id != q.user:
        return False
    if q.group and q.group not in p.group_ids:
        return False
    if q.os and os_family(p) != q.os:
        return False
    return not q.expired or p.login_expired


_NEVER = datetime.min.replace(tzinfo=UTC)


def filter_peers(snap: Snapshot, q: PeerQuery) -> list[Peer]:
    """Matching peers, last seen first; online peers count as seen now."""
    hits = [p for p in snap.peers.values() if _matches(snap, p, q)]
    return sorted(
        hits, key=lambda p: (not p.connected, -(p.last_seen or _NEVER).timestamp(), p.name.lower())
    )


def user_options(snap: Snapshot) -> list[User]:
    """Users that own at least one peer, for the filter drop-down."""
    owners = {p.user_id for p in snap.peers.values()}
    return sorted((u for u in snap.users.values() if u.id in owners), key=lambda u: u.label.lower())


def os_options(snap: Snapshot) -> list[str]:
    return sorted({os_family(p) for p in snap.peers.values()} - {""})


@dataclass(frozen=True)
class Counterpart:
    """Everything one peer may reach (or be reached by) on one other endpoint."""

    ref: Ref
    services: tuple[str, ...]
    policy_ids: tuple[str, ...]


@dataclass(frozen=True)
class PeerAccess:
    outgoing: tuple[Counterpart, ...]
    incoming: tuple[Counterpart, ...]


def _fold(pairs: Iterable[tuple[Ref, str, str]], snap: Snapshot) -> tuple[Counterpart, ...]:
    services: dict[Ref, set[str]] = {}
    policies: dict[Ref, set[str]] = {}
    for ref, service, pid in pairs:
        services.setdefault(ref, set()).add(service)
        policies.setdefault(ref, set()).add(pid)
    return tuple(
        Counterpart(ref, tuple(sorted(services[ref])), tuple(sorted(policies[ref])))
        for ref in sorted(services, key=lambda r: (r.kind != "peer", snap.endpoint_name(r).lower()))
    )


def peer_access(snap: Snapshot, peer_id: str) -> PeerAccess:
    """What the peer can reach and who can reach it, from the shared edges."""
    me = Ref("peer", peer_id)
    gl = grants(snap)
    out = (
        (e.dst, e.grant.service.label(), e.grant.policy_id) for e in edges(snap, gl, sources={me})
    )
    inc = (
        (e.src, e.grant.service.label(), e.grant.policy_id)
        for e in edges(snap, gl, destinations={me})
    )
    return PeerAccess(outgoing=_fold(out, snap), incoming=_fold(inc, snap))


def editable_groups(snap: Snapshot) -> frozenset[str]:
    """Groups an editor may add or remove; IdP/integration groups are synced."""
    return frozenset(g.id for g in snap.groups.values() if g.issued == "api" and not g.is_all)


def group_edit(
    current: frozenset[str],
    selected: Iterable[str],
    initial: Iterable[str],
    editable: frozenset[str],
) -> tuple[frozenset[str], frozenset[str]]:
    """(add, remove): the change the user made, not the whole form, so a
    group someone else assigned after the page loaded is kept."""
    sel, init = frozenset(selected) & editable, frozenset(initial) & editable
    return frozenset((sel - init) - current), frozenset((init - sel) & current)
