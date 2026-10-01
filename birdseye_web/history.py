"""Config history: read the dated snapshots `config_history.py` writes into
the shared jobs dir, and diff them per object.

Layout: `<jobs_dir>/history/<YYYYMMDDTHHMMSSZ>/<slug>.json`. This side only
reads. Objects are normalized before comparing: runtime state (online,
last seen, client version, usage counters) is dropped and embedded objects
are reduced to IDs, so a diff shows configuration changes only.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from birdseye_web.payloads import policy_for_put

STAMP = re.compile(r"^\d{8}T\d{6}Z$")  # same format as config_history.py
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"

# (slug, label) in display order; slugs are the file names config_history writes.
KINDS = (
    ("policies", "Policies"),
    ("groups", "Groups"),
    ("posture_checks", "Posture checks"),
    ("networks", "Networks"),
    ("users", "Users"),
    ("setup_keys", "Setup keys"),
    ("peers", "Peers"),
    ("routes", "Routes"),
    ("dns_nameservers", "DNS nameservers"),
    ("dns_settings", "DNS settings"),
    ("accounts", "Account settings"),
)
SLUGS = frozenset(s for s, _ in KINDS)

# Runtime state and counters: they change without anyone changing the config.
VOLATILE = frozenset(
    {
        "accessible_peers_count",
        "city_name",
        "connected",
        "connection_ip",
        "country_code",
        "geoname_id",
        "hostname",
        "is_current",
        "kernel_version",
        "key",  # setup keys: masked value only, never shown
        "last_login",
        "last_seen",
        "last_used",
        "login_expired",
        "os",
        "peers_count",
        "permissions",
        "resources_count",
        "routing_peers_count",
        "serial_number",
        "state",
        "status",
        "ui_version",
        "updated_at",
        "used_times",
        "valid",
        "version",
    }
)

Json = dict[str, Any]
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SnapshotInfo:
    stamp: str
    at: datetime


def _check(stamp: str, slug: str = "groups") -> None:
    if not STAMP.match(stamp) or slug not in SLUGS:
        raise ValueError("unknown snapshot or object type")


def list_snapshots(base: Path) -> list[SnapshotInfo]:
    """Finished snapshots, newest first."""
    if not base.is_dir():
        return []
    out = []
    for p in base.iterdir():
        if not (p.is_dir() and STAMP.match(p.name)):
            continue
        try:
            at = datetime.strptime(p.name, STAMP_FORMAT).replace(tzinfo=UTC)
        except ValueError:
            log.warning("history: skipping %r, not a valid date", p.name)
            continue
        out.append(SnapshotInfo(p.name, at))
    return sorted(out, key=lambda x: x.stamp, reverse=True)


@lru_cache(maxsize=4096)
def _read(path: str, mtime_ns: int) -> Any:
    """Parsed file. Snapshots never change once written, so it is cached by
    path and mtime; callers must not mutate the result."""
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        log.warning("history: cannot read %s", path)
        return None


def load_kind(base: Path, stamp: str, slug: str) -> list[Json]:
    """Objects of one type in one snapshot; a single settings object becomes
    a one-item list with the slug as its ID."""
    _check(stamp, slug)
    path = base / stamp / f"{slug}.json"
    if not path.is_file() or not path.resolve().is_relative_to(base.resolve()):
        return []
    data = _read(str(path), path.stat().st_mtime_ns)
    if isinstance(data, Mapping):
        return [{"id": slug, **data}]
    return [d for d in data if isinstance(d, Mapping)] if isinstance(data, list) else []


def _flatten(value: Any) -> Any:
    if (
        isinstance(value, list)
        and value
        and all(isinstance(v, Mapping) and "id" in v for v in value)
    ):
        return sorted(str(v["id"]) for v in value)
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return sorted(value)
    if isinstance(value, Mapping):
        return {k: _flatten(v) for k, v in value.items() if k not in VOLATILE}
    return value


def normalize(slug: str, obj: Mapping[str, Any]) -> Json:
    """Configuration-only view of one object, comparable across snapshots."""
    if slug == "policies":
        return {"id": str(obj.get("id") or ""), **policy_for_put(obj)}
    return {k: _flatten(v) for k, v in obj.items() if k not in VOLATILE and v is not None}


def label(obj: Mapping[str, Any]) -> str:
    return str(obj.get("name") or obj.get("email") or obj.get("id") or "")


@dataclass(frozen=True)
class ObjectChange:
    id: str
    name: str
    fields: tuple[tuple[str, Any, Any], ...]  # (field, old, new)


@dataclass(frozen=True)
class KindDiff:
    slug: str
    added: tuple[tuple[str, str], ...]
    removed: tuple[tuple[str, str], ...]
    changed: tuple[ObjectChange, ...]

    @property
    def changes(self) -> int:
        return len(self.added) + len(self.removed) + len(self.changed)


def field_changes(old: Json, new: Json) -> tuple[tuple[str, Any, Any], ...]:
    keys = sorted(set(old) | set(new))
    return tuple((k, old.get(k), new.get(k)) for k in keys if old.get(k) != new.get(k))


def _by_id(slug: str, objs: list[Json]) -> dict[str, Json]:
    return {str(o.get("id")): normalize(slug, o) for o in objs if o.get("id")}


def diff_kind(slug: str, old: list[Json], new: list[Json]) -> KindDiff:
    a, b = _by_id(slug, old), _by_id(slug, new)
    order = lambda pair: pair[1].lower()  # noqa: E731
    return KindDiff(
        slug,
        added=tuple(sorted(((i, label(b[i])) for i in b.keys() - a.keys()), key=order)),
        removed=tuple(sorted(((i, label(a[i])) for i in a.keys() - b.keys()), key=order)),
        changed=tuple(
            sorted(
                (
                    ObjectChange(i, label(b[i]), field_changes(a[i], b[i]))
                    for i in a.keys() & b.keys()
                    if a[i] != b[i]
                ),
                key=lambda c: c.name.lower(),
            )
        ),
    )


def diff_snapshots(base: Path, old: str, new: str) -> list[KindDiff]:
    """Every object type that changed between two snapshots, in KINDS order."""
    diffs = (diff_kind(s, load_kind(base, old, s), load_kind(base, new, s)) for s, _ in KINDS)
    return [d for d in diffs if d.changes]


def find_version(base: Path, stamp: str, oid: str) -> tuple[str, Json] | None:
    """(slug, raw object) of `oid` in one snapshot."""
    for slug, _ in KINDS:
        for obj in load_kind(base, stamp, slug):
            if str(obj.get("id")) == oid:
                return slug, obj
    return None


@dataclass(frozen=True)
class Version:
    stamp: str
    slug: str
    name: str
    event: str  # "first seen" | "changed" | "deleted"
    fields: tuple[tuple[str, Any, Any], ...] = ()


def object_timeline(base: Path, oid: str) -> list[Version]:
    """Snapshots where the object appeared, changed or vanished; newest first."""
    out: list[Version] = []
    prev: tuple[str, Json] | None = None
    for snap in reversed(list_snapshots(base)):
        found = find_version(base, snap.stamp, oid)
        if found is None:
            if prev is not None:
                out.append(Version(snap.stamp, prev[0], label(prev[1]), "deleted"))
            prev = None
            continue
        slug, obj = found
        now = normalize(slug, obj)
        if prev is None:
            out.append(Version(snap.stamp, slug, label(obj), "first seen"))
        elif prev[1] != now:
            out.append(
                Version(snap.stamp, slug, label(obj), "changed", field_changes(prev[1], now))
            )
        prev = (slug, now)
    return list(reversed(out))
