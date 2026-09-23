"""Group list and editor. Writes go straight to NetBird as the logged-in user."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import RedirectResponse, Response

from birdseye_web.access import grants
from birdseye_web.context import (
    OBJECT_ID,
    TEMPLATES,
    csrf_protect,
    ctx,
    current_session,
    error_message,
    log_change,
    snapshot,
    user_api,
)
from birdseye_web.diff import access_delta, with_group_members
from birdseye_web.matrix import MatrixFilter, group_matrix
from birdseye_web.models import Snapshot
from birdseye_web.nbapi import NetBirdAPI, NetBirdError
from birdseye_web.payloads import PayloadError, group_payload
from birdseye_web.sessions import Session

router = APIRouter(prefix="/groups")


def policies_using(snap: Snapshot, group_id: str) -> list:
    return [
        p
        for p in snap.policies.values()
        if any(group_id in (*r.source_group_ids, *r.destination_group_ids) for r in p.rules)
    ]


@router.get("")
async def group_list(
    request: Request,
    s: Session = Depends(current_session),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    q = request.query_params.get("q", "").strip().lower()
    groups = sorted(
        (g for g in snap.groups.values() if not q or q in g.name.lower()),
        key=lambda g: g.name.lower(),
    )
    usage = {g.id: len(policies_using(snap, g.id)) for g in groups}
    tpl = "_group_rows.html" if request.headers.get("HX-Target") == "group-rows" else "groups.html"
    return TEMPLATES.TemplateResponse(
        request, tpl, {"session": s, "groups": groups, "usage": usage, "q": q, "snap": snap}
    )


def _editor(request, s, snap, group=None, error="", name="", selected=None, status=200):
    selected = set(selected if selected is not None else (group.peer_ids if group else ()))
    peers = sorted(snap.peers.values(), key=lambda p: (p.id not in selected, p.name.lower()))
    reach = None
    if group is not None:
        m = group_matrix(snap, grants(snap), MatrixFilter())
        row = f"g:{group.id}"
        out = [(c, m.cell(row, c.key)) for c in m.cols if m.cell(row, c.key)]
        inc = [(r, m.cell(r.key, row)) for r in m.rows if m.cell(r.key, row)]
        reach = {"out": out, "in": inc}
    return TEMPLATES.TemplateResponse(
        request,
        "group_edit.html",
        {
            "session": s,
            "snap": snap,
            "group": group,
            "name": name or (group.name if group else ""),
            "peers": peers,
            "selected": selected,
            "policies": policies_using(snap, group.id) if group else [],
            "reach": reach,
            "error": error,
        },
        status_code=status,
    )


@router.get("/new")
async def group_new(
    request: Request, s: Session = Depends(current_session), snap: Snapshot = Depends(snapshot)
) -> Response:
    return _editor(request, s, snap)


@router.get("/{gid}")
async def group_edit(
    request: Request,
    gid: Annotated[str, Path(pattern=OBJECT_ID)],
    s: Session = Depends(current_session),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    group = snap.groups.get(gid)
    if group is None:
        return Response("Group not found", status_code=404)
    return _editor(request, s, snap, group)


async def _form(request: Request) -> tuple[str, list[str]]:
    form = await request.form()
    return str(form.get("name", "")), [str(p) for p in form.getlist("peers")]


@router.post("")
async def group_create(
    request: Request,
    s: Session = Depends(csrf_protect),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    name, peers = await _form(request)
    try:
        body = group_payload(name, peers)
        created = await api.post("groups", body)
    except (PayloadError, NetBirdError) as exc:
        return _editor(request, s, snap, None, error_message(exc), name, peers, 400)
    log_change(s, "create group", None, body)
    ctx(request).cache.invalidate()
    return RedirectResponse(f"/groups/{created['id']}?saved=1", status_code=303)


@router.post("/{gid}")
async def group_update(
    request: Request,
    gid: Annotated[str, Path(pattern=OBJECT_ID)],
    s: Session = Depends(csrf_protect),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    name, peers = await _form(request)
    group = snap.groups.get(gid)
    try:
        # PUT replaces the whole group: re-read now so resources and concurrent
        # edits by others are not lost between page load and save.
        fresh = await api.get(f"groups/{gid}")
        if fresh.get("name") == "All":
            raise PayloadError("The 'All' group is managed by NetBird and cannot be edited.")
        body = group_payload(name, peers, fresh.get("resources") or [])
        await api.put(f"groups/{gid}", body)
    except (PayloadError, NetBirdError) as exc:
        return _editor(request, s, snap, group, error_message(exc), name, peers, 400)
    before = {
        "name": fresh.get("name"),
        "peers": sorted(p["id"] for p in fresh.get("peers") or []),
    }
    log_change(s, f"update group {gid}", before, {"name": body["name"], "peers": body["peers"]})
    ctx(request).cache.invalidate()
    return RedirectResponse(f"/groups/{gid}?saved=1", status_code=303)


@router.post("/{gid}/delete")
async def group_delete(
    request: Request,
    gid: Annotated[str, Path(pattern=OBJECT_ID)],
    s: Session = Depends(csrf_protect),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    group = snap.groups.get(gid)
    try:
        await api.delete(f"groups/{gid}")
    except NetBirdError as exc:
        return _editor(request, s, snap, group, error_message(exc), status=400)
    log_change(s, f"delete group {gid}", group.name if group else gid, None)
    ctx(request).cache.invalidate()
    return RedirectResponse("/groups?deleted=1", status_code=303)


@router.post("/{gid}/preview")
async def group_preview(
    request: Request,
    gid: Annotated[str, Path(pattern=OBJECT_ID)],
    s: Session = Depends(csrf_protect),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    if gid not in snap.groups:
        return Response("Group not found", status_code=404)
    _, peers = await _form(request)
    delta = access_delta(snap, with_group_members(snap, gid, peers))
    return TEMPLATES.TemplateResponse(
        request, "_delta.html", {"delta": delta, "snap": snap, "limit": 60, "error": ""}
    )
