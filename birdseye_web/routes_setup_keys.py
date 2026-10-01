"""Setup keys: list with state, create, edit auto-groups, revoke, delete.

The key value exists in exactly one place: the response page of the create
request. It is never logged (`log_change` gets the request body, which has
no key), never redirected through a URL, and never cached — the snapshot
only holds NetBird's masked list, and `models.SetupKey` has no key field.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import quote, urlencode

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
from birdseye_web.models import SetupKey, Snapshot
from birdseye_web.nbapi import NetBirdAPI, NetBirdError
from birdseye_web.payloads import (
    MAX_KEY_DAYS,
    SETUP_KEY_TYPES,
    PayloadError,
    setup_key_create_payload,
    setup_key_update_payload,
)
from birdseye_web.peers import editable_groups, group_edit
from birdseye_web.sessions import Session

router = APIRouter(prefix="/setup-keys")
STATES = ("valid", "expired", "revoked", "exhausted")


def may_manage(s: Session, op: str) -> bool:
    """UI hint only: NetBird checks the real permission on every call."""
    return s.can("setup_keys", op) or s.is_admin


def _forbidden(request: Request, s: Session) -> Response:
    return TEMPLATES.TemplateResponse(
        request,
        "forbidden.html",
        {"session": s, "message": "Your NetBird role may not manage setup keys."},
        status_code=403,
    )


def _groups(snap: Snapshot):
    return sorted((snap.groups[g] for g in editable_groups(snap)), key=lambda g: g.name.lower())


@router.get("")
async def key_list(
    request: Request,
    s: Session = Depends(current_session),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    if not may_manage(s, "read"):
        return _forbidden(request, s)
    now = datetime.now(UTC)
    state = request.query_params.get("state", "")
    keys = sorted(snap.setup_keys.values(), key=lambda k: k.name.lower())
    states = {k.id: k.state_at(now) for k in keys}
    shown = [k for k in keys if not state or states[k.id] == state]
    return TEMPLATES.TemplateResponse(
        request,
        "setup_keys.html",
        {
            "session": s,
            "snap": snap,
            "keys": shown,
            "states": states,
            "state": state if state in STATES else "",
            "all_states": STATES,
            "counts": {st: sum(1 for v in states.values() if v == st) for st in STATES},
            "can_create": may_manage(s, "create"),
            "can_bulk": may_manage(s, "update") or may_manage(s, "delete"),
            "bulk": {
                k: _int(request.query_params.get(k), 0)
                for k in ("revoked", "deleted", "skipped", "failed")
            },
        },
    )


def _new_form(request, s, snap, error="", values=None, status=200):
    return TEMPLATES.TemplateResponse(
        request,
        "setup_key_new.html",
        {
            "session": s,
            "snap": snap,
            "groups": _groups(snap),
            "types": SETUP_KEY_TYPES,
            "max_days": MAX_KEY_DAYS,
            "v": values or {"type": "one-off", "expires_days": 7, "usage_limit": 0, "groups": []},
            "error": error,
        },
        status_code=status,
    )


@router.get("/new")
async def key_new(
    request: Request, s: Session = Depends(current_session), snap: Snapshot = Depends(snapshot)
) -> Response:
    if not may_manage(s, "create"):
        return _forbidden(request, s)
    return _new_form(request, s, snap)


def _int(value: object, default: int) -> int:
    try:
        return int(str(value).strip())
    except ValueError:
        return default


@router.post("")
async def key_create(
    request: Request,
    s: Session = Depends(csrf_protect),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    form = await request.form()
    allowed = editable_groups(snap)
    values = {
        "name": str(form.get("name", "")),
        "type": str(form.get("type", "")),
        "expires_days": _int(form.get("expires_days"), 0),
        "usage_limit": _int(form.get("usage_limit"), -1),
        "groups": [str(g) for g in form.getlist("groups") if g in allowed],
        "ephemeral": bool(form.get("ephemeral")),
    }
    try:
        body = setup_key_create_payload(
            name=values["name"],
            type=values["type"],
            expires_days=values["expires_days"],
            usage_limit=values["usage_limit"],
            auto_groups=values["groups"],
            ephemeral=values["ephemeral"],
        )
        created = await api.post("setup-keys", body)
        if not isinstance(created, dict):
            created = {}  # template then says NetBird returned no key
    except (PayloadError, NetBirdError) as exc:
        return _new_form(request, s, snap, error_message(exc), values, 400)
    log_change(s, "create setup key", None, body)  # request body: has no key value
    ctx(request).cache.invalidate()
    # Shown once, in this response only (no redirect: the value must not be in a URL).
    return TEMPLATES.TemplateResponse(
        request,
        "setup_key_created.html",
        {
            "session": s,
            "kid": str(created.get("id") or ""),
            "name": body["name"],
            "secret": str(created.get("key") or ""),
        },
    )


BULK_ACTIONS = ("revoke", "delete")


async def _bulk_one(api: NetBirdAPI, s: Session, kid: str, action: str) -> str:
    """Revoke or delete one key from a fresh GET; returns the outcome counter."""
    path = f"setup-keys/{quote(kid, safe='')}"
    fresh = await api.get(path)
    if action == "revoke":
        if fresh.get("revoked"):
            return "skipped"
        before = setup_key_update_payload(fresh)
        body = setup_key_update_payload(fresh, revoked=True)
        await api.put(path, body)
        log_change(s, f"update setup key {kid}", before, body)
        return "revoked"
    if not fresh.get("revoked"):
        return "skipped"  # only revoked keys are deleted
    await api.delete(path)
    log_change(s, f"delete setup key {kid}", fresh.get("name"), None)
    return "deleted"


@router.post("/bulk")
async def key_bulk(
    request: Request,
    s: Session = Depends(csrf_protect),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    """Revoke or delete the selected keys, one API call each; a failure on
    one key does not stop the others."""
    form = await request.form()
    action = str(form.get("action", ""))
    if action not in BULK_ACTIONS:
        return Response("Unknown action", status_code=400)
    counts = {"revoked": 0, "deleted": 0, "skipped": 0, "failed": 0}
    for kid in dict.fromkeys(str(k) for k in form.getlist("keys")):
        if kid not in snap.setup_keys:
            continue
        try:
            counts[await _bulk_one(api, s, kid, action)] += 1
        except (PayloadError, NetBirdError):
            counts["failed"] += 1
    ctx(request).cache.invalidate()
    return RedirectResponse(
        "/setup-keys?" + urlencode({k: v for k, v in counts.items() if v}), status_code=303
    )


def _editor(request, s, snap, key: SetupKey, error="", status=200):
    allowed = editable_groups(snap)
    return TEMPLATES.TemplateResponse(
        request,
        "setup_key_edit.html",
        {
            "session": s,
            "snap": snap,
            "key": key,
            "state": key.state_at(datetime.now(UTC)),
            "groups": _groups(snap),
            "initial_groups": sorted(key.auto_groups & allowed),
            "other_groups": sorted(key.auto_groups - allowed),
            "can_update": may_manage(s, "update"),
            "can_delete": may_manage(s, "delete"),
            "error": error,
        },
        status_code=status,
    )


@router.get("/{kid}")
async def key_edit(
    request: Request,
    kid: Annotated[str, Path(pattern=OBJECT_ID)],
    s: Session = Depends(current_session),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    if not may_manage(s, "read"):
        return _forbidden(request, s)
    key = snap.setup_keys.get(kid)
    if key is None:
        return Response("Setup key not found", status_code=404)
    return _editor(request, s, snap, key)


@router.post("/{kid}")
async def key_update(
    request: Request,
    kid: Annotated[str, Path(pattern=OBJECT_ID)],
    s: Session = Depends(csrf_protect),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    key = snap.setup_keys.get(kid)
    if key is None:
        return Response("Setup key not found", status_code=404)
    form = await request.form()
    try:
        fresh = await api.get(f"setup-keys/{quote(kid, safe='')}")
        before = setup_key_update_payload(fresh)
        groups = None
        if form.get("groups_field"):
            add, rem = group_edit(
                frozenset(before["auto_groups"]),
                [str(g) for g in form.getlist("groups")],
                [str(g) for g in form.getlist("groups_initial")],
                editable_groups(snap),
            )
            groups = (frozenset(before["auto_groups"]) | add) - rem
        revoke = True if form.get("revoke") else None
        body = setup_key_update_payload(fresh, auto_groups=groups, revoked=revoke)
        if body != before:
            await api.put(f"setup-keys/{quote(kid, safe='')}", body)
            log_change(s, f"update setup key {kid}", before, body)
    except (PayloadError, NetBirdError) as exc:
        return _editor(request, s, snap, key, error_message(exc), 400)
    ctx(request).cache.invalidate()
    return RedirectResponse(f"/setup-keys/{kid}?saved=1", status_code=303)


@router.post("/{kid}/delete")
async def key_delete(
    request: Request,
    kid: Annotated[str, Path(pattern=OBJECT_ID)],
    s: Session = Depends(csrf_protect),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    key = snap.setup_keys.get(kid)
    if key is None:
        return Response("Setup key not found", status_code=404)
    try:
        fresh = await api.get(f"setup-keys/{quote(kid, safe='')}")
        if not fresh.get("revoked"):
            raise PayloadError("Revoke the key before deleting it.")
        await api.delete(f"setup-keys/{quote(kid, safe='')}")
    except (PayloadError, NetBirdError) as exc:
        return _editor(request, s, snap, key, error_message(exc), 400)
    log_change(s, f"delete setup key {kid}", key.name, None)
    ctx(request).cache.invalidate()
    return RedirectResponse("/setup-keys?deleted=1", status_code=303)
