from birdseye_web.models import build_snapshot
from birdseye_web.resources_view import ResourceQuery, address_info, resource_rows
from tests.factory import group, peer, policy, rule


def _snap():
    net = {"id": "n1", "name": "office"}
    res = [
        {
            "id": "host",
            "name": "nas",
            "address": "10.0.0.5/32",
            "type": "host",
            "groups": [{"id": "RG"}],
            "enabled": True,
        },
        {
            "id": "lan",
            "name": "lan",
            "address": "10.0.0.0/24",
            "type": "subnet",
            "groups": [{"id": "LG"}],
            "enabled": True,
        },
        {
            "id": "dom",
            "name": "wiki",
            "address": "*.corp.local",
            "type": "domain",
            "groups": [{"id": "LG"}],
            "enabled": False,
        },
    ]
    return build_snapshot(
        peers=[peer("a", ["U"]), peer("b", ["U"]), peer("gw", ["GW"])],
        groups=[
            group("U", "Users", peers=["a", "b"]),
            group("RG", resources=["host"]),
            group("LG", resources=["lan", "dom"]),
            group("GW", peers=["gw"]),
        ],
        policies=[
            policy("p-rg", rule(["U"], ["RG"])),
            policy(
                "p-direct", rule(["U"], dst_resource={"id": "host", "type": "host"}), enabled=False
            ),
        ],
        networks=[(net, res, [{"id": "rt", "peer_groups": ["GW"], "enabled": True}])],
    )


def test_address_info():
    assert address_info("10.0.0.5/32") == ("host", 1)
    assert address_info("10.0.0.5") == ("host", 1)
    assert address_info("10.0.0.0/24") == ("subnet", 256)
    assert address_info("*.corp.local") == ("wildcard", None)
    assert address_info("grafana.corp.local") == ("domain", None)


def test_rows_policies_reach_and_routing():
    rows = {r.resource.id: r for r in resource_rows(_snap(), ResourceQuery())}
    nas = rows["host"]
    assert [p.id for p in nas.policies] == ["p-direct", "p-rg"]  # disabled ones listed too
    assert nas.reached_by == 2 and nas.routed and nas.network.name == "office"
    assert rows["lan"].reached_by == 0 and rows["lan"].policies == ()
    assert rows["lan"].broad and not nas.broad


def test_filters():
    s = _snap()
    ids = lambda q: [r.resource.id for r in resource_rows(s, q)]  # noqa: E731
    assert ids(ResourceQuery()) == ["lan", "host", "dom"]  # by name
    assert ids(ResourceQuery(q="10.0.0.5")) == ["host"]
    assert ids(ResourceQuery(unreached=True)) == ["lan", "dom"]
    assert ids(ResourceQuery(broad=True)) == ["lan"]
    assert ids(ResourceQuery(network="n2")) == []
    assert ResourceQuery.from_params({"unreached": "1", "q": " x "}) == ResourceQuery(
        q="x", unreached=True
    )
