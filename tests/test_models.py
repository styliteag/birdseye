from birdseye_web.models import PortRange, Ref, Service, parse_rule, parse_service
from tests.factory import group, peer, snap


def test_parse_service_all_ignores_ports():
    assert parse_service({"protocol": "all", "ports": ["22"]}) == Service("all")


def test_parse_service_merges_ports_and_ranges_sorted():
    svc = parse_service(
        {"protocol": "tcp", "ports": ["443", "22"], "port_ranges": [{"start": 8000, "end": 8099}]}
    )
    assert svc.ports == (PortRange(22, 22), PortRange(443, 443), PortRange(8000, 8099))
    assert svc.label() == "tcp/22,443,8000-8099"


def test_parse_service_icmp_label():
    assert parse_service({"protocol": "icmp"}).label() == "icmp"


def test_parse_rule_flattens_groups_and_resources():
    r = parse_rule(
        {
            "id": "r",
            "sources": [{"id": "g1", "name": "G1"}],
            "destinations": None,
            "destinationResource": {"id": "res1", "type": "subnet"},
            "protocol": "all",
        }
    )
    assert r.source_group_ids == ("g1",)
    assert r.destination_group_ids == ()
    assert r.destination_ref == Ref("resource", "res1")


def test_parse_rule_peer_destination_ref():
    r = parse_rule({"id": "r", "destinationResource": {"id": "p1", "type": "peer"}})
    assert r.destination_ref == Ref("peer", "p1")


def test_null_lists_become_empty():
    s = snap(groups=[group("g")], peers=[peer("p")])
    assert s.groups["g"].peer_ids == frozenset()
    assert s.peers["p"].group_ids == frozenset()
