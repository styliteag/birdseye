"""Networks (NetBird "Networks" feature): networks, their resources and routers.

Resources carry groups, so a resource edit can change who reaches what: the
resource editor previews the access delta like the group editor does.
Routers decide whether a resource is routed at all; they do not change the
policy-level access the matrix shows, so router edits have no preview.
"""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import quote

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
    snapshot,
    user_api,
)
from birdseye_web.diff import access_delta, with_resource, without_network, without_resource
from birdseye_web.models import Network, Resource, Router, Snapshot, parse_resource
from birdseye_web.nbapi import NetBirdAPI, NetBirdError
from birdseye_web.payloads import (
    PayloadError,
    network_payload,
    resource_payload,
    router_payload,
)
from birdseye_web.sessions import Session

router = APIRouter()
PREVIEW_LIMIT = 60
Oid = Annotated[str, Path(pattern=OBJECT_ID)]


def _q(oid: str) -> str:
    return quote(oid, safe="")


def _sorted_groups(snap: Snapshot):
    return sorted((g for g in snap.groups.values() if not g.is_all), key=lambda g: g.name.lower())


def _delta(request, snap, delta):
    return TEMPLATES.TemplateResponse(
        request,
        "_delta.html",
        {"delta": delta, "snap": snap, "limit": PREVIEW_LIMIT, "error": ""},
    )


def _done(request: Request, url: str) -> Response:
    ctx(request).cache.invalidate()
    return RedirectResponse(url, status_code=303)


# --- networks --------------------------------------------------------------------------


@router.get("/networks")
async def network_list(
    request: Request, s: Session = Depends(current_session), snap: Snapshot = Depends(snapshot)
) -> Response:
    nets = sorted(snap.networks.values(), key=lambda n: n.name.lower())
    resources = {
        n.id: sorted(
            (r for r in snap.resources.values() if r.network_id == n.id),
            key=lambda r: r.name.lower(),
        )
        for n in nets
    }
    return TEMPLATES.TemplateResponse(
        request,
        "networks.html",
        {"session": s, "snap": snap, "networks": nets, "resources": resources},
    )


def _network_page(request, s, snap, net: Network | None, error="", values=None, status=200):
    res = sorted(
        (r for r in snap.resources.values() if net and r.network_id == net.id),
        key=lambda r: r.name.lower(),
    )
    return TEMPLATES.TemplateResponse(
        request,
        "network_edit.html",
        {
            "session": s,
            "snap": snap,
            "net": net,
            "v": values
            or {"name": net.name if net else "", "description": net.description if net else ""},
            "resources": res,
            "can": s.can("networks", "update") or s.is_admin,
            "error": error,
        },
        status_code=status,
    )


@router.get("/networks/new")
async def network_new(
    request: Request, s: Session = Depends(current_session), snap: Snapshot = Depends(snapshot)
) -> Response:
    return _network_page(request, s, snap, None)


