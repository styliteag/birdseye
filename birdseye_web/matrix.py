"""Matrix views built from grants/edges.

Axis keys are prefixed by kind: `g:` group, `p:` peer, `r:` resource,
`u:` user. Every view goes through `access.grants()` and, for concrete
endpoints, `access.edges()`, so the four views cannot disagree.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from birdseye_web.access import Grant, edges, group_members, service_matches
from birdseye_web.models import Ref, Snapshot


@dataclass(frozen=True)
class Axis:
    key: str
    label: str
    sub: str = ""  # secondary text: IP, address, e-mail


@dataclass(frozen=True)
class Cell:
    grants: tuple[Grant, ...]
    via: frozenset[str] = frozenset()  # source endpoint keys that produce this cell

    @property
    def service_labels(self) -> tuple[str, ...]:
        return tuple(sorted({g.service.label() for g in self.grants}))

    @property
    def conditional(self) -> bool:
        """True when no grant reaches the target without a posture check."""
        return all(g.conditional for g in self.grants)

    @property
    def denied(self) -> bool:
        return any(g.action == "drop" for g in self.grants)

    @property
    def is_all(self) -> bool:
        return any(g.service.protocol == "all" for g in self.grants)


@dataclass(frozen=True)
class MatrixFilter:
    row_query: str = ""
    col_query: str = ""
    protocol: str | None = None
    port: int | None = None
    hide_all: bool = False
    only_conditional: bool = False
    max_rows: int = 150
    max_cols: int = 300


@dataclass(frozen=True)
class Matrix:
    rows: tuple[Axis, ...]
    cols: tuple[Axis, ...]
    cells: Mapping[tuple[str, str], Cell] = field(default_factory=dict)
    truncated: bool = False

    def cell(self, row_key: str, col_key: str) -> Cell | None:
        return self.cells.get((row_key, col_key))


# --- axis helpers ----------------------------------------------------------------


def ref_key(ref: Ref) -> str:
    return ("p:" if ref.kind == "peer" else "r:") + ref.id


def _axis(snap: Snapshot, key: str) -> Axis:
    kind, _, oid = key.partition(":")
    if kind == "g":
        g = snap.groups.get(oid)
        return Axis(key, g.name if g else oid)
    if kind == "p":
        p = snap.peers.get(oid)
        return Axis(key, p.name if p else oid, p.ip if p else "")
    if kind == "r":
        r = snap.resources.get(oid)
        return Axis(key, r.name if r else oid, r.address if r else "")
    u = snap.users.get(oid)
    return Axis(key, u.label if u else oid, u.email if u else "")


def _all_group_ids(snap: Snapshot) -> frozenset[str]:
    return frozenset(g.id for g in snap.groups.values() if g.is_all)


def _filter_grants(grant_list: Iterable[Grant], flt: MatrixFilter, snap: Snapshot):
    all_ids = _all_group_ids(snap) if flt.hide_all else frozenset()
    for g in grant_list:
        if not service_matches(g.service, flt.protocol, flt.port):
            continue
        if flt.only_conditional and not g.conditional:
            continue
        if all_ids:
            g = Grant(
                **{
                    **g.__dict__,
                    "src_groups": tuple(x for x in g.src_groups if x not in all_ids),
                    "dst_groups": tuple(x for x in g.dst_groups if x not in all_ids),
                }
            )
        yield g


def _matches(axis: Axis, query: str) -> bool:
    q = query.strip().lower()
    return not q or q in axis.label.lower() or q in axis.sub.lower()


def _assemble(
    snap: Snapshot,
    raw: Mapping[tuple[str, str], tuple[list[Grant], set[str]]],
    flt: MatrixFilter,
) -> Matrix:
    row_keys = {r for r, _ in raw}
    col_keys = {c for _, c in raw}
    sort = lambda a: (a.label.lower(), a.key)  # noqa: E731
    rows = sorted(
        (a for a in map(lambda k: _axis(snap, k), row_keys) if _matches(a, flt.row_query)), key=sort
    )
    cols = sorted(
        (a for a in map(lambda k: _axis(snap, k), col_keys) if _matches(a, flt.col_query)), key=sort
    )
    truncated = len(rows) > flt.max_rows or len(cols) > flt.max_cols
    rows, cols = rows[: flt.max_rows], cols[: flt.max_cols]
    row_set, col_set = {a.key for a in rows}, {a.key for a in cols}
    live = {k: v for k, v in raw.items() if k[0] in row_set and k[1] in col_set}
    used_rows = {r for r, _ in live}
    used_cols = {c for _, c in live}
    cells = {k: Cell(tuple(dict.fromkeys(gs)), frozenset(via)) for k, (gs, via) in live.items()}
    return Matrix(
        rows=tuple(a for a in rows if a.key in used_rows),
        cols=tuple(a for a in cols if a.key in used_cols),
        cells=cells,
        truncated=truncated,
    )


def _collector():
    return defaultdict(lambda: ([], set()))


# --- views -----------------------------------------------------------------------


def group_matrix(snap: Snapshot, grant_list, flt: MatrixFilter = MatrixFilter()) -> Matrix:
    """Declared view: rule source groups x rule destination groups/refs."""
    raw = _collector()
    for g in _filter_grants(grant_list, flt, snap):
        srcs = [f"g:{x}" for x in g.src_groups] + ([ref_key(g.src_ref)] if g.src_ref else [])
        dsts = [f"g:{x}" for x in g.dst_groups] + ([ref_key(g.dst_ref)] if g.dst_ref else [])
        for s in srcs:
            for d in dsts:
                entry = raw[(s, d)]
                entry[0].append(g)
                entry[1].add(s)
    return _assemble(snap, raw, flt)


def _edge_view(snap, grant_list, flt, row_of, dst_kind: str | None = None) -> Matrix:
    raw = _collector()
    for e in edges(snap, tuple(_filter_grants(grant_list, flt, snap))):
        if dst_kind and e.dst.kind != dst_kind:
            continue
        for row in row_of(e):
            entry = raw[(row, ref_key(e.dst))]
            entry[0].append(e.grant)
            entry[1].add(ref_key(e.src))
    return _assemble(snap, raw, flt)


def peer_matrix(snap: Snapshot, grant_list, flt: MatrixFilter = MatrixFilter()) -> Matrix:
    return _edge_view(snap, grant_list, flt, lambda e: (ref_key(e.src),))


def resource_matrix(
    snap: Snapshot, grant_list, by: str = "group", flt: MatrixFilter = MatrixFilter()
) -> Matrix:
    """Who reaches network resources; rows are rule source groups or peers."""
    if by == "peer":
        return _edge_view(snap, grant_list, flt, lambda e: (ref_key(e.src),), "resource")
    return _edge_view(
        snap,
        grant_list,
        flt,
        lambda e: (f"g:{e.src_group}",) if e.src_group else (ref_key(e.src),),
        "resource",
    )


def user_matrix(snap: Snapshot, grant_list, flt: MatrixFilter = MatrixFilter()) -> Matrix:
    """A user reaches whatever any of their peers reaches."""
    owner = {p.id: p.user_id for p in snap.peers.values() if p.user_id in snap.users}
    return _edge_view(
        snap,
        grant_list,
        flt,
        lambda e: (f"u:{owner[e.src.id]}",) if e.src.id in owner else (),
    )


__all__ = [
    "Axis",
    "Cell",
    "Matrix",
    "MatrixFilter",
    "group_matrix",
    "group_members",
    "peer_matrix",
    "ref_key",
    "resource_matrix",
    "user_matrix",
]
