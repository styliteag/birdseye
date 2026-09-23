from birdseye_web.anomalies import find_anomalies
from tests.factory import group, peer, policy, resource, rule, snap, user


def _by(findings, check):
    return [f for f in findings if f.check == check]


def test_direct_peer_destination_is_warning():
    s = snap(
        groups=[group("A", peers=["a1"])],
        peers=[peer("a1", ["A"]), peer("b1")],
        policies=[policy("p", rule(["A"], dst_resource={"id": "b1", "type": "peer"}))],
    )
    hits = _by(find_anomalies(s), "direct-peer")
    assert len(hits) == 1 and hits[0].severity == "warning" and "b1" in hits[0].title


def test_direct_resource_destination_is_info():
    s = snap(
        groups=[group("A", peers=["a1"])],
        peers=[peer("a1", ["A"])],
        resources=[resource("net")],
        policies=[policy("p", rule(["A"], dst_resource={"id": "net", "type": "subnet"}))],
    )
    hits = _by(find_anomalies(s), "direct-resource")
    assert [h.severity for h in hits] == ["info"]


def test_peer_group_deviation_from_user_default():
    s = snap(
        groups=[group("All", peers=["d1"]), group("U"), group("X"), group("Y")],
        peers=[peer("d1", ["All", "U", "X"], user="u1")],
        users=[user("u1", "Uschi", auto_groups=["U", "Y"])],
    )
    [hit] = _by(find_anomalies(s), "device-deviation")
    assert hit.severity == "warning"
    assert "missing: Y" in hit.detail and "extra: X" in hit.detail
    assert "Uschi" in hit.title


def test_peer_matching_user_default_is_fine():
    s = snap(
        groups=[group("All"), group("U")],
        peers=[peer("d1", ["All", "U"], user="u1")],
        users=[user("u1", auto_groups=["U"])],
    )
    assert _by(find_anomalies(s), "device-deviation") == []


def test_non_api_groups_are_not_deviations():
    jwt = {**group("J"), "issued": "jwt"}
    s = snap(
        groups=[group("U"), jwt],
        peers=[peer("d1", ["U", "J"], user="u1")],
        users=[user("u1", auto_groups=["U"])],
    )
    assert _by(find_anomalies(s), "device-deviation") == []


def test_empty_group_in_policy():
    s = snap(
        groups=[group("A", peers=["a1"]), group("E")],
        peers=[peer("a1", ["A"])],
        policies=[policy("p", rule(["A"], ["E"]))],
    )
    [hit] = _by(find_anomalies(s), "empty-group")
    assert "E" in hit.title


def test_missing_group_reference_is_error():
    s = snap(
        groups=[group("A", peers=["a1"])],
        peers=[peer("a1", ["A"])],
        policies=[policy("p", rule(["A"], ["GONE"]))],
    )
    [hit] = _by(find_anomalies(s), "missing-group")
    assert hit.severity == "error"


def test_resource_without_router():
    from birdseye_web.models import build_snapshot

    s = build_snapshot(
        networks=[({"id": "n", "name": "Net"}, [resource("r1")], [])],
    )
    [hit] = _by(find_anomalies(s), "no-router")
    assert "r1" in hit.title


def test_peer_only_in_all_and_unused_group_and_disabled():
    s = snap(
        groups=[group("All", peers=["lonely", "a1"]), group("A", peers=["a1"]), group("Unused")],
        peers=[peer("lonely", ["All"]), peer("a1", ["All", "A"])],
        policies=[policy("off", rule(["A"], ["A"]), enabled=False)],
    )
    found = find_anomalies(s)
    assert [h.title for h in _by(found, "only-all")] == ["lonely is only in “All”"]
    assert "Unused" in [h.title.split(" ")[0] for h in _by(found, "unused-group")]
    assert _by(found, "disabled")


def test_findings_sorted_by_severity():
    s = snap(
        groups=[group("A", peers=["a1"]), group("Unused")],
        peers=[peer("a1", ["A"])],
        policies=[policy("p", rule(["A"], ["GONE"]))],
    )
    sev = [f.severity for f in find_anomalies(s)]
    order = {"error": 0, "warning": 1, "info": 2}
    assert sev == sorted(sev, key=order.get)


def test_router_peer_groups_are_not_deviations():
    from birdseye_web.models import build_snapshot

    s = build_snapshot(
        groups=[group("U"), group("R")],
        peers=[peer("d1", ["U", "R"], user="u1")],
        users=[user("u1", auto_groups=["U"])],
        networks=[({"id": "n", "name": "N"}, [], [{"peer_groups": ["R"], "enabled": True}])],
    )
    assert _by(find_anomalies(s), "device-deviation") == []


def test_setup_key_and_router_groups_count_as_used():
    from birdseye_web.models import build_snapshot

    s = build_snapshot(
        groups=[group("K"), group("R"), group("Lost")],
        setup_keys=[
            {"id": "k", "auto_groups": ["K"]},
            {"id": "old", "revoked": True, "auto_groups": ["Lost"]},
        ],
        networks=[({"id": "n", "name": "N"}, [], [{"peer_groups": ["R"], "enabled": True}])],
    )
    unused = [f.title.split(" ")[0] for f in _by(find_anomalies(s), "unused-group")]
    assert unused == ["Lost"]


def test_resource_group_is_not_unused_when_resource_is_targeted_directly():
    s = snap(
        groups=[group("A", peers=["a1"]), group("RG", resources=["r1"])],
        peers=[peer("a1", ["A"])],
        resources=[resource("r1", ["RG"])],
        policies=[policy("p", rule(["A"], dst_resource={"id": "r1", "type": "subnet"}))],
    )
    found = find_anomalies(s)
    assert "RG" not in [f.title.split(" ")[0] for f in _by(found, "unused-group")]
    assert _by(found, "unreachable-resource") == []


def test_resource_nobody_reaches_is_flagged_and_its_group_stays_unused():
    s = snap(
        groups=[group("RG", resources=["r1"])],
        resources=[resource("r1", ["RG"])],
    )
    found = find_anomalies(s)
    assert [f.title.split(" ")[0] for f in _by(found, "unreachable-resource")] == ["r1"]
    assert [f.title.split(" ")[0] for f in _by(found, "unused-group")] == ["RG"]


def test_resource_reached_through_group_counts():
    s = snap(
        groups=[group("A", peers=["a1"]), group("RG", resources=["r1"])],
        peers=[peer("a1", ["A"])],
        resources=[resource("r1", ["RG"])],
        policies=[policy("p", rule(["A"], ["RG"]))],
    )
    assert _by(find_anomalies(s), "unreachable-resource") == []


def test_disabled_policy_does_not_reach_resource():
    s = snap(
        groups=[group("A", peers=["a1"])],
        peers=[peer("a1", ["A"])],
        resources=[resource("r1")],
        policies=[
            policy("p", rule(["A"], dst_resource={"id": "r1", "type": "subnet"}), enabled=False)
        ],
    )
    assert len(_by(find_anomalies(s), "unreachable-resource")) == 1


def test_ignore_pattern_hides_group_findings():
    import re

    s = snap(groups=[group("Z002 Short forms: -RG=ResourceGroup"), group("Real-Unused")])
    titles = [f.title for f in find_anomalies(s, re.compile(r"^Z\d{3} "))]
    assert titles == ["Real-Unused (0 members)"]