@router.post("/networks")
async def network_create(
    request: Request,
    s: Session = Depends(csrf_protect),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    form = await request.form()
    values = {"name": str(form.get("name", "")), "description": str(form.get("description", ""))}
    try:
        body = network_payload(values["name"], values["description"])
        created = await api.post("networks", body)
    except (PayloadError, NetBirdError) as exc:
        return _network_page(request, s, snap, None, error_message(exc), values, 400)
    log_change(s, "create network", None, body)
    return _done(request, f"/networks/{created['id']}?saved=1")


@router.get("/networks/{nid}")
async def network_edit(
    request: Request,
    nid: Oid,
    s: Session = Depends(current_session),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    net = snap.networks.get(nid)
    if net is None:
        return Response("Network not found", status_code=404)
    return _network_page(request, s, snap, net)


@router.post("/networks/{nid}")
async def network_update(
    request: Request,
    nid: Oid,
    s: Session = Depends(csrf_protect),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    net = snap.networks.get(nid)
    if net is None:
        return Response("Network not found", status_code=404)
    form = await request.form()
    values = {"name": str(form.get("name", "")), "description": str(form.get("description", ""))}
    try:
        fresh = await api.get(f"networks/{_q(nid)}")
        before = {"name": fresh.get("name"), "description": fresh.get("description") or ""}
        body = network_payload(values["name"], values["description"])
        if body != before:
            await api.put(f"networks/{_q(nid)}", body)
            log_change(s, f"update network {nid}", before, body)
    except (PayloadError, NetBirdError) as exc:
        return _network_page(request, s, snap, net, error_message(exc), values, 400)
    return _done(request, f"/networks/{nid}?saved=1")


@router.post("/networks/{nid}/delete-preview")
async def network_delete_preview(
    request: Request,
    nid: Oid,
    s: Session = Depends(csrf_protect),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    """Separate route on purpose: a preview must never share the delete handler."""
    return _delta(request, snap, access_delta(snap, without_network(snap, nid)))


@router.post("/networks/{nid}/delete")
async def network_delete(
    request: Request,
    nid: Oid,
    s: Session = Depends(csrf_protect),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    net = snap.networks.get(nid)
    if net is None:
        return Response("Network not found", status_code=404)
    try:
        await api.delete(f"networks/{_q(nid)}")
    except NetBirdError as exc:
        return _network_page(request, s, snap, net, error_message(exc), status=400)
    log_change(s, f"delete network {nid}", net.name, None)
    return _done(request, "/networks?deleted=1")


# --- resources -------------------------------------------------------------------------


@router.get("/resources/{rid}")
async def resource_link(rid: Oid, snap: Snapshot = Depends(snapshot)) -> Response:
    """Stable link for a resource by ID (matrix, peer editor, audit log)."""
    res = snap.resources.get(rid)
    if res is None:
        return Response("Resource not found", status_code=404)
    return RedirectResponse(f"/networks/{res.network_id}/resources/{rid}", status_code=303)


def _resource_values(form: FormData) -> dict[str, Any]:
    return {
        "name": str(form.get("name", "")),
        "address": str(form.get("address", "")),
        "description": str(form.get("description", "")),
        "groups": [str(g) for g in form.getlist("groups")],
        "enabled": bool(form.get("enabled")),
    }


def _resource_body(values: dict[str, Any]) -> dict[str, Any]:
    return resource_payload(
        name=values["name"],
        address=values["address"],
        groups=values["groups"],
        enabled=values["enabled"],
        description=values["description"],
    )


def _resource_page(request, s, snap, net, res: Resource | None, error="", values=None, status=200):
    if values is None:
        values = {
            "name": res.name if res else "",
            "address": res.address if res else "",
            "description": res.description if res else "",
            "groups": sorted(res.group_ids) if res else [],
            "enabled": res.enabled if res else True,
        }
    return TEMPLATES.TemplateResponse(
        request,
        "resource_edit.html",
        {
            "session": s,
            "snap": snap,
            "net": net,
            "res": res,
            "v": values,
            "groups": _sorted_groups(snap),
            "can": s.can("networks", "update") or s.is_admin,
            "error": error,
        },
        status_code=status,
    )


def _simulated(snap: Snapshot, nid: str, rid: str, values: dict[str, Any]) -> Snapshot:
    """Snapshot as if the form were saved (a new resource gets a dummy ID)."""
    old = snap.resources.get(rid)
    raw = {**_resource_body(values), "id": rid, "type": old.type if old else ""}
    return with_resource(snap, parse_resource(raw, nid))


@router.get("/networks/{nid}/resources/new")
async def resource_new(
    request: Request,
    nid: Oid,
    s: Session = Depends(current_session),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    net = snap.networks.get(nid)
    if net is None:
        return Response("Network not found", status_code=404)
    return _resource_page(request, s, snap, net, None)


@router.post("/networks/{nid}/resources")
async def resource_create(
    request: Request,
    nid: Oid,
    s: Session = Depends(csrf_protect),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    net = snap.networks.get(nid)
    if net is None:
        return Response("Network not found", status_code=404)
    values = _resource_values(await request.form())
    try:
        body = _resource_body(values)
        created = await api.post(f"networks/{_q(nid)}/resources", body)
    except (PayloadError, NetBirdError) as exc:
        return _resource_page(request, s, snap, net, None, error_message(exc), values, 400)
    log_change(s, f"create resource in network {nid}", None, body)
    return _done(request, f"/networks/{nid}/resources/{created['id']}?saved=1")


@router.post("/networks/{nid}/resources/preview")
async def resource_new_preview(
    request: Request,
    nid: Oid,
    s: Session = Depends(csrf_protect),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    return await _resource_preview(request, snap, nid, "__new__")


async def _resource_preview(request: Request, snap: Snapshot, nid: str, rid: str) -> Response:
    try:
        after = _simulated(snap, nid, rid, _resource_values(await request.form()))
    except PayloadError as exc:
        return TEMPLATES.TemplateResponse(request, "_delta.html", {"error": str(exc)})
    return _delta(request, snap, access_delta(snap, after))


def _find(snap: Snapshot, nid: str, rid: str) -> tuple[Network | None, Resource | None]:
    net, res = snap.networks.get(nid), snap.resources.get(rid)
    if net is None or res is None or res.network_id != nid:
        return None, None
    return net, res


@router.get("/networks/{nid}/resources/{rid}")
async def resource_edit(
    request: Request,
    nid: Oid,
    rid: Oid,
    s: Session = Depends(current_session),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    net, res = _find(snap, nid, rid)
    if res is None:
        return Response("Resource not found", status_code=404)
    return _resource_page(request, s, snap, net, res)


@router.post("/networks/{nid}/resources/{rid}")
async def resource_update(
    request: Request,
    nid: Oid,
    rid: Oid,
    s: Session = Depends(csrf_protect),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    net, res = _find(snap, nid, rid)
    if res is None:
        return Response("Resource not found", status_code=404)
    values = _resource_values(await request.form())
    path = f"networks/{_q(nid)}/resources/{_q(rid)}"
    try:
        body = _resource_body(values)
        fresh = await api.get(path)
        before = {
            "name": fresh.get("name"),
            "address": fresh.get("address"),
            "groups": sorted(
                str(g["id"]) if isinstance(g, dict) else str(g) for g in fresh.get("groups") or ()
            ),
            "enabled": bool(fresh.get("enabled", True)),
            "description": fresh.get("description") or "",
        }
        if body != before:
            await api.put(path, body)
            log_change(s, f"update resource {rid}", before, body)
    except (PayloadError, NetBirdError) as exc:
        return _resource_page(request, s, snap, net, res, error_message(exc), values, 400)
    return _done(request, f"/networks/{nid}/resources/{rid}?saved=1")


@router.post("/networks/{nid}/resources/{rid}/preview")
async def resource_preview(
    request: Request,
    nid: Oid,
    rid: Oid,
    s: Session = Depends(csrf_protect),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    if _find(snap, nid, rid)[1] is None:
        return Response("Resource not found", status_code=404)
    return await _resource_preview(request, snap, nid, rid)


@router.post("/networks/{nid}/resources/{rid}/delete-preview")
async def resource_delete_preview(
    request: Request,
    nid: Oid,
    rid: Oid,
    s: Session = Depends(csrf_protect),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    """Separate route on purpose: a preview must never share the delete handler."""
    return _delta(request, snap, access_delta(snap, without_resource(snap, rid)))


@router.post("/networks/{nid}/resources/{rid}/delete")
async def resource_delete(
    request: Request,
    nid: Oid,
    rid: Oid,
    s: Session = Depends(csrf_protect),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    net, res = _find(snap, nid, rid)
    if res is None:
        return Response("Resource not found", status_code=404)
    try:
        await api.delete(f"networks/{_q(nid)}/resources/{_q(rid)}")
    except NetBirdError as exc:
        return _resource_page(request, s, snap, net, res, error_message(exc), status=400)
    log_change(s, f"delete resource {rid}", res.name, None)
    return _done(request, f"/networks/{nid}?deleted=1")


# --- routers ---------------------------------------------------------------------------


def _router_values(form: FormData) -> dict[str, Any]:
    mode = str(form.get("mode", "peer"))
    try:
        metric = int(str(form.get("metric", "9999")).strip())
    except ValueError:
        metric = 0
    return {
        "mode": mode,
        "peer": str(form.get("peer", "")) if mode == "peer" else "",
        "peer_groups": [str(g) for g in form.getlist("peer_groups")] if mode == "group" else [],
        "metric": metric,
        "masquerade": bool(form.get("masquerade")),
        "enabled": bool(form.get("enabled")),
    }


def _router_body(values: dict[str, Any]) -> dict[str, Any]:
    return router_payload(
        peer=values["peer"],
        peer_groups=values["peer_groups"],
        metric=values["metric"],
        masquerade=values["masquerade"],
        enabled=values["enabled"],
    )


def _router_page(request, s, snap, net, rt: Router | None, error="", values=None, status=200):
    if values is None:
        values = {
            "mode": "group" if rt and rt.peer_groups else "peer",
            "peer": rt.peer if rt else "",
            "peer_groups": list(rt.peer_groups) if rt else [],
            "metric": rt.metric if rt else 9999,
            "masquerade": rt.masquerade if rt else True,
            "enabled": rt.enabled if rt else True,
        }
    return TEMPLATES.TemplateResponse(
        request,
        "router_edit.html",
        {
            "session": s,
            "snap": snap,
            "net": net,
            "rt": rt,
            "v": values,
            "peers": sorted(snap.peers.values(), key=lambda p: p.name.lower()),
            "groups": _sorted_groups(snap),
            "can": s.can("networks", "update") or s.is_admin,
            "error": error,
        },
        status_code=status,
    )


def _router(snap: Snapshot, nid: str, rtid: str) -> tuple[Network | None, Router | None]:
    net = snap.networks.get(nid)
    rt = next((r for r in net.routers if r.id == rtid), None) if net else None
    return net, rt


@router.get("/networks/{nid}/routers/new")
async def router_new(
    request: Request,
    nid: Oid,
    s: Session = Depends(current_session),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    net = snap.networks.get(nid)
    if net is None:
        return Response("Network not found", status_code=404)
    return _router_page(request, s, snap, net, None)


@router.post("/networks/{nid}/routers")
async def router_create(
    request: Request,
    nid: Oid,
    s: Session = Depends(csrf_protect),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    net = snap.networks.get(nid)
    if net is None:
        return Response("Network not found", status_code=404)
    values = _router_values(await request.form())
    try:
        body = _router_body(values)
        await api.post(f"networks/{_q(nid)}/routers", body)
    except (PayloadError, NetBirdError) as exc:
        return _router_page(request, s, snap, net, None, error_message(exc), values, 400)
    log_change(s, f"create router in network {nid}", None, body)
    return _done(request, f"/networks/{nid}?saved=1")


@router.get("/networks/{nid}/routers/{rtid}")
async def router_edit(
    request: Request,
    nid: Oid,
    rtid: Oid,
    s: Session = Depends(current_session),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    net, rt = _router(snap, nid, rtid)
    if rt is None:
        return Response("Router not found", status_code=404)
    return _router_page(request, s, snap, net, rt)


@router.post("/networks/{nid}/routers/{rtid}")
async def router_update(
    request: Request,
    nid: Oid,
    rtid: Oid,
    s: Session = Depends(csrf_protect),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    net, rt = _router(snap, nid, rtid)
    if rt is None:
        return Response("Router not found", status_code=404)
    values = _router_values(await request.form())
    path = f"networks/{_q(nid)}/routers/{_q(rtid)}"
    try:
        body = _router_body(values)
        fresh = await api.get(path)
        before = {k: fresh.get(k) for k in body}
        if body != before:
            await api.put(path, body)
            log_change(s, f"update router {rtid}", before, body)
    except (PayloadError, NetBirdError) as exc:
        return _router_page(request, s, snap, net, rt, error_message(exc), values, 400)
    return _done(request, f"/networks/{nid}?saved=1")


@router.post("/networks/{nid}/routers/{rtid}/delete")
async def router_delete(
    request: Request,
    nid: Oid,
    rtid: Oid,
    s: Session = Depends(csrf_protect),
    api: NetBirdAPI = Depends(user_api),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    net, rt = _router(snap, nid, rtid)
    if rt is None:
        return Response("Router not found", status_code=404)
    try:
        await api.delete(f"networks/{_q(nid)}/routers/{_q(rtid)}")
    except NetBirdError as exc:
        return _router_page(request, s, snap, net, rt, error_message(exc), status=400)
    log_change(s, f"delete router {rtid}", {"peer": rt.peer, "peer_groups": rt.peer_groups}, None)
    return _done(request, f"/networks/{nid}?deleted=1")
