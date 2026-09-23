"""Edit access straight from a matrix cell: allow a pair, or revoke a grant.

Every action is two-step: `preview=1` renders the gained/lost delta and a
confirm button; the confirm posts the same form without the flag. A write
answers with `HX-Trigger: matrix-changed`, which reloads the table with the
current filters.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from birdseye_web.context import (
    TEMPLATES,
    csrf_protect,
    ctx,
    error_message,
    log_change,
    snapshot,
    user_api,
)
from birdseye_web.diff import access_delta, with_policy, without_policy
from birdseye_web.models import Snapshot
from birdseye_web.nbapi import NetBirdAPI, NetBirdError
from birdseye_web.payloads import PROTOCOLS, PayloadError
from birdseye_web.quickedit import allow_policy, revoke
from birdseye_web.sessions import Session

router = APIRouter(prefix="/matrix")
PREVIEW_LIMIT = 25


def _render(request: Request, snap: Snapshot, **ctx_vars) -> Response:
    return TEMPLATES.TemplateResponse(
        request,
        "_quick_result.html",
        {"snap": snap, "limit": PREVIEW_LIMIT, "error": "", "done": "", **ctx_vars},
    )


def _fields(form) -> list[tuple[str, str]]:
    """Echo the previewed form so the confirm button posts exactly that."""
    return [(k, str(v)) for k, v in form.multi_items() if k not in ("preview", "csrf")]


def _done(request: Request, snap: Snapshot, message: str) -> Response:
    resp = _render(request, snap, done=message)
    resp.headers["HX-Trigger"] = "matrix-changed"
    return resp


@router.post("/allow")
async def quick_allow(
    request: Request,
    s: Session = Depends(csrf_protect),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    form = await request.form()
    try:
        protocol = str(form.get("protocol", "all"))
        if protocol not in PROTOCOLS:
            raise PayloadError(f"unknown protocol {protocol!r}")
        body = allow_policy(
            str(form.get("row", "")),
            str(form.get("col", "")),
            names=snap.names(),
            resource_types={r.id: r.type for r in snap.resources.values()},
            protocol=protocol,
            ports=str(form.get("ports", "")),
            bidirectional=form.get("bidirectional") == "on",
            posture=[str(p) for p in form.getlist("posture")],
            name=str(form.get("name", "")),
        )
    except PayloadError as exc:
        return _render(request, snap, error=str(exc))

    if form.get("preview") == "1":
        delta = access_delta(snap, with_policy(snap, {**body, "id": "__new__"}))
        return _render(
            request,
            snap,
            delta=delta,
            confirm="/matrix/allow",
            fields=_fields(form),
            summary=f"Create policy “{body['name']}”",
        )
    try:
        created = await api.post("policies", body)
    except NetBirdError as exc:
        return _render(request, snap, error=error_message(exc))
    log_change(s, "create policy (matrix)", None, body)
    ctx(request).cache.invalidate()
    return _done(request, snap, f"Created policy “{body['name']}” ({created.get('id', '')}).")


@router.post("/revoke")
async def quick_revoke(
    request: Request,
    s: Session = Depends(csrf_protect),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    form = await request.form()
    pid = str(form.get("policy_id", ""))
    if pid not in snap.policies:
        return _render(request, snap, error="Policy not found (reload the matrix).")
    try:
        current = await api.get(f"policies/{pid}")
        change = revoke(
            current,
            str(form.get("rule_id", "")),
            str(form.get("mode", "")),
            str(form.get("target", "")) or None,
        )
    except (PayloadError, NetBirdError) as exc:
        return _render(request, snap, error=error_message(exc))

    after = (
        without_policy(snap, pid)
        if change.method == "DELETE"
        else with_policy(snap, {**change.body, "id": pid})
    )
    if form.get("preview") == "1":
        what = "Delete" if change.method == "DELETE" else "Update"
        return _render(
            request,
            snap,
            delta=access_delta(snap, after),
            confirm="/matrix/revoke",
            fields=_fields(form),
            summary=f"{what} policy “{current.get('name', pid)}”",
            danger=change.method == "DELETE",
        )
    try:
        if change.method == "DELETE":
            await api.delete(f"policies/{pid}")
        else:
            await api.put(f"policies/{pid}", change.body)
    except NetBirdError as exc:
        return _render(request, snap, error=error_message(exc))
    log_change(
        s,
        f"{form.get('mode')} policy {pid} (matrix)",
        json.dumps({"rule": form.get("rule_id"), "target": form.get("target")}),
        change.method,
    )
    ctx(request).cache.invalidate()
    verb = "Deleted" if change.method == "DELETE" else "Updated"
    return _done(request, snap, f"{verb} policy “{current.get('name', pid)}”.")
