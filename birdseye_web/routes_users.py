"""User list and editor: role, block state, auto-groups, device group drift.

Writes go to NetBird as the logged-in user. The user PUT runs first: with
group propagation on, NetBird moves the user's peers itself, and the device
alignment that follows is computed from groups re-read after that.
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
from birdseye_web.diff import access_delta, with_memberships, with_user_groups
from birdseye_web.drift import alignment, managed_groups, user_drift
from birdseye_web.models import Snapshot, User
from birdseye_web.nbapi import NetBirdAPI, NetBirdError
from birdseye_web.payloads import ROLES, PayloadError, group_payload, user_payload
from birdseye_web.sessions import Session
from birdseye_web.snapshot import load_snapshot

router = APIRouter(prefix="/users")


def editable_groups(snap: Snapshot) -> frozenset[str]:
    """Groups the editor may add or remove; IdP/integration groups are synced."""
    return frozenset(g.id for g in snap.groups.values() if g.issued == "api" and not g.is_all)


def _people(snap: Snapshot) -> list[User]:
    return sorted(
        (u for u in snap.users.values() if not u.is_service_user), key=lambda u: u.label.lower()
    )


@router.get("")
async def user_list(
    request: Request,
    s: Session = Depends(current_session),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    q = request.query_params.get("q", "").strip().lower()
    people = [u for u in _people(snap) if not q or q in f"{u.name} {u.email}".lower()]
    devices = {u.id: sum(1 for p in snap.peers.values() if p.user_id == u.id) for u in people}
    drift = {u.id: len(user_drift(snap, u.id)) for u in people}
    tpl = "_user_rows.html" if request.headers.get("HX-Target") == "user-rows" else "users.html"
    return TEMPLATES.TemplateResponse(
        request,
        tpl,
        {"session": s, "snap": snap, "people": people, "devices": devices, "drift": drift, "q": q},
    )


@dataclass(frozen=True)
class UserForm:
    """Editor input. None means the form had no such field."""

    role: str | None = None
    blocked: bool | None = None
    groups: tuple[str, ...] | None = None
    groups_initial: tuple[str, ...] = ()
    align: tuple[str, ...] = ()

    def auto_groups(self, current: frozenset[str], editable: frozenset[str]) -> frozenset[str]:
        """Apply the change the user made, not the whole form: a group someone
        else assigned after the page loaded is kept."""
        if self.groups is None:
            return current
        sel, init = set(self.groups) & editable, set(self.groups_initial) & editable
        return frozenset((current - (init - sel)) | (sel - init))


async def _form(request: Request, s: Session, uid: str) -> UserForm:
    form = await request.form()
    own = uid == s.user_id  # NetBird refuses self-demotion and self-blocking
    groups = None
    if form.get("groups_field"):
        groups = tuple(str(g) for g in form.getlist("groups"))
    return UserForm(
        role=None if own or not form.get("role") else str(form["role"]),
        blocked=None if own or not form.get("user_field") else bool(form.get("blocked")),
        groups=groups,
        groups_initial=tuple(str(g) for g in form.getlist("groups_initial")),
        align=tuple(str(p) for p in form.getlist("align")),
    )


def _editor(request, s, snap, user, error="", form: UserForm | None = None, status=200):
    editable = editable_groups(snap)
    chosen = user.auto_groups if form is None else form.auto_groups(user.auto_groups, editable)
    devices = sorted(
        (p for p in snap.peers.values() if p.user_id == user.id), key=lambda p: p.name.lower()
    )
    return TEMPLATES.TemplateResponse(
        request,
        "user_edit.html",
        {
            "session": s,
            "snap": snap,
            "user": user,
            "is_self": user.id == s.user_id,
            "can_users": s.can("users", "update"),
            "can_groups": s.can("groups", "update"),
            "roles": ROLES,
            "role": (form.role if form and form.role else user.role),
            "blocked": form.blocked if form and form.blocked is not None else user.is_blocked,
            "groups": sorted((snap.groups[g] for g in editable), key=lambda g: g.name.lower()),
            "chosen": chosen,
            "initial_groups": sorted(user.auto_groups & editable),
            "other_groups": sorted(user.auto_groups - editable),
            "devices": devices,
            "drift": {d.peer_id: d for d in user_drift(snap, user.id)},
            "align": set(form.align) if form else set(),
            "error": error,
        },
        status_code=status,
    )


def _person(snap: Snapshot, uid: str) -> User | None:
    """The user, unless missing or a service user (not editable here)."""
    u = snap.users.get(uid)
    return None if u is None or u.is_service_user else u


@router.get("/{uid}")
async def user_edit(
    request: Request,
    uid: Annotated[str, Path(pattern=OBJECT_ID)],
    s: Session = Depends(current_session),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    user = _person(snap, uid)
    if user is None:
        return Response("User not found", status_code=404)
    return _editor(request, s, snap, user)


def _own_peers(snap: Snapshot, uid: str, peer_ids) -> frozenset[str]:
    return frozenset(p for p in peer_ids if p in snap.peers and snap.peers[p].user_id == uid)


async def _update_user(
    api: NetBirdAPI, s: Session, snap: Snapshot, uid: str, form: UserForm
) -> frozenset[str]:
    """PUT the user if anything changed; returns the auto-groups now in effect."""
    # No single-user GET in the API; re-read all so the update starts fresh.
    fresh = {str(u["id"]): u for u in await api.get("users") or []}
    raw = fresh.get(uid)
    if raw is None or raw.get("is_service_user"):
        raise PayloadError("User not found or is a service user.")
    old = frozenset(
        str(g["id"]) if isinstance(g, dict) else str(g) for g in raw.get("auto_groups") or ()
    )
    new = form.auto_groups(old, editable_groups(snap))
    body = user_payload(raw, new, role=form.role, is_blocked=form.blocked)
    before = {
        "role": raw.get("role"),
        "auto_groups": sorted(old),
        "is_blocked": bool(raw.get("is_blocked")),
    }
    if body != before:
        await api.put(f"users/{quote(uid, safe='')}", body)
        log_change(s, f"update user {uid}", before, body)
    return new


async def _align_devices(
    api: NetBirdAPI, s: Session, snap: Snapshot, peer_ids: frozenset[str], want: frozenset[str]
) -> None:
    """Put the peers into exactly their user's managed groups. Groups are
    re-read here, after the user PUT, so NetBird's own propagation counts."""
    fresh = {str(g["id"]): g for g in await api.get("groups") or []}
    members = {
        gid: frozenset(str(p["id"]) for p in g.get("peers") or ()) for gid, g in fresh.items()
    }
    changes = alignment(members, managed_groups(snap) & fresh.keys(), peer_ids, want)
    for gid, peers in changes.items():
        g = fresh[gid]
        body = group_payload(str(g.get("name") or ""), peers, g.get("resources") or [])
        await api.put(f"groups/{quote(gid, safe='')}", body)
        log_change(s, f"update group {gid}", sorted(members[gid]), body["peers"])


