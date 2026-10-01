"""Config history: diff two snapshots, an object's timeline, restore one
policy or group.

Snapshots are taken by the birdseye container with the admin key, so they
show more than a restricted user may see: the pages are for owners and
admins only. A restore still writes with the signed-in user's token.
"""

from __future__ import annotations

from pathlib import Path as FsPath
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import RedirectResponse, Response

from birdseye_web.audit import parse_event
from birdseye_web.context import (
    OBJECT_ID,
    TEMPLATES,
    config_changed,
    csrf_protect,
    ctx,
    current_session,
    error_message,
    log_change,
    snapshot,
    user_api,
)
from birdseye_web.diff import access_delta
from birdseye_web.history import (
    KINDS,
    STAMP,
    diff_snapshots,
    find_version,
    list_snapshots,
    load_kind,
    normalize,
    object_timeline,
)
from birdseye_web.history_view import SnapshotStatus, flat_changes, snapshot_status
from birdseye_web.jobs import load_jobs
from birdseye_web.models import Snapshot, parse_time
from birdseye_web.nbapi import NetBirdAPI, NetBirdError
from birdseye_web.restore import RESTORABLE, plan_restore
from birdseye_web.sessions import Session

router = APIRouter(prefix="/history")
PREVIEW_LIMIT = 60
Stamp = Annotated[str, Path(pattern=STAMP.pattern)]
Oid = Annotated[str, Path(pattern=OBJECT_ID)]
LABELS = dict(KINDS)
SINGULAR = {"policies": "policy", "groups": "group"}
ADMIN_ROLES = ("owner", "admin")
EDITORS = {
    "policies": "/policies/",
    "groups": "/groups/",
    "users": "/users/",
    "peers": "/peers/",
    "setup_keys": "/setup-keys/",
    "networks": "/networks/",
}


TEMPLATES.env.globals["flat_changes"] = flat_changes
NAMED = ("groups", "peers", "posture_checks", "users", "networks", "setup_keys", "policies")
# audit codes that do not change the configuration (same default as the forwarder)
NOISE = "login"
POLL_S = 5


def _names(base: FsPath, stamps: list[str], snap: Snapshot) -> dict[str, str]:
    """ID -> name from the compared snapshots, then the live account."""
    names: dict[str, str] = {}
    for stamp in stamps:
        for slug in NAMED:
            for obj in load_kind(base, stamp, slug):
                label = obj.get("name") or obj.get("email")
                if obj.get("id") and label:
                    names.setdefault(str(obj["id"]), str(label))
    return {**names, **snap.names()}


async def _status(api: NetBirdAPI, base: FsPath) -> SnapshotStatus:
    try:
        raw = await api.get("events/audit") or []
    except NetBirdError:
        raw = []
    events = (parse_event(e) for e in raw)
    changes = [e.timestamp for e in events if e.timestamp and NOISE not in e.activity_code]
    snaps = list_snapshots(base)
    view = load_jobs(base.parent)
    job = next((j for j in view.jobs if j.key == "history"), None)
    return snapshot_status(
        changes,
        snaps[0].at if snaps else None,
        job_enabled=bool(job and job.enabled),
        running=bool(job and job.status == "running"),
        last_run=parse_time(job.started) if job else None,
    )


def history_base(request: Request) -> FsPath | None:
    jobs = ctx(request).settings.jobs_dir
    return FsPath(jobs) / "history" if jobs else None


async def _gate(request: Request, s: Session, api: NetBirdAPI) -> Response | FsPath:
    # Snapshots bypass NetBird's own checks (admin key, read from disk), so
    # check the role live: the one in the session is from sign-in.
    me = await api.get("users/current") or {}
    if me.get("role") not in ADMIN_ROLES:
        return TEMPLATES.TemplateResponse(
            request,
            "forbidden.html",
            {"session": s, "message": "Config history is for owners and admins."},
            status_code=403,
        )
    base = history_base(request)
    if base is None:
        return TEMPLATES.TemplateResponse(
            request, "history_off.html", {"session": s}, status_code=404
        )
    return base


