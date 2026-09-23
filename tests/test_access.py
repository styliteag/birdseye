import pytest

from birdseye_web.access import edges, grants, reach
from birdseye_web.models import Ref
from tests.factory import group, peer, policy, resource, rule, snap

P = lambda i: Ref("peer", i)  # noqa: E731
R = lambda i: Ref("resource", i)  # noqa: E731


def _pairs(s, **kw):
    return {(e.src, e.dst) for e in edges(s, grants(s, **kw))}


@pytest.fixture
def two_groups():
    return dict(
        groups=[group("A", peers=["a1", "a2"]), group("B", peers=["b1"])],
        peers=[peer("a1", ["A"]), peer("a2", ["A"]), peer("b1", ["B"])],
    )


def test_simple_accept_expands_to_peers(two_groups):
    s = snap(**two_groups, policies=[policy("p", rule(["A"], ["B"]))])
    assert _pairs(s) == {(P("a1"), P("b1")), (P("a2"), P("b1"))}


def test_bidirectional_adds_reverse(two_groups):
    s = snap(**two_groups, policies=[policy("p", rule(["A"], ["B"], bidirectional=True))])
    pairs = _pairs(s)
    assert (P("b1"), P("a1")) in pairs
    assert (P("a1"), P("b1")) in pairs
    rev = [g for g in grants(s) if g.reverse]
    assert len(rev) == 1 and rev[0].src_groups == ("B",)


@pytest.mark.parametrize(
    "pol",
    [
        policy("p", rule(["A"], ["B"]), enabled=False),
        policy("p", rule(["A"], ["B"], enabled=False)),
    ],
    ids=["policy-disabled", "rule-disabled"],
)
def test_disabled_excluded_by_default(two_groups, pol):
    s = snap(**two_groups, policies=[pol])
    assert _pairs(s) == set()
    assert _pairs(s, include_disabled=True) == {(P("a1"), P("b1")), (P("a2"), P("b1"))}


def test_disabled_grant_flagged(two_groups):
    s = snap(**two_groups, policies=[policy("p", rule(["A"], ["B"]), enabled=False)])
    assert all(not g.enabled for g in grants(s, include_disabled=True))


def test_self_edges_skipped():
    s = snap(
        groups=[group("A", peers=["a1", "a2"])],
        peers=[peer("a1"), peer("a2")],
        policies=[policy("p", rule(["A"], ["A"]))],
    )
    assert _pairs(s) == {(P("a1"), P("a2")), (P("a2"), P("a1"))}


def test_destination_resource_subnet(two_groups):
    s = snap(
        **two_groups,
        resources=[resource("net-a")],
        policies=[policy("p", rule(["B"], dst_resource={"id": "net-a", "type": "subnet"}))],
    )
    assert _pairs(s) == {(P("b1"), R("net-a"))}


def test_destination_resource_peer(two_groups):
    s = snap(
        **two_groups, policies=[policy("p", rule(["A"], dst_resource={"id": "b1", "type": "peer"}))]
    )
    assert _pairs(s) == {(P("a1"), P("b1")), (P("a2"), P("b1"))}


def test_destination_group_containing_resources():
    s = snap(
        groups=[group("A", peers=["a1"]), group("RG", resources=["h1"])],
        peers=[peer("a1")],
        resources=[resource("h1", ["RG"], rtype="host")],
        policies=[policy("p", rule(["A"], ["RG"]))],
    )
    assert _pairs(s) == {(P("a1"), R("h1"))}


def test_bidirectional_never_makes_resource_a_source():
    s = snap(
        groups=[group("A", peers=["a1"])],
        peers=[peer("a1")],
        resources=[resource("h1")],
        policies=[
            policy("p", rule(["A"], bidirectional=True, dst_resource={"id": "h1", "type": "host"}))
        ],
    )
    assert _pairs(s) == {(P("a1"), R("h1"))}


