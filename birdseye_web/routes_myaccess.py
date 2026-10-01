"""My access: the signed-in user's own devices and what each can reach.

Admins and auditors get it from the policies (with services, like the peer
editor). Regular users cannot read policies; for them NetBird's
`GET /peers/{id}/accessible-peers` lists the peers a device may connect to —
only if the account lets regular users see their devices at all.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from birdseye_web.context import TEMPLATES, current_session, snapshot, user_api
from birdseye_web.models import Snapshot
from birdseye_web.nbapi import NetBirdAPI, NetBirdError
from birdseye_web.peers import peer_access
from birdseye_web.sessions import Session

router = APIRouter()


async def _accessible(api: NetBirdAPI, pid: str) -> list[dict] | None:
    try:
        found = await api.get(f"peers/{quote(pid, safe='')}/accessible-peers") or []
    except NetBirdError:
        return None
    return sorted(found, key=lambda p: str(p.get("name") or "").lower())


@router.get("/my-access")
async def my_access(
    request: Request,
    s: Session = Depends(current_session),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    own = sorted(
        (p for p in snap.peers.values() if p.user_id == s.user_id), key=lambda p: p.name.lower()
    )
    devices = []
    for p in own:
        if s.limited:
            devices.append({"peer": p, "peers": await _accessible(api, p.id), "access": None})
        else:
            devices.append({"peer": p, "peers": None, "access": peer_access(snap, p.id)})
    return TEMPLATES.TemplateResponse(
        request, "my_access.html", {"session": s, "snap": snap, "devices": devices}
    )
