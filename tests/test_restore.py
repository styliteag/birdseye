from birdseye_web.diff import access_delta
from birdseye_web.restore import plan_restore
from tests.factory import group, peer, policy, rule, snap

NOW = snap(
    peers=[peer("a", ["G"]), peer("b", ["H"])],
    groups=[group("G", peers=["a"]), group("H", peers=["b"])],
    policies=[policy("p", rule(["G"], ["H"], protocol="tcp", ports=["22"]))],
)


def test_restore_changed_policy_is_update_with_delta():
    old = policy("p", rule(["G"], ["H"]))  # used to allow everything
    plan = plan_restore(NOW, "policies", old)
    assert plan.method == "PUT" and plan.path == "policies/p" and not plan.problems
    assert plan.payload["rules"][0]["protocol"] == "all"
    d = access_delta(NOW, plan.after)
    assert {x.service for x in d.gained} == {"all"} and {x.service for x in d.lost} == {"tcp/22"}


def test_restore_deleted_policy_is_create_without_rule_ids():
    plan = plan_restore(NOW, "policies", policy("gone", rule(["H"], ["G"], rid="r9")))
    assert plan.method == "POST" and plan.path == "policies"
    assert "id" not in plan.payload["rules"][0]
    assert [(x.src.id, x.dst.id) for x in access_delta(NOW, plan.after).gained] == [("b", "a")]


def test_restore_policy_blocked_on_missing_references():
    old = policy("p", rule(["G", "Z"], ["H"]), posture=["pc9"])
    plan = plan_restore(NOW, "policies", old)
    assert plan.blocked
    assert any("Z" in p for p in plan.problems) and any("pc9" in p for p in plan.problems)


def test_restore_policy_blocked_on_missing_direct_peer():
    old = policy("p", rule(["G"], dst_resource={"id": "zz", "type": "peer"}))
    assert plan_restore(NOW, "policies", old).blocked


def test_restore_group_drops_vanished_members_with_note():
    old = {
        **group("G", "Gee", peers=["a", "b", "gone"]),
        "resources": [{"id": "r0", "type": "host"}],
    }
    plan = plan_restore(NOW, "groups", old)
    assert plan.method == "PUT" and plan.path == "groups/G" and not plan.blocked
    assert plan.payload == {"name": "Gee", "peers": ["a", "b"], "resources": []}
    assert any("gone" in n for n in plan.notes) and any("r0" in n for n in plan.notes)
    assert "b" in plan.after.groups["G"].peer_ids


def test_restore_deleted_group_is_create():
    plan = plan_restore(NOW, "groups", group("X", "Ex", peers=["a"]))
    assert plan.method == "POST" and plan.path == "groups"
    assert plan.after.groups["X"].peer_ids == frozenset({"a"})


def test_all_group_and_other_kinds_cannot_be_restored():
    assert plan_restore(NOW, "groups", group("ALL", "All")).blocked
    assert plan_restore(NOW, "users", {"id": "u"}).blocked


def test_group_without_name_is_blocked_not_an_error():
    assert plan_restore(NOW, "groups", {"id": "X", "name": ""}).blocked
