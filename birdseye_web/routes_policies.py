"""Policy list and editor, with an access-delta preview before saving."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import RedirectResponse, Response
from starlette.datastructures import FormData

from birdseye_web.context import (
    OBJECT_ID,
    TEMPLATES,
    csrf_protect,
    ctx,
    current_session,
    error_message,
    log_change,
    safe_path,
    snapshot,
    user_api,
)
from birdseye_web.diff import access_delta, with_policy, without_policy
from birdseye_web.models import Snapshot
from birdseye_web.nbapi import NetBirdAPI, NetBirdError
from birdseye_web.payloads import (
    PROTOCOLS,
    PayloadError,
    build_rule,
    policy_for_put,
    rule_for_put,
)
from birdseye_web.sessions import Session

router = APIRouter(prefix="/policies")
PREVIEW_LIMIT = 60


# --- form <-> payload ----------------------------------------------------------------


def _dst_resource(value: str) -> dict[str, str] | None:
    """Select value `peer:<id>` or `<type>:<id>` for a network resource."""
    kind, _, oid = value.partition(":")
    return {"id": oid, "type": kind} if oid else None


def policy_from_form(form: FormData) -> dict[str, Any]:
    rules = []
    for idx in form.getlist("rule_idx"):
        p = f"rules-{idx}-"
        rules.append(
            build_rule(
                name=str(form.get(p + "name") or form.get("name") or ""),
                description=str(form.get(p + "description", "")),
                sources=[str(x) for x in form.getlist(p + "sources")],
                destinations=[str(x) for x in form.getlist(p + "destinations")],
                destination_resource=_dst_resource(str(form.get(p + "dst_resource", ""))),
                protocol=str(form.get(p + "protocol", "all")),
                ports=str(form.get(p + "ports", "")),
                bidirectional=form.get(p + "bidirectional") == "on",
                action=str(form.get(p + "action", "accept")),
                enabled=form.get(p + "enabled") == "on",
                rule_id=str(form.get(p + "id", "")) or None,
            )
        )
    if not rules:
        raise PayloadError("A policy needs at least one rule.")
    name = str(form.get("name", "")).strip()
    if not name:
        raise PayloadError("Policy name is required.")
    return {
        "name": name,
        "description": str(form.get("description", "")),
        "enabled": form.get("enabled") == "on",
        "source_posture_checks": [str(x) for x in form.getlist("posture")],
        "rules": rules,
    }


def _raw_policy(snap: Snapshot, pid: str) -> dict[str, Any] | None:
    """Editor-shaped dict from the parsed snapshot (IDs, not embedded groups)."""
    pol = snap.policies.get(pid)
    if pol is None:
        return None
    return {
        "id": pol.id,
        "name": pol.name,
        "description": pol.description,
        "enabled": pol.enabled,
        "source_posture_checks": list(pol.posture_check_ids),
        "rules": [
            {
                "id": r.id,
                "name": r.name,
                "description": r.description,
                "enabled": r.enabled,
                "action": r.action,
                "bidirectional": r.bidirectional,
                "protocol": r.service.protocol,
                "ports_text": ", ".join(x.label() for x in r.service.ports),
                "sources": list(r.source_group_ids),
                "destinations": list(r.destination_group_ids),
                "dst_resource": _ref_value(snap, r.destination_ref),
            }
            for r in pol.rules
        ],
    }


def _ref_value(snap: Snapshot, ref) -> str:
    if ref is None:
        return ""
    if ref.kind == "peer":
        return f"peer:{ref.id}"
    res = snap.resources.get(ref.id)
    return f"{res.type if res else 'subnet'}:{ref.id}"


def _form_to_editor(form_policy: dict[str, Any], snap: Snapshot) -> dict[str, Any]:
    rules = []
    for r in form_policy.get("rules", []):
        dr = r.get("destinationResource")
        ports = list(r.get("ports") or []) + [
            f"{x['start']}-{x['end']}" for x in r.get("port_ranges") or []
        ]
        rules.append(
            {
                **r,
                "ports_text": ", ".join(ports),
                "destinations": r.get("destinations", []),
                "dst_resource": f"{dr['type']}:{dr['id']}" if dr else "",
            }
        )
    return {**form_policy, "rules": rules}


_KEEP = ("authorized_groups", "sourceResource")


def carry_over(before: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """Keep rule fields the editor does not show (e.g. SSH `authorized_groups`)."""
    old = {r.get("id"): rule_for_put(r) for r in before.get("rules") or () if r.get("id")}
    rules = []
    for r in body["rules"]:
        prev = old.get(r.get("id"), {})
        extra = {k: prev[k] for k in _KEEP if k in prev and k not in r}
        if "sourceResource" in extra:
            r = {k: v for k, v in r.items() if k != "sources"}
        rules.append({**r, **extra})
    return {**body, "rules": rules}


def _empty_rule(idx: int = 0) -> dict[str, Any]:
    return {
        "idx": idx,
        "id": "",
        "name": "",
        "description": "",
        "enabled": True,
        "action": "accept",
        "bidirectional": False,
        "protocol": "all",
        "ports_text": "",
        "sources": [],
        "destinations": [],
        "dst_resource": "",
    }


# --- pages ----------------------------------------------------------------------------


@router.get("")
async def policy_list(
    request: Request, s: Session = Depends(current_session), snap: Snapshot = Depends(snapshot)
) -> Response:
    q = request.query_params.get("q", "").strip().lower()
    policies = sorted(
        (
            p
            for p in snap.policies.values()
            if not q or q in p.name.lower() or q in p.description.lower()
        ),
        key=lambda p: p.name.lower(),
    )
    tpl = (
        "_policy_rows.html"
        if request.headers.get("HX-Target") == "policy-rows"
        else "policies.html"
    )
    return TEMPLATES.TemplateResponse(
        request, tpl, {"session": s, "policies": policies, "snap": snap, "q": q}
    )


def _editor(request, s, snap, policy, error="", status=200, pid=""):
    return TEMPLATES.TemplateResponse(
        request,
        "policy_edit.html",
        {
            "session": s,
            "snap": snap,
            "policy": policy,
            "pid": pid,
            "error": error,
            "protocols": PROTOCOLS,
            "groups": sorted(snap.groups.values(), key=lambda g: g.name.lower()),
            "resources": sorted(snap.resources.values(), key=lambda r: r.name.lower()),
            "peers": sorted(snap.peers.values(), key=lambda p: p.name.lower()),
            "posture_checks": sorted(snap.posture_checks.values(), key=lambda c: c.name.lower()),
        },
        status_code=status,
    )


@router.get("/new")
async def policy_new(
    request: Request, s: Session = Depends(current_session), snap: Snapshot = Depends(snapshot)
) -> Response:
    blank = {
        "name": "",
        "description": "",
        "enabled": True,
        "source_posture_checks": [],
        "rules": [_empty_rule()],
    }
    return _editor(request, s, snap, blank)


@router.get("/rule-row")
async def rule_row(
    request: Request,
    idx: int,
    s: Session = Depends(current_session),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    return TEMPLATES.TemplateResponse(
        request,
        "_rule_fields.html",
        {
            "r": _empty_rule(idx),
            "idx": idx,
            "protocols": PROTOCOLS,
            "groups": sorted(snap.groups.values(), key=lambda g: g.name.lower()),
            "resources": sorted(snap.resources.values(), key=lambda r: r.name.lower()),
            "peers": sorted(snap.peers.values(), key=lambda p: p.name.lower()),
        },
    )


@router.get("/{pid}")
async def policy_edit(
    request: Request,
    pid: Annotated[str, Path(pattern=OBJECT_ID)],
    s: Session = Depends(current_session),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    raw = _raw_policy(snap, pid)
    if raw is None:
        return Response("Policy not found", status_code=404)
    return _editor(request, s, snap, raw, pid=pid)


@router.post("/preview")
async def policy_preview(
    request: Request, s: Session = Depends(csrf_protect), snap: Snapshot = Depends(snapshot)
) -> Response:
    form = await request.form()
    pid = str(form.get("pid", "")) or "__new__"
    try:
        body = policy_from_form(form)
    except PayloadError as exc:
        return TEMPLATES.TemplateResponse(request, "_delta.html", {"error": str(exc)})
    delta = access_delta(snap, with_policy(snap, {**body, "id": pid}))
    return _delta(request, snap, delta)


def _delta(request, snap, delta):
    return TEMPLATES.TemplateResponse(
        request,
        "_delta.html",
        {"delta": delta, "snap": snap, "limit": PREVIEW_LIMIT, "error": ""},
    )


async def _save(request, s, api, snap, pid: str | None) -> Response:
    form = await request.form()
    try:
        body = policy_from_form(form)
        if pid:
            before = await api.get(f"policies/{pid}")
            body = carry_over(before, body)
            await api.put(f"policies/{pid}", body)
            log_change(s, f"update policy {pid}", policy_for_put(before), body)
        else:
            created = await api.post("policies", body)
            pid = str(created["id"])
            log_change(s, "create policy", None, body)
    except (PayloadError, NetBirdError) as exc:
        editor = _form_to_editor(_partial_form(form), snap)
        return _editor(request, s, snap, editor, error_message(exc), 400, pid or "")
    ctx(request).cache.invalidate()
    return RedirectResponse(f"/policies/{pid}?saved=1", status_code=303)


def _partial_form(form: FormData) -> dict[str, Any]:
    """Re-render what the user typed after a failed save (no validation)."""
    rules = []
    for idx in form.getlist("rule_idx"):
        p = f"rules-{idx}-"
        rules.append(
            {
                "idx": idx,
                "id": form.get(p + "id", ""),
                "name": form.get(p + "name", ""),
                "description": form.get(p + "description", ""),
                "enabled": form.get(p + "enabled") == "on",
                "action": form.get(p + "action", "accept"),
                "bidirectional": form.get(p + "bidirectional") == "on",
                "protocol": form.get(p + "protocol", "all"),
                "ports": [],
                "ports_text": form.get(p + "ports", ""),
                "sources": form.getlist(p + "sources"),
                "destinations": form.getlist(p + "destinations"),
                "destinationResource": _dst_resource(str(form.get(p + "dst_resource", ""))),
            }
        )
    return {
        "name": form.get("name", ""),
        "description": form.get("description", ""),
        "enabled": form.get("enabled") == "on",
        "source_posture_checks": form.getlist("posture"),
        "rules": rules,
    }


@router.post("")
async def policy_create(
    request: Request,
    s: Session = Depends(csrf_protect),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    return await _save(request, s, api, snap, None)


@router.post("/{pid}")
async def policy_update(
    request: Request,
    pid: Annotated[str, Path(pattern=OBJECT_ID)],
    s: Session = Depends(csrf_protect),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    return await _save(request, s, api, snap, pid)


@router.post("/{pid}/toggle")
async def policy_toggle(
    request: Request,
    pid: Annotated[str, Path(pattern=OBJECT_ID)],
    s: Session = Depends(csrf_protect),
    api: NetBirdAPI = Depends(user_api),
) -> Response:
    current = await api.get(f"policies/{pid}")
    body = policy_for_put(current, enabled=not current.get("enabled", True))
    await api.put(f"policies/{pid}", body)
    log_change(s, f"toggle policy {pid}", current.get("enabled"), body["enabled"])
    ctx(request).cache.invalidate()
    return RedirectResponse(safe_path(request.headers.get("referer"), "/policies"), status_code=303)


@router.post("/{pid}/delete-preview")
async def policy_delete_preview(
    request: Request,
    pid: Annotated[str, Path(pattern=OBJECT_ID)],
    s: Session = Depends(csrf_protect),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    """Separate route on purpose: a preview must never share the delete handler."""
    return _delta(request, snap, access_delta(snap, without_policy(snap, pid)))


@router.post("/{pid}/delete")
async def policy_delete(
    request: Request,
    pid: Annotated[str, Path(pattern=OBJECT_ID)],
    s: Session = Depends(csrf_protect),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    pol = snap.policies.get(pid)
    await api.delete(f"policies/{pid}")
    log_change(s, f"delete policy {pid}", pol.name if pol else pid, None)
    ctx(request).cache.invalidate()
    return RedirectResponse("/policies?deleted=1", status_code=303)
