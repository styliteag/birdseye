"""Presentation helpers for the History page: field-level changes and the
"is a newer snapshot on its way" status. Pure; the route passes data in."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Change:
    path: tuple[str, ...]
    kind: str  # "value" | "list" | "added" | "removed"
    old: Any = None
    new: Any = None
    added: tuple[Any, ...] = ()
    removed: tuple[Any, ...] = ()
    kept: tuple[Any, ...] = ()


def _objects(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, Mapping) and "id" in v for v in value)


def _label(obj: Mapping[str, Any]) -> str:
    return str(obj.get("name") or obj.get("id"))


def flat_changes(field: str, old: Any, new: Any, path: tuple[str, ...] = ()) -> list[Change]:
    """Leaf-level changes between two normalized values, e.g.
    `rules › web › ports: +443` instead of two whole JSON blobs."""
    here = (*path, field)
    if old == new:
        return []
    if isinstance(old, Mapping) and isinstance(new, Mapping):
        out: list[Change] = []
        for key in sorted(set(old) | set(new)):
            out += flat_changes(str(key), old.get(key), new.get(key), here)
        return out
    if _objects(old or []) and _objects(new or []) and (old or new):
        a = {str(o["id"]): o for o in old or []}
        b = {str(o["id"]): o for o in new or []}
        out = []
        for oid in [*a, *(k for k in b if k not in a)]:
            if oid not in b:
                out.append(Change((*here, _label(a[oid])), "removed", a[oid], None))
            elif oid not in a:
                out.append(Change((*here, _label(b[oid])), "added", None, b[oid]))
            else:
                out += flat_changes(_label(b[oid]), a[oid], b[oid], here)
        return out
    if isinstance(old or [], list) and isinstance(new or [], list):
        before, after = list(old or []), list(new or [])
        return [
            Change(
                here,
                "list",
                old,
                new,
                added=tuple(x for x in after if x not in before),
                removed=tuple(x for x in before if x not in after),
                kept=tuple(x for x in after if x in before),
            )
        ]
    return [Change(here, "value", old, new)]


@dataclass(frozen=True)
class SnapshotStatus:
    state: str  # "current" | "pending" | "running" | "unscheduled"
    changed_at: datetime | None = None


def snapshot_status(
    change_times: Iterable[datetime],
    newest: datetime | None,
    *,
    job_enabled: bool,
    running: bool = False,
    last_run: datetime | None = None,
) -> SnapshotStatus:
    """Is the newest snapshot behind the configuration? A run that started
    after the last change covers it even if it wrote nothing (unchanged)."""
    latest = max(change_times, default=None)
    if running:
        return SnapshotStatus("running", latest)
    covered = max((t for t in (newest, last_run) if t is not None), default=None)
    if latest is None or (covered is not None and latest <= covered):
        return SnapshotStatus("current", latest)
    return SnapshotStatus("pending" if job_enabled else "unscheduled", latest)
