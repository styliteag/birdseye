"""Anomalies page: configuration smells grouped by check."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from birdseye_web.anomalies import CHECKS, SEVERITY_ORDER, find_anomalies
from birdseye_web.context import TEMPLATES, ctx, current_session, snapshot
from birdseye_web.models import Snapshot
from birdseye_web.sessions import Session

router = APIRouter()


@router.get("/anomalies")
async def anomalies_page(
    request: Request,
    s: Session = Depends(current_session),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    level = request.query_params.get("min", "info")
    limit = SEVERITY_ORDER.get(level, 2)
    pattern = ctx(request).settings.anomaly_ignore
    everything = find_anomalies(snap, re.compile(pattern) if pattern else None)
    found = [f for f in everything if SEVERITY_ORDER[f.severity] <= limit]
    sections = []
    for check in CHECKS:
        hits = [f for f in found if f.check == check.key]
        if hits:
            sections.append({"check": check, "findings": hits, "severity": hits[0].severity})
    sections.sort(key=lambda sec: SEVERITY_ORDER[sec["severity"]])
    counts = {
        sev: sum(1 for f in find_anomalies(snap) if f.severity == sev) for sev in SEVERITY_ORDER
    }
    return TEMPLATES.TemplateResponse(
        request,
        "anomalies.html",
        {"session": s, "sections": sections, "counts": counts, "level": level},
    )
