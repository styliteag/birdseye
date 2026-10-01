"""Audit log page: NetBird's audit events, filtered and linked to the editors.

Fetched on every page view (no cache): it is the record of what just
changed, so it must not lag behind a write.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from birdseye_web.audit import (
    AuditQuery,
    activity_options,
    filter_events,
    initiator_label,
    parse_event,
    target_info,
    via_birdseye,
)
from birdseye_web.context import (
    RECENT_WRITES,
    TEMPLATES,
    current_session,
    snapshot,
    user_api,
)
from birdseye_web.models import Snapshot
from birdseye_web.nbapi import NetBirdAPI, NetBirdError
from birdseye_web.sessions import Session

router = APIRouter(prefix="/audit")
PAGE_SIZE = 100


def _page(value: str) -> int:
    try:
        return max(1, int(value))
    except ValueError:
        return 1


@router.get("")
async def audit_page(
    request: Request,
    s: Session = Depends(current_session),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    try:
        raw = await api.get("events/audit") or []
    except NetBirdError as exc:
        if not exc.forbidden:
            raise
        return TEMPLATES.TemplateResponse(
            request,
            "forbidden.html",
            {"session": s, "message": "Your NetBird role may not read the audit log."},
            status_code=403,
        )
    events = [parse_event(e) for e in raw]
    q = AuditQuery.from_params(request.query_params)
    hits = filter_events(events, q, snap)
    page = _page(request.query_params.get("page", "1"))
    shown = hits[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
    categories, codes = activity_options(events)
    initiators = sorted(
        {(e.initiator_id, initiator_label(e, snap)) for e in events if e.initiator_id},
        key=lambda x: x[1].lower(),
    )
    writes = list(RECENT_WRITES)
    rows = [
        {
            "e": e,
            "who": initiator_label(e, snap),
            "target": target_info(e, snap),
            "ours": via_birdseye(e, writes),
        }
        for e in shown
    ]
    params = {k: v for k, v in request.query_params.items() if k != "page"}
    tpl = "_audit_rows.html" if request.headers.get("HX-Target") == "audit-body" else "audit.html"
    return TEMPLATES.TemplateResponse(
        request,
        tpl,
        {
            "session": s,
            "rows": rows,
            "total": len(hits),
            "page": page,
            "pages": max(1, -(-len(hits) // PAGE_SIZE)),
            "params": params,
            "query": q,
            "categories": categories,
            "codes": codes,
            "initiators": initiators,
            "target_label": target_info_for(q.target, events, snap),
        },
    )


def target_info_for(target: str, events, snap: Snapshot) -> str:
    """Name of the object the page is filtered to, for the heading."""
    if not target:
        return ""
    for e in events:
        if e.target_id == target:
            return target_info(e, snap)[0]
    return snap.names().get(target, target)
