"""Housekeeping findings: stale or unapproved peers, old clients, risky
setup keys, users without devices, unused posture checks.

Unlike the policy checks in `anomalies.py` these depend on time and on two
settings, so both come in as arguments: the module stays pure and testable.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from birdseye_web.findings import Check, Finding
from birdseye_web.models import Peer, Snapshot


@dataclass(frozen=True)
class HygieneOptions:
    stale_days: int = 30
    min_version: str = ""  # empty: compare against the newest version seen


HYGIENE_CHECKS = (
    Check(
        "login-expired",
        "Peer login expired",
        "The peer's user must sign in again (`netbird up`) before it connects.",
    ),
    Check(
        "approval-pending",
        "Peer waits for approval",
        "The peer joined but cannot connect until an admin approves it.",
    ),
    Check(
        "stale-peer",
        "Peer not seen for a long time",
        "Offline longer than WEB_STALE_PEER_DAYS. A device that is gone keeps its group "
        "memberships and access until it is deleted.",
    ),
    Check(
        "outdated-client",
        "Outdated NetBird client",
        "Older than WEB_MIN_CLIENT_VERSION, or than the newest version any peer runs.",
    ),
    Check(
        "key-no-expiry",
        "Setup key never expires",
        "Anyone who obtains the key can enroll devices for good. Revoke it and create one "
        "with an expiry.",
    ),
    Check(
        "key-unlimited",
        "Reusable setup key without usage limit",
        "The key enrolls any number of devices until it expires.",
    ),
    Check(
        "key-unused",
        "Valid setup key never used",
        "Still able to enroll a device, but nobody has used it. Revoke it if it is not needed.",
    ),
    Check(
        "key-expired",
        "Expired setup key not revoked",
        "Harmless, but clutter: revoke and delete it.",
    ),
    Check(
        "user-no-device",
        "User without any device",
        "The user has no peer. Service users and blocked users are not listed.",
    ),
    Check(
        "posture-unused",
        "Posture check not used by any policy",
        "Defined, but no policy gates its sources with it.",
    ),
)

_VERSION = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?")


def parse_version(text: str) -> tuple[int, int, int] | None:
    """`0.36.5` -> (0, 36, 5); unparseable (e.g. `development`) -> None."""
    m = _VERSION.match(text.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


def _peers(snap: Snapshot) -> list[Peer]:
    return sorted(snap.peers.values(), key=lambda p: p.name.lower())


def _peer_state(snap: Snapshot, now: datetime, opts: HygieneOptions) -> Iterator[Finding]:
    for p in _peers(snap):
        link = f"/peers/{p.id}"
        if p.login_expired:
            yield Finding("login-expired", "warning", p.name, link=link, peer_id=p.id)
        if p.approval_required:
            yield Finding("approval-pending", "warning", p.name, link=link, peer_id=p.id)
        if not p.connected and p.last_seen is not None:
            days = (now - p.last_seen).days
            if days > opts.stale_days:
                yield Finding(
                    "stale-peer",
                    "info",
                    p.name,
                    detail=f"last seen {days} days ago",
                    link=link,
                    peer_id=p.id,
                )


def _outdated(snap: Snapshot, opts: HygieneOptions) -> Iterator[Finding]:
    versions = {p.id: parse_version(p.version) for p in snap.peers.values()}
    known = [v for v in versions.values() if v is not None]
    target = parse_version(opts.min_version) if opts.min_version else max(known, default=None)
    if target is None:
        return
    label = opts.min_version or ".".join(map(str, target))
    for p in _peers(snap):
        v = versions[p.id]
        if v is not None and v < target:
            yield Finding(
                "outdated-client",
                "info",
                p.name,
                detail=f"{p.version} < {label}",
                link=f"/peers/{p.id}",
                peer_id=p.id,
            )


def _setup_keys(snap: Snapshot, now: datetime) -> Iterator[Finding]:
    for k in sorted(snap.setup_keys.values(), key=lambda k: k.name.lower()):
        state, link = k.state_at(now), f"/setup-keys/{k.id}"
        if state == "expired":
            yield Finding("key-expired", "info", k.name, link=link)
        if state != "valid":
            continue
        if k.expires is None:
            yield Finding("key-no-expiry", "warning", k.name, link=link)
        if k.reusable and not k.usage_limit:
            yield Finding("key-unlimited", "info", k.name, link=link)
        if not k.used_times:
            yield Finding("key-unused", "info", k.name, link=link)


def _users_without_device(snap: Snapshot) -> Iterator[Finding]:
    owners = {p.user_id for p in snap.peers.values()}
    for u in sorted(snap.users.values(), key=lambda u: u.label.lower()):
        if not (u.is_service_user or u.is_blocked or u.id in owners):
            yield Finding("user-no-device", "info", u.label, detail=u.role, link=f"/users/{u.id}")


def _unused_posture(snap: Snapshot) -> Iterator[Finding]:
    used = {c for pol in snap.policies.values() for c in pol.posture_check_ids}
    for c in sorted(snap.posture_checks.values(), key=lambda c: c.name.lower()):
        if c.id not in used:
            yield Finding("posture-unused", "info", c.name)


def hygiene_findings(snap: Snapshot, now: datetime, opts: HygieneOptions) -> Iterator[Finding]:
    yield from _peer_state(snap, now, opts)
    yield from _outdated(snap, opts)
    yield from _setup_keys(snap, now)
    yield from _users_without_device(snap)
    yield from _unused_posture(snap)
