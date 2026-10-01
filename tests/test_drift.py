from birdseye_web.drift import alignment, managed_groups, peer_drift, user_drift
from tests.factory import group, peer, snap, user


def _snap(**kw):
    return snap(
        groups=[group("All", peers=["d1", "d2"]), group("U", peers=["d1"]), group("X")],
        peers=[peer("d1", ["All", "U"], user="u1"), peer("d2", ["All"], user="u1")],
        users=[user("u1", auto_groups=["U"])],
        **kw,
    )


def test_managed_groups_skip_all_non_api_and_routers():
    s = snap(groups=[group("All"), group("U"), {**group("J"), "issued": "jwt"}])
    assert managed_groups(s) == {"U"}


def test_peer_drift_against_user_default():
    d = peer_drift(_snap(), "d2")
    assert d.missing == {"U"} and d.extra == frozenset() and not d.ok


def test_peer_drift_against_explicit_target():
    d = peer_drift(_snap(), "d1", want={"X"})
    assert d.missing == {"X"} and d.extra == {"U"}


def test_user_drift_lists_only_drifting_peers():
    assert [d.peer_id for d in user_drift(_snap(), "u1")] == ["d2"]


def test_user_drift_with_new_auto_groups():
    assert [d.peer_id for d in user_drift(_snap(), "u1", {"U", "X"})] == ["d1", "d2"]


def test_alignment_changes_only_differing_groups():
    s = _snap()
    members = {gid: g.peer_ids for gid, g in s.groups.items()}
    out = alignment(members, managed_groups(s), {"d1", "d2"}, {"X"})
    assert out == {"U": frozenset(), "X": frozenset({"d1", "d2"})}


def test_alignment_keeps_other_peers():
    members = {"U": frozenset({"d1", "other"})}
    assert alignment(members, {"U"}, {"d1"}, set()) == {"U": frozenset({"other"})}


def test_alignment_noop_when_aligned():
    members = {"U": frozenset({"d1"})}
    assert alignment(members, {"U"}, {"d1"}, {"U"}) == {}
