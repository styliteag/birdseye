"""Jinja filters for compact matrix cells."""

from __future__ import annotations

from birdseye_web.matrix import Cell


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


def register(env) -> None:
    env.filters["cell_short"] = cell_short
    env.filters["cell_class"] = cell_class
