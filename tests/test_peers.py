from datetime import UTC, datetime

from birdseye_web.diff import access_delta, with_peer_groups, without_peer
from birdseye_web.models import Ref, parse_peer
from birdseye_web.payloads import peer_payload
from birdseye_web.peers import PeerQuery, filter_peers, group_edit, peer_access, user_options
from tests.factory import group, peer, policy, rule, snap, user

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _peer(pid, groups=(), user="", **extra):
    return {**peer(pid, groups, user=user), **extra}


def test_parse_peer_extended_fields():
    p = parse_peer(
        _peer(
            "p1",
            hostname="host-1",
            version="0.36.5",
            last_seen="2026-09-30T10:00:00.123456Z",
            login_expiration_enabled=True,
            login_expired=True,
            ssh_enabled=True,
            approval_required=True,
            dns_label="host-1.netbird.selfhosted",
            os="Linux 6.1",
        )
    )
    assert p.hostname == "host-1" and p.version == "0.36.5"
    assert p.last_seen == datetime(2026, 9, 30, 10, 0, 0, 123456, tzinfo=UTC)
    assert p.login_expiration_enabled and p.login_expired and p.ssh_enabled
    assert p.approval_required and p.dns_label.startswith("host-1")


def test_parse_peer_tolerates_missing_and_bad_time():
    p = parse_peer(_peer("p1", last_seen="garbage"))
    assert p.last_seen is None and p.version == "" and not p.ssh_enabled


def _snap():
    return snap(
        peers=[
            _peer(
                "a", ["G"], user="u1", connected=True, os="linux", last_seen="2026-09-30T10:00:00Z"
            ),
            _peer(
                "b",
                [],
                user="u2",
                os="windows",
                login_expired=True,
                last_seen="2026-09-01T10:00:00Z",
            ),
            _peer("c", ["G"], os="darwin"),
        ],
        groups=[group("ALL", "All", peers=["a", "b", "c"]), group("G", "Gee", peers=["a", "c"])],
        users=[user("u1", "Alice"), user("u2", "Bob")],
        policies=[policy("p1", rule(["G"], ["G"]))],
    )


def test_filter_sorts_by_last_seen_newest_first_unknown_last():
    assert [p.id for p in filter_peers(_snap(), PeerQuery())] == ["a", "b", "c"]


def test_filter_by_text_status_user_group_os_expired():
    s = _snap()
    ids = lambda q: [p.id for p in filter_peers(s, q)]  # noqa: E731
    assert ids(PeerQuery(q="WINDOWS")) == ["b"]
    assert ids(PeerQuery(q="bob")) == ["b"]  # user name
    assert ids(PeerQuery(status="online")) == ["a"]
    assert ids(PeerQuery(status="offline")) == ["b", "c"]
    assert ids(PeerQuery(user="u2")) == ["b"]
    assert ids(PeerQuery(user="-")) == ["c"]  # no user
    assert ids(PeerQuery(group="G")) == ["a", "c"]
    assert ids(PeerQuery(os="darwin")) == ["c"]
    assert ids(PeerQuery(expired=True)) == ["b"]


def test_filter_from_query_params_ignores_unknown_values():
    q = PeerQuery.from_params({"status": "weird", "expired": "1", "q": " x "})
    assert q == PeerQuery(q="x", expired=True)


def test_user_options_only_users_with_peers():
    assert [u.id for u in user_options(_snap())] == ["u1", "u2"]


def test_peer_access_both_directions_grouped_by_counterpart():
    acc = peer_access(_snap(), "a")
    assert [(x.ref, x.services) for x in acc.outgoing] == [(Ref("peer", "c"), ("all",))]
    assert [(x.ref, x.services) for x in acc.incoming] == [(Ref("peer", "c"), ("all",))]
    assert acc.outgoing[0].policy_ids == ("p1",)


def test_without_peer_removes_its_access():
    s = _snap()
    d = access_delta(s, without_peer(s, "a"))
    assert not d.gained and {(x.src.id, x.dst.id) for x in d.lost} == {("a", "c"), ("c", "a")}


def test_with_peer_groups_moves_membership():
    s = _snap()
    after = with_peer_groups(s, "b", {"G"})
    assert "b" in after.groups["G"].peer_ids and "G" in after.peers["b"].group_ids
    d = access_delta(s, after)
    assert {(x.src.id, x.dst.id) for x in d.gained} == {
        ("a", "b"),
        ("b", "a"),
        ("b", "c"),
        ("c", "b"),
    }


def test_group_edit_applies_only_the_users_change():
    editable = frozenset({"G", "H", "K"})
    # page showed G checked; user unticked G and ticked H; K was added elsewhere meanwhile
    current = frozenset({"G", "K", "ALL"})
    assert group_edit(current, ("H",), ("G",), editable) == (frozenset({"H"}), frozenset({"G"}))


def test_peer_payload_carries_required_fields():
    fresh = {
        "id": "p1",
        "name": "old",
        "ssh_enabled": False,
        "login_expiration_enabled": True,
        "inactivity_expiration_enabled": False,
        "approval_required": False,
    }
    assert peer_payload(fresh, name=" new ", ssh_enabled=True) == {
        "name": "new",
        "ssh_enabled": True,
        "login_expiration_enabled": True,
        "inactivity_expiration_enabled": False,
    }
    assert peer_payload(fresh, approved=True)["approval_required"] is False


def test_peer_payload_requires_name():
    import pytest

    from birdseye_web.payloads import PayloadError

    with pytest.raises(PayloadError):
        peer_payload({"name": "x"}, name="  ")


def test_without_peer_drops_direct_peer_rules():
    s = snap(
        peers=[_peer("a"), _peer("c", ["G"])],
        groups=[group("G", peers=["c"])],
        policies=[policy("p", rule(["G"], dst_resource={"id": "a", "type": "peer"}))],
    )
    d = access_delta(s, without_peer(s, "a"))
    assert {(x.src.id, x.dst.id) for x in d.lost} == {("c", "a")}


def test_go_zero_time_is_never():
    assert parse_peer(_peer("x", last_seen="0001-01-01T00:00:00Z")).last_seen is None


def test_ago_and_links():
    from birdseye_web.ui import ago, obj_link

    assert ago(None) == ""
    assert ago(datetime(2026, 10, 1, 9, 0, tzinfo=UTC), NOW) == "3h ago"
    assert ago(datetime(2026, 9, 21, 12, 0, tzinfo=UTC), NOW) == "10d ago"
    assert obj_link("p:x") == "/peers/x" and obj_link("peer:x") == "/peers/x"
    assert obj_link("g:x") == "/groups/x" and obj_link("zz:x") == "" and obj_link("p:") == ""
