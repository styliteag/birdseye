import pytest

from birdseye_web.diff import access_delta, with_resource, without_network, without_resource
from birdseye_web.models import build_snapshot
from birdseye_web.payloads import (
    PayloadError,
    network_payload,
    resource_payload,
    router_payload,
)
from tests.factory import group, peer, policy, rule


def _snap():
    net = {"id": "n1", "name": "office", "description": "HQ"}
    res = [
        {
            "id": "r1",
            "name": "nas",
            "address": "10.0.0.5/32",
            "type": "host",
            "groups": [{"id": "RG", "name": "RG"}],
            "enabled": True,
            "description": "files",
        }
    ]
    routers = [
        {"id": "rt1", "peer": "gw", "metric": 100, "masquerade": True, "enabled": True},
        {"id": "rt2", "peer_groups": ["GW"], "metric": 9999, "masquerade": False, "enabled": False},
    ]
    return build_snapshot(
        peers=[peer("u", ["U"]), peer("gw", ["GW"])],
        groups=[group("U", peers=["u"]), group("RG", resources=["r1"]), group("GW", peers=["gw"])],
        policies=[policy("p", rule(["U"], ["RG"]))],
        networks=[(net, res, routers)],
    )


def test_network_keeps_routers_and_description():
    s = _snap()
    n = s.networks["n1"]
    assert n.description == "HQ" and [r.id for r in n.routers] == ["rt1", "rt2"]
    rt1, rt2 = n.routers
    assert rt1.peer == "gw" and rt1.metric == 100 and rt1.masquerade and rt1.enabled
    assert rt2.peer_groups == ("GW",) and not rt2.enabled
    # only enabled routers count as routing
    assert n.router_peer_ids == frozenset({"gw"}) and n.router_group_ids == frozenset()
    assert s.resources["r1"].description == "files"


def test_with_resource_regroup_changes_access():
    s = _snap()
    r = s.resources["r1"]
    after = with_resource(s, r.__class__(**{**r.__dict__, "group_ids": frozenset()}))
    # RG still lists r1 itself (group side); with_resource drops it there too
    d = access_delta(s, after)
    assert [(x.src.id, x.dst.id) for x in d.lost] == [("u", "r1")]


def test_without_resource_and_network():
    s = _snap()
    assert [(x.src.id, x.dst.id) for x in access_delta(s, without_resource(s, "r1")).lost] == [
        ("u", "r1")
    ]
    after = without_network(s, "n1")
    assert "n1" not in after.networks and "r1" not in after.resources


def test_network_payload():
    assert network_payload(" net ", " d ") == {"name": "net", "description": "d"}
    with pytest.raises(PayloadError):
        network_payload("", "")


def test_resource_payload():
    body = resource_payload(
        name=" nas ", address=" 10.0.0.5 ", groups=["RG", "RG"], enabled=True, description=""
    )
    assert body == {
        "name": "nas",
        "address": "10.0.0.5",
        "groups": ["RG"],
        "enabled": True,
        "description": "",
    }


@pytest.mark.parametrize(
    "kwargs", [{"name": ""}, {"address": ""}, {"groups": []}, {"address": "a b"}]
)
def test_resource_payload_rejects(kwargs):
    base = dict(name="x", address="10.0.0.1", groups=["G"], enabled=True, description="")
    with pytest.raises(PayloadError):
        resource_payload(**{**base, **kwargs})


def test_router_payload_peer_or_groups():
    assert router_payload(peer="gw", peer_groups=(), metric=100, masquerade=True, enabled=True) == {
        "peer": "gw",
        "metric": 100,
        "masquerade": True,
        "enabled": True,
    }
    body = router_payload(peer="", peer_groups=["GW"], metric=9999, masquerade=False, enabled=True)
    assert body["peer_groups"] == ["GW"] and "peer" not in body


@pytest.mark.parametrize(
    "kwargs",
    [
        {"peer": "", "peer_groups": []},
        {"peer": "gw", "peer_groups": ["GW"]},
        {"metric": 0},
        {"metric": 10000},
    ],
)
def test_router_payload_rejects(kwargs):
    base = dict(peer="gw", peer_groups=(), metric=100, masquerade=True, enabled=True)
    with pytest.raises(PayloadError):
        router_payload(**{**base, **kwargs})
