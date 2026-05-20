"""Map NetBird audit-event initiator IDs to human-readable labels.

NetBird's `/events/audit` endpoint returns events whose `initiator_id` can
point at a setup-key or a service entity. For those cases the server leaves
`initiator_name`/`initiator_email` empty (and logs a noisy WARN). Building
this resolver once at startup gives a useful display label instead.

Note: the resolver is a startup snapshot. Setup-keys or users added after
process start are not picked up until the process is restarted; an unresolved
ID falls back to `id:<prefix>`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from netbird import APIClient

Json = dict[str, Any]


@dataclass(frozen=True)
class InitiatorResolver:
    users_by_id: dict[str, str]
    keys_by_id: dict[str, str]


def build_initiator_resolver(client: APIClient) -> InitiatorResolver:
    users = client.get("users") or []
    keys = client.get("setup-keys") or []
    users_by_id = {
        u["id"]: (u.get("name") or u.get("email") or u["id"]) for u in users if u.get("id")
    }
    keys_by_id = {k["id"]: (k.get("name") or k["id"]) for k in keys if k.get("id")}
    return InitiatorResolver(users_by_id=users_by_id, keys_by_id=keys_by_id)


def resolve_initiator(event: Json, resolver: InitiatorResolver) -> str:
    name = event.get("initiator_name") or event.get("initiator_email")
    if name:
        return name
    iid = (event.get("initiator_id") or "").strip()
    if not iid or iid == "sys":
        return "system"
    if iid in resolver.keys_by_id:
        return f"setup-key:{resolver.keys_by_id[iid]}"
    if iid in resolver.users_by_id:
        return resolver.users_by_id[iid]
    return f"id:{iid[:12]}"
