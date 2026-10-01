"""NetBird audit events (`GET /events/audit`): parse, resolve, filter.

The events are not part of the cached snapshot: they change with every
write and are only read on the audit page. IDs are resolved to names through
the snapshot; a deleted object falls back to the name NetBird stored in the
event's meta.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from birdseye_web.models import Snapshot, parse_time

# activity_code prefix -> (kind, editor URL prefix); longest prefix first.
_TARGETS = (
    ("network.resource", "resource", "/resources/"),
    ("network.router", "", ""),
    ("network", "network", "/networks/"),
    ("setupkey", "setup_key", "/setup-keys/"),
    ("service.user", "user", "/users/"),
    ("user", "user", "/users/"),
    ("peer", "peer", "/peers/"),
    ("group", "group", "/groups/"),
    ("policy", "policy", "/policies/"),
)


@dataclass(frozen=True)
class AuditEvent:
    id: int
    timestamp: datetime | None
    activity: str
    activity_code: str
    initiator_id: str
    initiator_name: str
    initiator_email: str
    target_id: str
    meta: tuple[tuple[str, str], ...] = ()

    @property
    def category(self) -> str:
        return self.activity_code.split(".", 1)[0]

    def meta_get(self, key: str) -> str:
        return next((v for k, v in self.meta if k == key), "")


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_event(raw: Mapping[str, Any]) -> AuditEvent:
    meta = raw.get("meta") or {}
    return AuditEvent(
        id=_int(raw.get("id")),
        timestamp=parse_time(raw.get("timestamp")),
        activity=str(raw.get("activity") or ""),
        activity_code=str(raw.get("activity_code") or ""),
        initiator_id=str(raw.get("initiator_id") or ""),
        initiator_name=str(raw.get("initiator_name") or ""),
        initiator_email=str(raw.get("initiator_email") or ""),
        target_id=str(raw.get("target_id") or ""),
        meta=tuple(sorted((str(k), str(v)) for k, v in meta.items()))
        if isinstance(meta, Mapping)
        else (),
    )


def initiator_label(e: AuditEvent, snap: Snapshot) -> str:
    """Who did it. NetBird leaves name/email empty for setup keys and system."""
    if e.initiator_name or e.initiator_email:
        return e.initiator_name or e.initiator_email
    if not e.initiator_id or e.initiator_id == "sys":
        return "system"
    if e.initiator_id in snap.setup_keys:
        return f"setup key {snap.setup_keys[e.initiator_id].name}"
    if e.initiator_id in snap.users:
        return snap.users[e.initiator_id].label
    return e.initiator_id


def _target_kind(code: str) -> tuple[str, str]:
    for prefix, kind, url in _TARGETS:
        if code == prefix or code.startswith(prefix + "."):
            return kind, url
    return "", ""


def _lookup(snap: Snapshot, kind: str, oid: str) -> str | None:
    tables: dict[str, Mapping[str, Any]] = {
        "resource": snap.resources,
        "network": snap.networks,
        "setup_key": snap.setup_keys,
        "peer": snap.peers,
        "group": snap.groups,
        "policy": snap.policies,
    }
    if kind == "user":
        u = snap.users.get(oid)
        return u.label if u else None
    obj = tables.get(kind, {}).get(oid)
    return obj.name if obj is not None else None


def target_info(e: AuditEvent, snap: Snapshot) -> tuple[str, str]:
    """(label, editor link). The link is empty when the object is gone or has no editor."""
    if not e.target_id:
        return "", ""
    kind, url = _target_kind(e.activity_code)
    name = _lookup(snap, kind, e.target_id) if kind else None
    if name is not None:
        return name, f"{url}{e.target_id}"
    meta_name = e.meta_get("name")
    if meta_name:
        return f"{meta_name} (deleted)", ""
    return e.target_id, ""


def _date(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


@dataclass(frozen=True)
class AuditQuery:
    initiator: str = ""  # initiator ID
    activity: str = ""  # exact activity_code, or a category such as "policy"
    target: str = ""  # target ID
    since: date | None = None
    until: date | None = None  # inclusive
    q: str = ""

    @classmethod
    def from_params(cls, params: Mapping[str, str]) -> AuditQuery:
        return cls(
            initiator=params.get("initiator", "").strip(),
            activity=params.get("activity", "").strip(),
            target=params.get("target", "").strip(),
            since=_date(params.get("since", "")),
            until=_date(params.get("until", "")),
            q=params.get("q", "").strip(),
        )


def _day(e: AuditEvent) -> date | None:
    return e.timestamp.date() if e.timestamp else None


def _text(e: AuditEvent, snap: Snapshot) -> str:
    target = target_info(e, snap)[0]
    meta = " ".join(v for _, v in e.meta)
    return f"{e.activity} {e.activity_code} {initiator_label(e, snap)} {target} {meta}".lower()


def _matches(e: AuditEvent, q: AuditQuery, snap: Snapshot) -> bool:
    if q.initiator and e.initiator_id != q.initiator:
        return False
    if q.activity and q.activity not in (e.activity_code, e.category):
        return False
    if q.target and e.target_id != q.target:
        return False
    day = _day(e)
    if q.since and (day is None or day < q.since):
        return False
    if q.until and (day is None or day > q.until):
        return False
    return not q.q or q.q.lower() in _text(e, snap)


_NEVER = datetime.min.replace(tzinfo=UTC)


def filter_events(events: Iterable[AuditEvent], q: AuditQuery, snap: Snapshot) -> list[AuditEvent]:
    """Matching events, newest first."""
    hits = [e for e in events if _matches(e, q, snap)]
    return sorted(hits, key=lambda e: (e.timestamp or _NEVER, e.id), reverse=True)


def activity_options(events: Iterable[AuditEvent]) -> tuple[list[str], list[str]]:
    """(categories, activity codes) present in the events, for the filter."""
    codes = sorted({e.activity_code for e in events if e.activity_code})
    return sorted({c.split(".", 1)[0] for c in codes}), codes


MATCH_WINDOW_S = 30


def via_birdseye(e: AuditEvent, writes: Iterable[Any]) -> bool:
    """Did this event come from a write made here? Same user, within
    MATCH_WINDOW_S, and the write names the event's target (when it has one).
    `writes` are `context.WriteRecord`s."""
    if e.timestamp is None:
        return False
    for w in writes:
        if w.user_id != e.initiator_id:
            continue
        if abs((w.at - e.timestamp).total_seconds()) > MATCH_WINDOW_S:
            continue
        if not e.target_id or e.target_id in w.what.split():
            return True
        # a create is logged before NetBird has assigned the new object's ID
        if w.what.startswith("create ") and e.activity_code.endswith((".add", ".create")):
            return True
    return False
