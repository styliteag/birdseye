import pytest

from birdseye_web.payloads import PayloadError
from birdseye_web.quickedit import Revoke, allow_policy, revoke

GROUPS = {"A": "Admins", "B": "Servers"}


def test_allow_group_to_group():
    body = allow_policy("g:A", "g:B", names=GROUPS, protocol="tcp", ports="22")
    assert body["name"] == "Admins -> Servers"
    r = body["rules"][0]
    assert r["sources"] == ["A"] and r["destinations"] == ["B"]
    assert r["ports"] == ["22"] and body["enabled"] is True


def test_allow_group_to_resource_uses_resource_type():
    body = allow_policy(
        "g:A", "r:R1", names={**GROUPS, "R1": "Net"}, resource_types={"R1": "subnet"}
    )
    assert body["rules"][0]["destinationResource"] == {"id": "R1", "type": "subnet"}
    assert "destinations" not in body["rules"][0]


def test_allow_group_to_peer():
    body = allow_policy("g:A", "p:P1", names={**GROUPS, "P1": "host"})
    assert body["rules"][0]["destinationResource"] == {"id": "P1", "type": "peer"}


def test_allow_keeps_posture_and_custom_name():
    body = allow_policy("g:A", "g:B", names=GROUPS, name="ssh", posture=["pc"])
    assert body["name"] == "ssh" and body["source_posture_checks"] == ["pc"]


@pytest.mark.parametrize("src,dst", [("p:X", "g:B"), ("g:A", "u:U"), ("g:", "g:B")])
def test_allow_rejects_non_group_sources_and_bad_targets(src, dst):
    with pytest.raises(PayloadError):
        allow_policy(src, dst, names=GROUPS)


def _policy(*rules, posture=("pc",)):
    return {
        "id": "p1",
        "name": "P",
        "enabled": True,
        "source_posture_checks": list(posture),
        "rules": list(rules),
    }


def _rule(rid, src, dst=None, dst_res=None, **extra):
    return {
        "id": rid,
        "name": rid,
        "enabled": True,
        "action": "accept",
        "protocol": "all",
        "bidirectional": False,
        "sources": [{"id": g, "name": g} for g in src],
        "destinations": [{"id": g, "name": g} for g in dst or []] or None,
        "destinationResource": dst_res,
        **extra,
    }


def test_disable_keeps_everything_else():
    pol = _policy(_rule("r1", ["A"], ["B"], authorized_groups={"A": ["root"]}))
    out = revoke(pol, "r1", "disable")
    assert out.method == "PUT" and out.body["enabled"] is False
    assert out.body["source_posture_checks"] == ["pc"]
    assert out.body["rules"][0]["authorized_groups"] == {"A": ["root"]}


def test_remove_source_from_multi_group_rule():
    out = revoke(_policy(_rule("r1", ["A", "C"], ["B"])), "r1", "remove_source", "A")
    assert out.method == "PUT"
    assert out.body["rules"][0]["sources"] == ["C"]


def test_remove_last_source_drops_rule_and_keeps_other_rules():
    pol = _policy(_rule("r1", ["A"], ["B"]), _rule("r2", ["C"], ["B"]))
    out = revoke(pol, "r1", "remove_source", "A")
    assert [r["id"] for r in out.body["rules"]] == ["r2"]


def test_remove_last_pair_of_last_rule_deletes_policy():
    out = revoke(_policy(_rule("r1", ["A"], ["B"])), "r1", "remove_destination", "B")
    assert out == Revoke("DELETE", None)


def test_remove_destination_resource_drops_rule():
    pol = _policy(
        _rule("r1", ["A"], dst_res={"id": "R1", "type": "subnet"}), _rule("r2", ["A"], ["B"])
    )
    out = revoke(pol, "r1", "remove_destination", "R1")
    assert [r["id"] for r in out.body["rules"]] == ["r2"]


def test_delete():
    assert revoke(_policy(_rule("r1", ["A"], ["B"])), "r1", "delete") == Revoke("DELETE", None)


@pytest.mark.parametrize(
    "rule_id,mode,group",
    [("nope", "disable", None), ("r1", "explode", None), ("r1", "remove_source", "Z")],
)
def test_revoke_rejects_unknown(rule_id, mode, group):
    with pytest.raises(PayloadError):
        revoke(_policy(_rule("r1", ["A"], ["B"])), rule_id, mode, group)


def test_revoke_does_not_mutate_input():
    pol = _policy(_rule("r1", ["A", "C"], ["B"]))
    before = repr(pol)
    revoke(pol, "r1", "remove_source", "A")
    assert repr(pol) == before


def _grant(src, dst, reverse=False, dst_ref=None):
    from birdseye_web.access import Grant
    from birdseye_web.models import Service

    return Grant("p1", "r1", tuple(src), None, tuple(dst), dst_ref, Service("all"), reverse=reverse)


def test_cell_actions_forward():
    from birdseye_web.quickedit import cell_actions

    acts = cell_actions(_grant(["A"], ["B"]), "g:A", "g:B", GROUPS)
    assert [(a.mode, a.target) for a in acts] == [
        ("remove_source", "A"),
        ("remove_destination", "B"),
        ("disable", ""),
        ("delete", ""),
    ]
    assert "Admins" in acts[0].label


def test_cell_actions_reverse_swaps_sides():
    from birdseye_web.quickedit import cell_actions

    # rule A -> B bidirectional; the cell B -> A comes from the reverse grant
    g = _grant(["B"], ["A"], reverse=True)
    acts = cell_actions(g, "g:B", "g:A", GROUPS)
    assert [(a.mode, a.target) for a in acts[:2]] == [
        ("remove_destination", "B"),
        ("remove_source", "A"),
    ]


def test_cell_actions_resource_reached_via_group_has_no_destination_action():
    from birdseye_web.quickedit import cell_actions

    acts = cell_actions(_grant(["A"], ["RG"]), "g:A", "r:R1", GROUPS)
    assert [a.mode for a in acts] == ["remove_source", "disable", "delete"]


def test_cell_actions_direct_resource():
    from birdseye_web.models import Ref
    from birdseye_web.quickedit import cell_actions

    acts = cell_actions(_grant(["A"], [], dst_ref=Ref("resource", "R1")), "g:A", "r:R1", GROUPS)
    assert ("remove_destination", "R1") in [(a.mode, a.target) for a in acts]