@router.post("/{uid}")
async def user_update(
    request: Request,
    uid: Annotated[str, Path(pattern=OBJECT_ID)],
    s: Session = Depends(csrf_protect),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    user = _person(snap, uid)
    if user is None:
        return Response("User not found", status_code=404)
    form = await _form(request, s, uid)
    try:
        want = await _update_user(api, s, snap, uid, form)
    except (PayloadError, NetBirdError) as exc:
        return _editor(request, s, snap, user, error_message(exc), form, 400)
    ctx(request).cache.invalidate()
    peers = _own_peers(snap, uid, form.align) if s.can("groups", "update") else frozenset()
    if peers:
        try:
            await _align_devices(api, s, snap, peers, want)
        except (PayloadError, NetBirdError) as exc:
            return await _partial_failure(request, s, api, uid, exc)
        ctx(request).cache.invalidate()
    return RedirectResponse(f"/users/{uid}?saved=1", status_code=303)


async def _partial_failure(
    request: Request, s: Session, api: NetBirdAPI, uid: str, exc: Exception
) -> Response:
    """The user was saved but a device fix failed: say so, show fresh state."""
    c = ctx(request)
    c.cache.invalidate()
    fresh = await c.cache.get(s.user_id, lambda: load_snapshot(api))
    msg = f"User saved, but not every device was fixed. {error_message(exc)}"
    user = _person(fresh, uid)
    if user is None:
        return Response(msg, status_code=400)
    return _editor(request, s, fresh, user, msg, status=400)


@router.post("/{uid}/preview")
async def user_preview(
    request: Request,
    uid: Annotated[str, Path(pattern=OBJECT_ID)],
    s: Session = Depends(csrf_protect),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    user = _person(snap, uid)
    if user is None:
        return Response("User not found", status_code=404)
    form = await _form(request, s, uid)
    want = form.auto_groups(user.auto_groups, editable_groups(snap))
    after = with_user_groups(snap, uid, want)
    peers = _own_peers(snap, uid, form.align)
    if peers:
        members = {gid: g.peer_ids for gid, g in after.groups.items()}
        after = with_memberships(after, alignment(members, managed_groups(snap), peers, want))
    delta = access_delta(snap, after)
    return TEMPLATES.TemplateResponse(
        request, "_delta.html", {"delta": delta, "snap": snap, "limit": 60, "error": ""}
    )
