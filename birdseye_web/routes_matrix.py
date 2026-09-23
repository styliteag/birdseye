"""Access matrix, cell explanation and point-to-point reach query."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response

from birdseye_web.access import grants, reach
from birdseye_web.context import TEMPLATES, current_session, snapshot
from birdseye_web.matrix import (
    Matrix,
    MatrixFilter,
    group_matrix,
    peer_matrix,
    resource_matrix,
    user_matrix,
)
from birdseye_web.models import Ref, Snapshot
from birdseye_web.sessions import Session

router = APIRouter()


@dataclass(frozen=True)
class View:
    key: str
    label: str
    rows: str
    cols: str


VIEWS = (
    View("groups", "Group × Group", "Source group", "Destination group / resource"),
    View("peers", "Peer × Peer", "Source peer", "Destination peer / resource"),
    View("resources", "Group × Resource", "Source group", "Network resource"),
    View("resources-peers", "Peer × Resource", "Source peer", "Network resource"),
    View("users", "User × Destination", "User (via their peers)", "Destination peer / resource"),
)
VIEW_BY_KEY = {v.key: v for v in VIEWS}
PROTOCOLS = ("", "tcp", "udp", "icmp", "netbird-ssh")


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def parse_filter(request: Request) -> tuple[View, MatrixFilter]:
    q = request.query_params
    view = VIEW_BY_KEY.get(q.get("view", "groups"), VIEWS[0])
    proto = q.get("proto", "")
    flt = MatrixFilter(
        row_query=q.get("rq", "")[:100],
        col_query=q.get("cq", "")[:100],
        protocol=proto if proto in PROTOCOLS and proto else None,
        port=_int(q.get("port")),
        hide_all=q.get("hide_all") == "1",
        only_conditional=q.get("cond") == "1",
        max_rows=min(_int(q.get("max")) or 150, 500),
    )
    return view, flt


def _compute(snap: Snapshot, view: View, flt: MatrixFilter) -> Matrix:
    g = grants(snap)
    if view.key == "peers":
        return peer_matrix(snap, g, flt)
    if view.key == "resources":
        return resource_matrix(snap, g, "group", flt)
    if view.key == "resources-peers":
        return resource_matrix(snap, g, "peer", flt)
    if view.key == "users":
        return user_matrix(snap, g, flt)
    return group_matrix(snap, g, flt)


_CACHE: OrderedDict[tuple, tuple[Snapshot, Matrix]] = OrderedDict()
_CACHE_SIZE = 32


def build(snap: Snapshot, view: View, flt: MatrixFilter) -> Matrix:
    """Matrix for one snapshot + view + filter, memoised.

    A new snapshot (TTL expiry or any write) is a new object, so entries go
    stale on their own. The snapshot is stored with the entry so its id()
    cannot be reused while the entry lives.
    """
    key = (id(snap), view.key, flt)
    hit = _CACHE.get(key)
    if hit is not None and hit[0] is snap:
        _CACHE.move_to_end(key)
        return hit[1]
    m = _compute(snap, view, flt)
    _CACHE[key] = (snap, m)
    while len(_CACHE) > _CACHE_SIZE:
        _CACHE.popitem(last=False)
    return m


@router.get("/")
async def root() -> Response:
    return RedirectResponse("/matrix", status_code=303)


@router.get("/matrix")
async def matrix_page(
    request: Request,
    s: Session = Depends(current_session),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    view, flt = parse_filter(request)
    m = build(snap, view, flt)
    ctx = {
        "session": s,
        "view": view,
        "views": VIEWS,
        "flt": flt,
        "protocols": PROTOCOLS,
        "m": m,
        "query": request.url.query,
        "snap": snap,
    }
    tpl = "_matrix_table.html" if request.headers.get("HX-Target") == "matrix" else "matrix.html"
    return TEMPLATES.TemplateResponse(request, tpl, ctx)


def _rule(snap: Snapshot, policy_id: str, rule_id: str):
    pol = snap.policies.get(policy_id)
    rule = next((r for r in pol.rules if r.id == rule_id), None) if pol else None
    return pol, rule


@router.get("/matrix/cell")
async def cell_detail(
    request: Request,
    row: str,
    col: str,
    s: Session = Depends(current_session),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    view, flt = parse_filter(request)
    m = build(
        snap,
        view,
        MatrixFilter(
            **{
                **flt.__dict__,
                "row_query": "",
                "col_query": "",
                "max_rows": 10_000,
                "max_cols": 10_000,
            }
        ),
    )
    cell = m.cell(row, col)
    axes = {a.key: a for a in (*m.rows, *m.cols)}
    items = []
    for g in cell.grants if cell else ():
        pol, rule = _rule(snap, g.policy_id, g.rule_id)
        items.append(
            {
                "grant": g,
                "policy": pol,
                "rule": rule,
                "posture": [
                    snap.posture_checks[c].name if c in snap.posture_checks else c
                    for c in g.conditional
                ],
            }
        )
    via = sorted(
        (axes[k].label if k in axes else _label(snap, k)) for k in (cell.via if cell else ())
    )
    return TEMPLATES.TemplateResponse(
        request,
        "_cell_detail.html",
        {
            "session": s,
            "row": axes.get(row),
            "col": axes.get(col),
            "cell": cell,
            "items": items,
            "via": via,
            "view": view,
        },
    )


def _label(snap: Snapshot, key: str) -> str:
    kind, _, oid = key.partition(":")
    if kind == "p" and oid in snap.peers:
        return snap.peers[oid].name
    if kind == "g" and oid in snap.groups:
        return snap.groups[oid].name
    if kind == "r" and oid in snap.resources:
        return snap.resources[oid].name
    return oid


def _ref(key: str) -> Ref | None:
    kind, _, oid = key.partition(":")
    if not oid or kind not in ("p", "r"):
        return None
    return Ref("peer" if kind == "p" else "resource", oid)


@router.get("/reach")
async def reach_query(
    request: Request,
    s: Session = Depends(current_session),
    snap: Snapshot = Depends(snapshot),
) -> Response:
    q = request.query_params
    src, dst = _ref(q.get("src", "")), _ref(q.get("dst", ""))
    proto = q.get("proto") or None
    port = _int(q.get("port"))
    hits = reach(snap, src, dst, protocol=proto, port=port) if src and dst else ()
    rows = []
    for e in hits:
        pol, rule = _rule(snap, e.grant.policy_id, e.grant.rule_id)
        rows.append(
            {
                "edge": e,
                "policy": pol,
                "rule": rule,
                "src_group": _label(snap, f"g:{e.src_group}") if e.src_group else "direct",
                "dst_group": _label(snap, f"g:{e.dst_group}") if e.dst_group else "direct",
            }
        )
    ctx = {
        "session": s,
        "snap": snap,
        "src": q.get("src", ""),
        "dst": q.get("dst", ""),
        "proto": proto or "",
        "port": port or "",
        "asked": bool(src and dst),
        "rows": rows,
        "src_label": _label(snap, q.get("src", "")),
        "dst_label": _label(snap, q.get("dst", "")),
        "protocols": PROTOCOLS,
    }
    tpl = "_reach_result.html" if request.headers.get("HX-Request") else "reach.html"
    return TEMPLATES.TemplateResponse(request, tpl, ctx)
