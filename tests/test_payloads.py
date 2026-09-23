import pytest

from birdseye_web.payloads import (
    PayloadError,
    build_rule,
    group_payload,
    parse_ports,
    policy_for_put,
    rule_for_put,
)


def test_rule_for_put_flattens_groups():
    got = rule_for_put(
        {
            "id": "r1",
            "name": "n",
            "action": "accept",
            "protocol": "netbird-ssh",
            "bidirectional": False,
            "enabled": True,
            "sources": [{"id": "g1", "name": "G1"}],
            "destinations": [{"id": "g2", "name": "G2"}],
            "ports": None,
        }
    )
    assert got["sources"] == ["g1"] and got["destinations"] == ["g2"]
    assert got["protocol"] == "netbird-ssh"
    assert "ports" not in got


def test_rule_for_put_resource_excludes_groups():
    got = rule_for_put(
        {
            "name": "n",
            "action": "accept",
            "protocol": "all",
            "sources": [{"id": "g1"}],
            "destinations": None,
            "destinationResource": {"id": "res", "type": "subnet", "extra": 1},
        }
    )
    assert got["destinationResource"] == {"id": "res", "type": "subnet"}
    assert "destinations" not in got


def test_policy_for_put_keeps_posture_and_does_not_mutate():
    raw = {
        "id": "p",
        "name": "P",
        "enabled": True,
        "source_posture_checks": ["pc"],
        "rules": [{"name": "r", "action": "accept", "protocol": "all", "sources": [{"id": "a"}]}],
    }
    snapshot = repr(raw)
    got = policy_for_put(raw)
    assert got["source_posture_checks"] == ["pc"]
    assert got["rules"][0]["sources"] == ["a"]
    assert repr(raw) == snapshot


@pytest.mark.parametrize(
    "text,ports,ranges",
    [
        ("", [], []),
        ("22", ["22"], []),
        ("22, 443 ,8000-8099", ["22", "443"], [{"start": 8000, "end": 8099}]),
        ("80 443", ["80", "443"], []),
    ],
)
def test_parse_ports(text, ports, ranges):
    assert parse_ports(text) == (ports, ranges)


@pytest.mark.parametrize("text", ["abc", "0", "70000", "90-80", "1-2-3"])
def test_parse_ports_rejects(text):
    with pytest.raises(PayloadError):
        parse_ports(text)


def test_build_rule_tcp_with_ports():
    r = build_rule(
        name="ssh",
        sources=["a"],
        destinations=["b"],
        protocol="tcp",
        ports="22,8000-8001",
    )
    assert r["ports"] == ["22"]
    assert r["port_ranges"] == [{"start": 8000, "end": 8001}]
    assert r["action"] == "accept" and r["enabled"] is True


def test_build_rule_destination_resource():
    r = build_rule(
        name="net",
        sources=["a"],
        destination_resource={"id": "x", "type": "subnet"},
        protocol="all",
    )
    assert r["destinationResource"] == {"id": "x", "type": "subnet"}
    assert "destinations" not in r


@pytest.mark.parametrize(
    "kwargs,msg",
    [
        (dict(sources=[], destinations=["b"]), "source"),
        (dict(sources=["a"], destinations=[]), "destination"),
        (dict(sources=["a"], destinations=["b"], protocol="gre"), "protocol"),
        (dict(sources=["a"], destinations=["b"], protocol="icmp", ports="22"), "ports"),
        (dict(sources=["a"], destinations=["b"], protocol="all", ports="22"), "ports"),
    ],
)
def test_build_rule_validation(kwargs, msg):
    with pytest.raises(PayloadError, match=msg):
        build_rule(name="x", **{"protocol": "tcp", **kwargs})


def test_build_rule_requires_name():
    with pytest.raises(PayloadError, match="name"):
        build_rule(name=" ", sources=["a"], destinations=["b"], protocol="all")


def test_group_payload_sorted_and_resources_preserved():
    got = group_payload("G", ["b", "a", "a"], [{"id": "r", "type": "host", "name": "x"}])
    assert got == {"name": "G", "peers": ["a", "b"], "resources": [{"id": "r", "type": "host"}]}


def test_group_payload_requires_name():
    with pytest.raises(PayloadError):
        group_payload("  ", [], [])
