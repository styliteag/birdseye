"""Finding and check types shared by the anomaly modules."""

from __future__ import annotations

from dataclasses import dataclass

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


@dataclass(frozen=True)
class Finding:
    check: str
    severity: str  # "error" | "warning" | "info"
    title: str
    detail: str = ""
    link: str = ""  # page to fix it
    group_id: str = ""  # the group the finding is about, if any
    peer_id: str = ""  # the peer the finding is about, if any (links its editor)
    resource_id: str = ""  # likewise for a network resource


@dataclass(frozen=True)
class Check:
    key: str
    label: str
    explain: str
