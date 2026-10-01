"""Group list and editor. Writes go straight to NetBird as the logged-in user."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated
from urllib.parse import quote

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
from birdseye_web.diff import (
    access_delta,
    user_group_changes,
    with_group_members,
    with_user_auto_groups,
)
from birdseye_web.matrix import MatrixFilter, group_matrix
from birdseye_web.models import Snapshot
from birdseye_web.nbapi import NetBirdAPI, NetBirdError
from birdseye_web.payloads import PayloadError, group_payload, user_payload
from birdseye_web.sessions import Session
from birdseye_web.snapshot import load_snapshot

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


def users_editable(s: Session, group) -> bool:
    """IdP- or integration-issued groups are overwritten on the next sync."""
    return s.can("users", "update") and (
        group is None or (group.issued == "api" and not group.is_all)
    )


def _auto_group_users(snap: Snapshot, group) -> set[str]:
    if group is None:
        return set()
    return {
        u.id for u in snap.users.values() if not u.is_service_user and group.id in u.auto_groups
    }


def _editor(request, s, snap, group=None, error="", name="", selected=None, status=200, users=None):
    selected = set(selected if selected is not None else (group.peer_ids if group else ()))
    peers = sorted(snap.peers.values(), key=lambda p: (p.id not in selected, p.name.lower()))
    initial = _auto_group_users(snap, group)
    chosen = set(users.selected) if users is not None else initial
    people = sorted(
        (u for u in snap.users.values() if not u.is_service_user),
        key=lambda u: (u.id not in chosen, u.label.lower()),
    )
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
            "users_editable": users_editable(s, group),
            "people": people,
            "chosen_users": chosen,
            "initial_users": sorted(users.initial if users is not None else initial),
            "peer_count": {
                u.id: sum(1 for p in snap.peers.values() if p.user_id == u.id) for u in people
            },
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
    """`?peer=<id>` (repeatable) preselects peers, e.g. from the Anomalies page."""
    peers = [p for p in request.query_params.getlist("peer") if p in snap.peers]
    return _editor(request, s, snap, selected=peers)


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


@dataclass(frozen=True)
class UserPick:
    """User section of the form: checked now, and checked when rendered."""

    selected: tuple[str, ...]
    initial: tuple[str, ...]


async def _form(request: Request) -> tuple[str, list[str], UserPick | None]:
    """Name, peer IDs, and the user pick — None when the form had no user section."""
    form = await request.form()
    users = None
    if form.get("users_field"):
        users = UserPick(
            tuple(str(u) for u in form.getlist("users")),
            tuple(str(u) for u in form.getlist("users_initial")),
        )
    return str(form.get("name", "")), [str(p) for p in form.getlist("peers")], users


async def _assign_users(
    api: NetBirdAPI, s: Session, snap: Snapshot, gid: str, pick: UserPick
) -> None:
    """Add or remove `gid` in each changed user's auto-groups.

    Runs after the group PUT: with group propagation on, NetBird then moves
    the users' peers, which a later group PUT would undo.
    """
    added, removed = user_group_changes(snap, gid, pick.selected, pick.initial)
    if not added and not removed:
        return
    # No single-user GET in the API; re-read all so role, block state and
    # other auto-groups are current.
    fresh = {str(u["id"]): u for u in await api.get("users") or []}
    for uid in sorted(added | removed):
        raw = fresh.get(uid)
        if raw is None or raw.get("is_service_user"):
            continue
        old = {
            str(g["id"]) if isinstance(g, dict) else str(g) for g in raw.get("auto_groups") or ()
        }
        new = old | {gid} if uid in added else old - {gid}
        if new == old:
            continue
        body = user_payload(raw, new)
        await api.put(f"users/{quote(uid, safe='')}", body)
        log_change(s, f"update user {uid} auto_groups", sorted(old), body["auto_groups"])


@router.post("")
async def group_create(
    request: Request,
    s: Session = Depends(csrf_protect),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    name, peers, users = await _form(request)
    try:
        body = group_payload(name, peers)
        created = await api.post("groups", body)
    except (PayloadError, NetBirdError) as exc:
        return _editor(request, s, snap, None, error_message(exc), name, peers, 400, users)
    log_change(s, "create group", None, body)
    ctx(request).cache.invalidate()
    gid = str(created["id"])
    if users is not None and users_editable(s, None):
        try:
            await _assign_users(api, s, snap, gid, users)
        except (PayloadError, NetBirdError) as exc:
            return await _partial_failure(request, s, api, gid, exc)
    return RedirectResponse(f"/groups/{gid}?saved=1", status_code=303)


async def _partial_failure(
    request: Request, s: Session, api: NetBirdAPI, gid: str, exc: Exception
) -> Response:
    """The group was saved but a user update failed: say so, show fresh state."""
    c = ctx(request)
    c.cache.invalidate()
    fresh = await c.cache.get(s.user_id, lambda: load_snapshot(api))
    msg = f"Group saved, but not every user was updated. {error_message(exc)}"
    return _editor(request, s, fresh, fresh.groups.get(gid), msg, status=400)


@router.post("/{gid}")
async def group_update(
    request: Request,
    gid: Annotated[str, Path(pattern=OBJECT_ID)],
    s: Session = Depends(csrf_protect),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    name, peers, users = await _form(request)
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
        return _editor(request, s, snap, group, error_message(exc), name, peers, 400, users)
    before = {
        "name": fresh.get("name"),
        "peers": sorted(p["id"] for p in fresh.get("peers") or []),
    }
    log_change(s, f"update group {gid}", before, {"name": body["name"], "peers": body["peers"]})
    ctx(request).cache.invalidate()
    if users is not None and group is not None and users_editable(s, group):
        try:
            await _assign_users(api, s, snap, gid, users)
        except (PayloadError, NetBirdError) as exc:
            return await _partial_failure(request, s, api, gid, exc)
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
    _, peers, users = await _form(request)
    after = with_group_members(snap, gid, peers)
    if users is not None and users_editable(s, snap.groups[gid]):
        added, removed = user_group_changes(snap, gid, users.selected, users.initial)
        after = with_user_auto_groups(after, gid, added=added, removed=removed)
    delta = access_delta(snap, after)
    return TEMPLATES.TemplateResponse(
        request, "_delta.html", {"delta": delta, "snap": snap, "limit": 60, "error": ""}
    )
