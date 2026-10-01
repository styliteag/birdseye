"""Peer list and editor: settings, group membership, delete.

Settings go out as a full `PUT /peers/{id}` built from a fresh GET. Group
membership has no peer-side endpoint: each changed group is re-read and
PUT whole, so its resources and concurrent edits survive.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import RedirectResponse, Response

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
from birdseye_web.diff import access_delta, with_peer_groups, without_peer
from birdseye_web.models import Peer, Snapshot
from birdseye_web.nbapi import NetBirdAPI, NetBirdError
from birdseye_web.payloads import PayloadError, group_payload, peer_payload
from birdseye_web.peers import (
    PeerQuery,
    editable_groups,
    filter_peers,
    group_edit,
    os_options,
    peer_access,
    user_options,
)
from birdseye_web.sessions import Session
from birdseye_web.snapshot import load_snapshot

router = APIRouter(prefix="/peers")
PREVIEW_LIMIT = 60


@router.get("")
async def peer_list(
    request: Request,
    s: Session = Depends(current_session),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    q = PeerQuery.from_params(request.query_params)
    peers = filter_peers(snap, q)
    tpl = "_peer_rows.html" if request.headers.get("HX-Target") == "peer-rows" else "peers.html"
    return TEMPLATES.TemplateResponse(
        request,
        tpl,
        {
            "session": s,
            "snap": snap,
            "peers": peers,
            "query": q,
            "users": user_options(snap),
            "groups": sorted(snap.groups.values(), key=lambda g: g.name.lower()),
            "oses": os_options(snap),
        },
    )


@dataclass(frozen=True)
class PeerForm:
    """Editor input. None means the form had no such field (no permission)."""

    name: str | None = None
    ssh_enabled: bool | None = None
    login_expiration_enabled: bool | None = None
    approved: bool | None = None
    groups: tuple[str, ...] | None = None
    groups_initial: tuple[str, ...] = ()

    def group_changes(self, snap: Snapshot, peer: Peer) -> tuple[frozenset[str], frozenset[str]]:
        if self.groups is None:
            return frozenset(), frozenset()
        return group_edit(peer.group_ids, self.groups, self.groups_initial, editable_groups(snap))


async def _form(request: Request) -> PeerForm:
    form = await request.form()
    settings = bool(form.get("settings_field"))
    groups = None
    if form.get("groups_field"):
        groups = tuple(str(g) for g in form.getlist("groups"))
    return PeerForm(
        name=str(form.get("name", "")) if settings else None,
        ssh_enabled=bool(form.get("ssh_enabled")) if settings else None,
        login_expiration_enabled=bool(form.get("login_expiration_enabled")) if settings else None,
        approved=bool(form.get("approve")) if settings else None,
        groups=groups,
        groups_initial=tuple(str(g) for g in form.getlist("groups_initial")),
    )


def _editor(request, s, snap, peer: Peer, error="", form: PeerForm | None = None, status=200):
    editable = editable_groups(snap)
    chosen = set(peer.group_ids)
    if form is not None and form.groups is not None:
        add, rem = form.group_changes(snap, peer)
        chosen = (chosen | add) - rem
    owner = snap.users.get(peer.user_id)
    return TEMPLATES.TemplateResponse(
        request,
        "peer_edit.html",
        {
            "session": s,
            "snap": snap,
            "peer": peer,
            "owner": owner,
            "form": form,
            "can_peers": s.can("peers", "update") or s.is_admin,
            "can_delete": s.can("peers", "delete") or s.is_admin,
            "can_groups": s.can("groups", "update") or s.is_admin,
            "groups": sorted((snap.groups[g] for g in editable), key=lambda g: g.name.lower()),
            "chosen": chosen,
            "initial_groups": sorted(peer.group_ids & editable),
            "other_groups": sorted(peer.group_ids - editable),
            "access": peer_access(snap, peer.id),
            "error": error,
        },
        status_code=status,
    )


@router.get("/{pid}")
async def peer_edit(
    request: Request,
    pid: Annotated[str, Path(pattern=OBJECT_ID)],
    s: Session = Depends(current_session),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    peer = snap.peers.get(pid)
    if peer is None:
        return Response("Peer not found", status_code=404)
    return _editor(request, s, snap, peer)


async def _update_settings(api: NetBirdAPI, s: Session, pid: str, form: PeerForm) -> None:
    fresh = await api.get(f"peers/{quote(pid, safe='')}")
    body = peer_payload(
        fresh,
        name=form.name,
        ssh_enabled=form.ssh_enabled,
        login_expiration_enabled=form.login_expiration_enabled,
        approved=form.approved,
    )
    before = peer_payload(fresh)
    if body != before:
        await api.put(f"peers/{quote(pid, safe='')}", body)
        log_change(s, f"update peer {pid}", before, body)


async def _update_groups(
    api: NetBirdAPI, s: Session, pid: str, add: frozenset[str], remove: frozenset[str]
) -> None:
    for gid in sorted(add | remove):
        # PUT replaces the whole group: re-read so resources and others' edits stay.
        g = await api.get(f"groups/{quote(gid, safe='')}")
        old = {str(p["id"]) if isinstance(p, dict) else str(p) for p in g.get("peers") or ()}
        new = old | {pid} if gid in add else old - {pid}
        if new == old:
            continue
        body = group_payload(str(g.get("name") or ""), new, g.get("resources") or [])
        await api.put(f"groups/{quote(gid, safe='')}", body)
        log_change(s, f"update group {gid}", sorted(old), body["peers"])


@router.post("/{pid}")
async def peer_update(
    request: Request,
    pid: Annotated[str, Path(pattern=OBJECT_ID)],
    s: Session = Depends(csrf_protect),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    peer = snap.peers.get(pid)
    if peer is None:
        return Response("Peer not found", status_code=404)
    form = await _form(request)
    try:
        if form.name is not None:
            await _update_settings(api, s, pid, form)
    except (PayloadError, NetBirdError) as exc:
        return _editor(request, s, snap, peer, error_message(exc), form, 400)
    ctx(request).cache.invalidate()
    add, rem = form.group_changes(snap, peer)
    try:
        await _update_groups(api, s, pid, add, rem)
    except (PayloadError, NetBirdError) as exc:
        return await _partial_failure(request, s, api, pid, exc)
    ctx(request).cache.invalidate()
    return RedirectResponse(f"/peers/{pid}?saved=1", status_code=303)


async def _partial_failure(
    request: Request, s: Session, api: NetBirdAPI, pid: str, exc: Exception
) -> Response:
    """Settings saved but a group update failed: say so, show fresh state."""
    c = ctx(request)
    c.cache.invalidate()
    fresh = await c.cache.get(s.user_id, lambda: load_snapshot(api))
    msg = f"Peer saved, but not every group was updated. {error_message(exc)}"
    peer = fresh.peers.get(pid)
    if peer is None:
        return Response(msg, status_code=400)
    return _editor(request, s, fresh, peer, msg, status=400)


def _delta(request, snap, delta):
    return TEMPLATES.TemplateResponse(
        request,
        "_delta.html",
        {"delta": delta, "snap": snap, "limit": PREVIEW_LIMIT, "error": ""},
    )


@router.post("/{pid}/preview")
async def peer_preview(
    request: Request,
    pid: Annotated[str, Path(pattern=OBJECT_ID)],
    s: Session = Depends(csrf_protect),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    peer = snap.peers.get(pid)
    if peer is None:
        return Response("Peer not found", status_code=404)
    add, rem = (await _form(request)).group_changes(snap, peer)
    after = with_peer_groups(snap, pid, (peer.group_ids | add) - rem)
    return _delta(request, snap, access_delta(snap, after))


@router.post("/{pid}/delete-preview")
async def peer_delete_preview(
    request: Request,
    pid: Annotated[str, Path(pattern=OBJECT_ID)],
    s: Session = Depends(csrf_protect),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    """Separate route on purpose: a preview must never share the delete handler."""
    if pid not in snap.peers:
        return Response("Peer not found", status_code=404)
    return _delta(request, snap, access_delta(snap, without_peer(snap, pid)))


@router.post("/{pid}/delete")
async def peer_delete(
    request: Request,
    pid: Annotated[str, Path(pattern=OBJECT_ID)],
    s: Session = Depends(csrf_protect),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    peer = snap.peers.get(pid)
    if peer is None:
        return Response("Peer not found", status_code=404)
    try:
        await api.delete(f"peers/{quote(pid, safe='')}")
    except NetBirdError as exc:
        return _editor(request, s, snap, peer, error_message(exc), status=400)
    log_change(s, f"delete peer {pid}", peer.name, None)
    ctx(request).cache.invalidate()
    return RedirectResponse("/peers?deleted=1", status_code=303)
