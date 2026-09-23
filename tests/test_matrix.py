import pytest

from birdseye_web.access import grants
from birdseye_web.matrix import (
    MatrixFilter,
    group_matrix,
    peer_matrix,
    resource_matrix,
    user_matrix,
)
from tests.factory import group, peer, policy, resource, rule, snap, user


@pytest.fixture
def s():
    return snap(
        groups=[
            group("All", peers=["a1", "b1", "u1p"]),
            group("A", peers=["a1"]),
            group("B", peers=["b1"]),
            group("RG", resources=["h1"]),
        ],
        peers=[peer("a1", ["A"], user="u1"), peer("b1", ["B"]), peer("u1p", user="u1")],
        users=[user("u1", "Uschi"), user("u2", "Nobody")],
        resources=[resource("h1", ["RG"], rtype="host")],
        policies=[
            policy("ssh", rule(["A"], ["B"], protocol="tcp", ports=["22"])),
            policy(
                "web", rule(["A"], ["B"], protocol="tcp", ports=["443"], rid="r2"), posture=["pc"]
            ),
            policy("net", rule(["B"], ["RG"], rid="r3")),
            policy("ping", rule(["All"], ["All"], protocol="icmp", rid="r4")),
        ],
    )


def _keys(axis):
    return [a.key for a in axis]


def test_group_matrix_cells_aggregate_services(s):
    m = group_matrix(s, grants(s))
    cell = m.cell("g:A", "g:B")
    assert cell.service_labels == ("tcp/22", "tcp/443")
    assert {g.policy_id for g in cell.grants} == {"ssh", "web"}


def test_group_matrix_conditional_only_when_every_grant_is(s):
    m = group_matrix(s, grants(s))
    assert not m.cell("g:A", "g:B").conditional  # ssh has no posture check
    only_web = group_matrix(s, grants(s), MatrixFilter(port=443))
    assert only_web.cell("g:A", "g:B").conditional


def test_group_matrix_hide_all(s):
    m = group_matrix(s, grants(s), MatrixFilter(hide_all=True))
    assert "g:All" not in _keys(m.rows) + _keys(m.cols)


def test_group_matrix_protocol_filter(s):
    m = group_matrix(s, grants(s), MatrixFilter(protocol="icmp"))
    # protocol "all" includes icmp, so B->RG stays
    assert _keys(m.rows) == ["g:All", "g:B"]
    assert m.cell("g:A", "g:B") is None


def test_row_and_col_query_filter(s):
    m = group_matrix(s, grants(s), MatrixFilter(row_query="b", col_query="rg"))
    assert _keys(m.rows) == ["g:B"] and _keys(m.cols) == ["g:RG"]


def test_peer_matrix_expands(s):
    m = peer_matrix(s, grants(s), MatrixFilter(hide_all=True))
    assert m.cell("p:a1", "p:b1").service_labels == ("tcp/22", "tcp/443")
    assert m.cell("p:b1", "r:h1").service_labels == ("all",)
    assert m.cell("p:b1", "p:a1") is None


def test_peer_matrix_row_limit_reports_truncation(s):
    m = peer_matrix(s, grants(s), MatrixFilter(max_rows=1))
    assert len(m.rows) == 1 and m.truncated


def test_resource_matrix_by_group(s):
    m = resource_matrix(s, grants(s), by="group")
    assert _keys(m.cols) == ["r:h1"]
    assert _keys(m.rows) == ["g:B"]


def test_resource_matrix_by_peer(s):
    m = resource_matrix(s, grants(s), by="peer")
    assert _keys(m.rows) == ["p:b1"]


def test_user_matrix_unions_user_peers(s):
    m = user_matrix(s, grants(s))
    assert "u:u2" not in _keys(m.rows)  # user without peers reaches nothing
    cell = m.cell("u:u1", "p:b1")
    assert "tcp/22" in cell.service_labels and "icmp" in cell.service_labels
    assert {e_src for e_src in cell.via} == {"p:a1", "p:u1p"}


def test_labels_are_names(s):
    m = user_matrix(s, grants(s))
    assert m.rows[0].label == "Uschi"


def test_rows_sorted_by_label(s):
    m = group_matrix(s, grants(s))
    labels = [a.label for a in m.rows]
    assert labels == sorted(labels, key=str.lower)
