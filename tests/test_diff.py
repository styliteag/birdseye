from birdseye_web.diff import access_delta, with_policy, without_policy
from birdseye_web.models import Ref
from tests.factory import group, peer, policy, rule, snap


def _base():
    return snap(
        groups=[group("A", peers=["a1"]), group("B", peers=["b1"]), group("C", peers=["c1"])],
        peers=[peer("a1"), peer("b1"), peer("c1")],
        policies=[policy("p", rule(["A"], ["B"], protocol="tcp", ports=["22"]))],
    )


def test_changing_destination_shows_gain_and_loss():
    before = _base()
    after = with_policy(before, policy("p", rule(["A"], ["C"], protocol="tcp", ports=["22"])))
    d = access_delta(before, after)
    assert [(x.src, x.dst, x.service) for x in d.gained] == [
        (Ref("peer", "a1"), Ref("peer", "c1"), "tcp/22")
    ]
    assert [(x.src, x.dst) for x in d.lost] == [(Ref("peer", "a1"), Ref("peer", "b1"))]


def test_new_policy_only_gains():
    before = _base()
    after = with_policy(before, policy("new", rule(["B"], ["C"])))
    d = access_delta(before, after)
    assert len(d.gained) == 1 and d.lost == ()


def test_delete_policy_only_loses():
    before = _base()
    d = access_delta(before, without_policy(before, "p"))
    assert d.gained == () and len(d.lost) == 1


def test_port_change_is_service_delta():
    before = _base()
    after = with_policy(before, policy("p", rule(["A"], ["B"], protocol="tcp", ports=["443"])))
    d = access_delta(before, after)
    assert [x.service for x in d.gained] == ["tcp/443"]
    assert [x.service for x in d.lost] == ["tcp/22"]


def test_redundant_policy_changes_nothing():
    before = _base()
    after = with_policy(before, policy("dup", rule(["A"], ["B"], protocol="tcp", ports=["22"])))
    assert access_delta(before, after).empty


def test_with_policy_does_not_mutate():
    before = _base()
    with_policy(before, policy("x", rule(["A"], ["C"])))
    assert set(before.policies) == {"p"}


def test_group_membership_change_both_sides():
    from birdseye_web.diff import with_group_members

    before = snap(
        groups=[group("A", peers=["a1"]), group("B", peers=["b1"])],
        peers=[peer("a1", ["A"]), peer("a2"), peer("b1", ["B"])],
        policies=[policy("p", rule(["A"], ["B"]))],
    )
    after = with_group_members(before, "A", ["a2"])
    d = access_delta(before, after)
    assert [(x.src.id, x.dst.id) for x in d.gained] == [("a2", "b1")]
    assert [(x.src.id, x.dst.id) for x in d.lost] == [("a1", "b1")]
    assert "A" not in after.peers["a1"].group_ids
    assert "A" in before.peers["a1"].group_ids
