"""Jinja filters: compact matrix cells, relative times, editor links."""

from __future__ import annotations

from datetime import UTC, datetime

from markupsafe import Markup, escape

from birdseye_web.matrix import Cell

# Axis/ref key prefix -> editor URL prefix (see matrix.ref_key / axis keys).
_EDITORS = {"g": "/groups/", "p": "/peers/", "u": "/users/", "r": "/resources/"}
_KINDS = {"peer": "p", "resource": "r", "group": "g", "user": "u"}


def cell_short(cell: Cell) -> str:
    """Two-to-six character badge text for a matrix cell."""
    if cell.is_all:
        return "ALL"
    labels = cell.service_labels
    if labels == ("icmp",):
        return "ping"
    if len(labels) == 1:
        label = labels[0]
        if label == "netbird-ssh":
            return "ssh"
        proto, _, ports = label.partition("/")
        if not ports:
            return proto
        return ports if len(ports) <= 6 and "," not in ports else f"{ports.split(',')[0]}+"
    return f"{len(labels)}×"


def cell_class(cell: Cell) -> str:
    parts = []
    if cell.denied:
        parts.append("c-deny")
    elif cell.is_all:
        parts.append("c-all")
    elif cell.service_labels == ("icmp",):
        parts.append("c-icmp")
    elif any(label.startswith("netbird-ssh") for label in cell.service_labels):
        parts.append("c-ssh")
    else:
        parts.append("c-ports")
    if cell.conditional:
        parts.append("c-cond")
    return " ".join(parts)


def ago(when: datetime | None, now: datetime | None = None) -> str:
    """`5m ago`, `3h ago`, `12d ago`; empty for unknown."""
    if when is None:
        return ""
    secs = int(((now or datetime.now(UTC)) - when).total_seconds())
    if secs < 60:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit} ago"
    return "just now"


_FORMATS = {"datetime": "%Y-%m-%d %H:%M:%S", "time": "%H:%M:%S", "date": "%Y-%m-%d"}


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    for parse in (
        lambda v: datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC),
        lambda v: datetime.fromisoformat(v.replace("Z", "+00:00")),
    ):
        try:
            dt = parse(value)
        except ValueError:
            continue
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


def when(value: object, fmt: str = "datetime") -> Markup:
    """A UTC timestamp as `<time>`; app.js rewrites it into the viewer's own
    time zone. The UTC text stays as the fallback without JavaScript."""
    dt = _as_datetime(value)
    if dt is None:
        return Markup("") if not value else escape(str(value))
    utc = dt.astimezone(UTC)
    iso = utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    text = utc.strftime(_FORMATS.get(fmt, _FORMATS["datetime"]))
    return Markup('<time datetime="{}" data-fmt="{}">{} UTC</time>').format(iso, fmt, text)


def obj_link(key: str) -> str:
    """Editor URL for an axis key (`p:<id>`) or `kind:<id>` (`peer:<id>`)."""
    kind, _, oid = key.partition(":")
    prefix = _EDITORS.get(_KINDS.get(kind, kind))
    return f"{prefix}{oid}" if prefix and oid else ""


def ref_link(ref) -> str:
    return obj_link(f"{ref.kind}:{ref.id}")


def register(env) -> None:
    env.filters["cell_short"] = cell_short
    env.filters["cell_class"] = cell_class
    env.filters["ago"] = ago
    env.filters["when"] = when
    env.filters["ref_link"] = ref_link
    env.globals["obj_link"] = obj_link
