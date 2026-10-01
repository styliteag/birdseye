"""What a policy change does: peer-level access gained and lost."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from birdseye_web.access import edges, grants
from birdseye_web.models import Ref, Snapshot, parse_policy


@dataclass(frozen=True, order=True)
class Access:
    src: Ref
    dst: Ref
    service: str


@dataclass(frozen=True)
class Delta:
    gained: tuple[Access, ...]
    lost: tuple[Access, ...]

    @property
    def empty(self) -> bool:
        return not self.gained and not self.lost


def _access_set(snap: Snapshot) -> frozenset[Access]:
    return frozenset(
        Access(e.src, e.dst, e.grant.service.label()) for e in edges(snap, grants(snap))
    )


def access_delta(before: Snapshot, after: Snapshot) -> Delta:
    b, a = _access_set(before), _access_set(after)
    key = lambda x: (x.src.id, x.dst.id, x.service)  # noqa: E731
    return Delta(gained=tuple(sorted(a - b, key=key)), lost=tuple(sorted(b - a, key=key)))


def with_policy(snap: Snapshot, raw_policy: Mapping[str, Any]) -> Snapshot:
    """Snapshot copy with one policy added or replaced (by `id`)."""
    pol = parse_policy(raw_policy)
    return replace(snap, policies={**snap.policies, pol.id: pol})


def without_policy(snap: Snapshot, policy_id: str) -> Snapshot:
    return replace(snap, policies={k: v for k, v in snap.policies.items() if k != policy_id})


def with_group_members(snap: Snapshot, group_id: str, peer_ids: Iterable[str]) -> Snapshot:
    """Snapshot copy with a group's peer membership replaced, on both sides."""
    members = frozenset(peer_ids)
    group = snap.groups[group_id]
    peers = {
        pid: replace(
            p,
            group_ids=(p.group_ids | {group_id}) if pid in members else (p.group_ids - {group_id}),
        )
        for pid, p in snap.peers.items()
    }
    return replace(
        snap,
        groups={**snap.groups, group_id: replace(group, peer_ids=members)},
        peers=peers,
    )


def user_group_changes(
    snap: Snapshot,
    group_id: str,
    selected: Iterable[str],
    initial: Iterable[str] | None = None,
) -> tuple[frozenset[str], frozenset[str]]:
    """(added, removed) user IDs for a group's auto-assigned users.

    `initial` is what the form showed checked; with it, a user someone else
    assigned after the page loaded is left alone. Service users are never
    touched: they have no peers and the editor does not list them.
    """
    people = {uid for uid, u in snap.users.items() if not u.is_service_user}
    if initial is None:
        now = {uid for uid in people if group_id in snap.users[uid].auto_groups}
    else:
        now = set(initial) & people
    want = frozenset(selected) & people
    return frozenset(want - now), frozenset(now - want)


def with_user_auto_groups(
    snap: Snapshot, group_id: str, *, added: Iterable[str], removed: Iterable[str]
) -> Snapshot:
    """Snapshot copy after users gain or lose `group_id` as an auto-group.

    With group propagation on, NetBird moves the users' existing peers too
    (added and removed); without it only peers enrolled later are affected.
    """
    add, rem = frozenset(added), frozenset(removed)
    users = {
        uid: replace(
            u,
            auto_groups=(u.auto_groups | {group_id})
            if uid in add
            else (u.auto_groups - {group_id}),
        )
        if uid in add | rem
        else u
        for uid, u in snap.users.items()
    }
    if not snap.groups_propagation:
        return replace(snap, users=users)
    group = snap.groups[group_id]
    joins = frozenset(pid for pid, p in snap.peers.items() if p.user_id in add)
    leaves = frozenset(pid for pid, p in snap.peers.items() if p.user_id in rem)
    return replace(
        with_group_members(snap, group_id, (group.peer_ids - leaves) | joins),
        users=users,
    )


def with_user_groups(snap: Snapshot, user_id: str, auto_groups: Iterable[str]) -> Snapshot:
    """Snapshot copy after one user's auto-groups become `auto_groups`.

    Group by group, so propagation moves the user's peers exactly as
    `with_user_auto_groups` does for the group editor.
    """
    old, new = snap.users[user_id].auto_groups, frozenset(auto_groups)
    after = snap
    for gid in sorted(old ^ new):
        if gid not in snap.groups:
            continue
        add = (user_id,) if gid in new else ()
        rem = () if gid in new else (user_id,)
        after = with_user_auto_groups(after, gid, added=add, removed=rem)
    user = replace(after.users[user_id], auto_groups=new)
    return replace(after, users={**after.users, user_id: user})


def with_memberships(snap: Snapshot, members: Mapping[str, Iterable[str]]) -> Snapshot:
    """Snapshot copy with several groups' peer sets replaced."""
    after = snap
    for gid, peer_ids in members.items():
        after = with_group_members(after, gid, peer_ids)
    return after
