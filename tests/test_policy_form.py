from starlette.datastructures import FormData

from birdseye_web.payloads import PayloadError
from birdseye_web.routes_policies import carry_over, policy_from_form


def _form(**extra):
    base = [
        ("name", "P"),
        ("enabled", "on"),
        ("rule_idx", "0"),
        ("rules-0-id", "r1"),
        ("rules-0-sources", "g1"),
        ("rules-0-destinations", "g2"),
        ("rules-0-protocol", "tcp"),
        ("rules-0-ports", "22"),
        ("rules-0-enabled", "on"),
    ]
    return FormData(base + list(extra.items()))


def test_policy_from_form_builds_rule():
    body = policy_from_form(_form())
    r = body["rules"][0]
    assert body["name"] == "P" and body["enabled"] is True
    assert r["id"] == "r1" and r["ports"] == ["22"] and r["name"] == "P"


def test_policy_from_form_destination_resource():
    f = FormData(
        [
            ("name", "P"),
            ("rule_idx", "0"),
            ("rules-0-sources", "g1"),
            ("rules-0-dst_resource", "subnet:res1"),
        ]
    )
    r = policy_from_form(f)["rules"][0]
    assert r["destinationResource"] == {"id": "res1", "type": "subnet"}


def test_policy_from_form_requires_rules():
    import pytest

    with pytest.raises(PayloadError):
        policy_from_form(FormData([("name", "P")]))


def test_carry_over_keeps_authorized_groups():
    before = {
        "rules": [
            {
                "id": "r1",
                "name": "x",
                "action": "accept",
                "protocol": "netbird-ssh",
                "sources": [{"id": "g1"}],
                "destinations": [{"id": "g2"}],
                "authorized_groups": {"g1": ["root"]},
            }
        ]
    }
    body = policy_from_form(_form())
    out = carry_over(before, body)
    assert out["rules"][0]["authorized_groups"] == {"g1": ["root"]}
    assert "authorized_groups" not in body["rules"][0]