def test_posture_checks_mark_conditional(two_groups):
    s = snap(**two_groups, policies=[policy("p", rule(["A"], ["B"]), posture=["pc1"])])
    assert all(e.grant.conditional == ("pc1",) for e in edges(s, grants(s)))


def test_ports_carried_on_edge(two_groups):
    s = snap(**two_groups, policies=[policy("p", rule(["A"], ["B"], protocol="tcp", ports=["22"]))])
    assert {e.grant.service.label() for e in edges(s, grants(s))} == {"tcp/22"}


def test_netbird_ssh_protocol_kept(two_groups):
    s = snap(**two_groups, policies=[policy("p", rule(["A"], ["B"], protocol="netbird-ssh"))])
    assert {g.service.protocol for g in grants(s)} == {"netbird-ssh"}


def test_membership_from_peer_side_counts_too():
    # group object lacks the peer, but the peer lists the group
    s = snap(
        groups=[group("A"), group("B", peers=["b1"])],
        peers=[peer("a1", ["A"]), peer("b1", ["B"])],
        policies=[policy("p", rule(["A"], ["B"]))],
    )
    assert _pairs(s) == {(P("a1"), P("b1"))}


def test_edges_source_filter(two_groups):
    s = snap(**two_groups, policies=[policy("p", rule(["A"], ["B"]))])
    got = {(e.src, e.dst) for e in edges(s, grants(s), sources={P("a1")})}
    assert got == {(P("a1"), P("b1"))}


def test_edge_carries_via_groups(two_groups):
    s = snap(**two_groups, policies=[policy("p", rule(["A"], ["B"]))])
    e = next(iter(edges(s, grants(s), sources={P("a1")})))
    assert (e.src_group, e.dst_group) == ("A", "B")


# --- reach() query --------------------------------------------------------------


@pytest.fixture
def ssh_only(two_groups):
    return snap(
        **two_groups,
        policies=[
            policy("ssh", rule(["A"], ["B"], protocol="tcp", ports=["22"])),
            policy(
                "web",
                rule(
                    ["A"],
                    ["B"],
                    protocol="tcp",
                    port_ranges=[{"start": 8000, "end": 8099}],
                    rid="r2",
                ),
            ),
        ],
    )


@pytest.mark.parametrize(
    "protocol,port,expected",
    [
        ("tcp", 22, {"ssh"}),
        ("tcp", 8080, {"web"}),
        ("tcp", 443, set()),
        ("udp", 22, set()),
        (None, None, {"ssh", "web"}),
    ],
)
def test_reach_matches_protocol_and_port(ssh_only, protocol, port, expected):
    hits = reach(ssh_only, P("a1"), P("b1"), protocol=protocol, port=port)
    assert {h.grant.policy_id for h in hits} == expected


def test_reach_all_protocol_matches_any_port(two_groups):
    s = snap(**two_groups, policies=[policy("p", rule(["A"], ["B"]))])
    assert reach(s, P("a1"), P("b1"), protocol="udp", port=53)


def test_reach_wrong_direction_empty(ssh_only):
    assert reach(ssh_only, P("b1"), P("a1")) == ()


@pytest.mark.parametrize(
    "service,protocol,port,expected",
    [
        (("all", ()), "udp", 53, True),
        (("tcp", ()), None, 443, True),
        (("tcp", ((22, 22),)), None, 443, False),
        (("tcp", ((22, 22),)), "udp", None, False),
        (("icmp", ()), None, 22, False),
        (("icmp", ()), "icmp", None, True),
        (("netbird-ssh", ()), None, 22, True),
        (("netbird-ssh", ()), None, 2222, False),
    ],
)
def test_service_matches(service, protocol, port, expected):
    from birdseye_web.access import service_matches
    from birdseye_web.models import PortRange, Service

    proto, ranges = service
    svc = Service(proto, tuple(PortRange(a, b) for a, b in ranges))
    assert service_matches(svc, protocol, port) is expected