@router.get("")
async def history_page(
    request: Request,
    s: Session = Depends(current_session),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    base = await _gate(request, s, api)
    if isinstance(base, Response):
        return base
    snaps = list_snapshots(base)
    stamps = [x.stamp for x in snaps]
    q = request.query_params
    new = q.get("to") if q.get("to") in stamps else (stamps[0] if stamps else "")
    older = [x for x in stamps if x < new]
    old = q.get("from") if q.get("from") in stamps else (older[0] if older else "")
    if old > new:
        old, new = new, old
    diffs = diff_snapshots(base, old, new) if old and new and old != new else []
    return TEMPLATES.TemplateResponse(
        request,
        "history.html",
        {
            "session": s,
            "snaps": snaps,
            "old": old,
            "new": new,
            "diffs": diffs,
            "names": _names(base, [old, new], snap) if diffs else {},
            "status": await _status(api, base),
            "poll_s": POLL_S,
            "labels": LABELS,
            "editors": EDITORS,
            "restorable": RESTORABLE,
        },
    )


@router.get("/status")
async def history_status(
    request: Request,
    s: Session = Depends(current_session),
    api: NetBirdAPI = Depends(user_api),
) -> Response:
    """Polled while a snapshot is due; reloads the page once it is there."""
    base = await _gate(request, s, api)
    if isinstance(base, Response):
        return base
    status = await _status(api, base)
    if request.query_params.get("waiting") and status.state == "current":
        return Response(status_code=200, headers={"HX-Refresh": "true"})
    return TEMPLATES.TemplateResponse(
        request, "_history_status.html", {"status": status, "poll_s": POLL_S}
    )


@router.get("/object/{oid}")
async def object_history(
    request: Request,
    oid: Oid,
    s: Session = Depends(current_session),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    base = await _gate(request, s, api)
    if isinstance(base, Response):
        return base
    timeline = object_timeline(base, oid)
    return TEMPLATES.TemplateResponse(
        request,
        "history_object.html",
        {
            "session": s,
            "oid": oid,
            "timeline": timeline,
            "names": _names(base, [v.stamp for v in timeline], snap),
            "status": await _status(api, base),
            "poll_s": POLL_S,
            "labels": LABELS,
            "editors": EDITORS,
            "restorable": RESTORABLE,
        },
    )


@router.get("/{stamp}/{oid}")
async def version_page(
    request: Request,
    stamp: Stamp,
    oid: Oid,
    s: Session = Depends(current_session),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    base = await _gate(request, s, api)
    if isinstance(base, Response):
        return base
    found = find_version(base, stamp, oid)
    if found is None:
        return Response("Not in this snapshot", status_code=404)
    slug, raw = found
    plan = plan_restore(snap, slug, raw) if slug in RESTORABLE else None
    return TEMPLATES.TemplateResponse(
        request,
        "history_version.html",
        {
            "session": s,
            "stamp": stamp,
            "oid": oid,
            "slug": slug,
            "label": LABELS[slug],
            "obj": normalize(slug, raw),
            "plan": plan,
            "exists": oid in snap.policies or oid in snap.groups,
            "editors": EDITORS,
        },
    )


@router.post("/{stamp}/{oid}/preview")
async def restore_preview(
    request: Request,
    stamp: Stamp,
    oid: Oid,
    s: Session = Depends(csrf_protect),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    base = await _gate(request, s, api)
    if isinstance(base, Response):
        return base
    found = find_version(base, stamp, oid)
    if found is None:
        return Response("Not in this snapshot", status_code=404)
    plan = plan_restore(snap, *found)
    if plan.blocked or plan.after is None:
        return TEMPLATES.TemplateResponse(
            request, "_delta.html", {"error": "Cannot restore: " + "; ".join(plan.problems)}
        )
    return TEMPLATES.TemplateResponse(
        request,
        "_delta.html",
        {
            "delta": access_delta(snap, plan.after),
            "snap": snap,
            "limit": PREVIEW_LIMIT,
            "error": "",
        },
    )


@router.post("/{stamp}/{oid}/restore")
async def restore(
    request: Request,
    stamp: Stamp,
    oid: Oid,
    s: Session = Depends(csrf_protect),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    base = await _gate(request, s, api)
    if isinstance(base, Response):
        return base
    found = find_version(base, stamp, oid)
    if found is None:
        return Response("Not in this snapshot", status_code=404)
    slug, _ = found
    plan = plan_restore(snap, *found)
    if plan.blocked:
        return Response("Cannot restore: " + "; ".join(plan.problems), status_code=409)
    try:
        if plan.method == "PUT":
            before = await api.get(plan.path)  # fresh, for the before/after line
            await api.put(plan.path, plan.payload)
            new_id = oid
        else:
            before = None
            created = await api.post(plan.path, plan.payload)
            new_id = str((created if isinstance(created, dict) else {}).get("id") or "")
    except NetBirdError as exc:
        return Response(error_message(exc), status_code=400)
    log_change(s, f"restore {SINGULAR[slug]} {oid} from {stamp} as {new_id}", before, plan.payload)
    config_changed(request, s)
    target = f"{EDITORS[slug]}{new_id}" if new_id else EDITORS[slug].rstrip("/")
    return RedirectResponse(f"{target}?saved=1", status_code=303)
