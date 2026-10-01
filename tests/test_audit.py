from datetime import UTC, date, datetime

from birdseye_web.audit import (
    AuditQuery,
    filter_events,
    initiator_label,
    parse_event,
    target_info,
)
from birdseye_web.models import build_snapshot
from tests.factory import group, peer, policy, rule, user


def _ev(eid, code, target="", ts="2026-09-30T10:00:00Z", **extra):
    return {
        "id": str(eid),
        "timestamp": ts,
        "activity": code.replace(".", " "),
        "activity_code": code,
        "initiator_id": "u1",
        "initiator_name": "Alice",
        "initiator_email": "a@x",
        "target_id": target,
        "meta": None,
        **extra,
    }


SNAP = build_snapshot(
    peers=[peer("p1")],
    groups=[group("G", "Admins")],
    users=[user("u1", "Alice"), user("u2", "Bob")],
    policies=[policy("pol", rule(["G"], ["G"]))],
    setup_keys=[{"id": "k1", "name": "office", "type": "reusable"}],
)


def test_parse_event():
    e = parse_event(_ev(7, "group.add", "G", meta={"name": "Admins"}))
    assert e.id == 7 and e.timestamp == datetime(2026, 9, 30, 10, tzinfo=UTC)
    assert e.category == "group" and dict(e.meta) == {"name": "Admins"}


def test_target_resolves_names_and_links():
    assert target_info(parse_event(_ev(1, "group.update", "G")), SNAP) == ("Admins", "/groups/G")
    assert target_info(parse_event(_ev(1, "policy.update", "pol")), SNAP) == (
        "pol",
        "/policies/pol",
    )
    assert target_info(parse_event(_ev(1, "user.role.update", "u2")), SNAP) == ("Bob", "/users/u2")
    assert target_info(parse_event(_ev(1, "peer.rename", "p1")), SNAP) == ("p1", "/peers/p1")
    assert target_info(parse_event(_ev(1, "setupkey.update", "k1")), SNAP) == (
        "office",
        "/setup-keys/k1",
    )
    res = parse_event(_ev(1, "network.resource.create", "r9"))
    assert target_info(res, SNAP) == ("r9", "")  # not (or no longer) visible: no link


def test_deleted_target_uses_meta_name_without_link():
    e = parse_event(_ev(1, "group.delete", "gone", meta={"name": "Old group"}))
    assert target_info(e, SNAP) == ("Old group (deleted)", "")


def test_initiator_label_fallbacks():
    assert initiator_label(parse_event(_ev(1, "x")), SNAP) == "Alice"
    blank = {"initiator_name": "", "initiator_email": ""}
    assert initiator_label(parse_event(_ev(1, "x", initiator_id="sys", **blank)), SNAP) == "system"
    assert (
        initiator_label(parse_event(_ev(1, "x", initiator_id="k1", **blank)), SNAP)
        == "setup key office"
    )
    assert initiator_label(parse_event(_ev(1, "x", initiator_id="u2", **blank)), SNAP) == "Bob"


def _events():
    return [
        parse_event(_ev(1, "group.add", "G", ts="2026-09-01T10:00:00Z")),
        parse_event(_ev(2, "policy.update", "pol", ts="2026-09-20T10:00:00Z")),
        parse_event(
            _ev(
                3,
                "peer.rename",
                "p1",
                ts="2026-09-30T10:00:00Z",
                initiator_id="u2",
                initiator_name="Bob",
            )
        ),
    ]


def test_filter_newest_first_and_by_fields():
    ev = _events()
    ids = lambda q: [e.id for e in filter_events(ev, q, SNAP)]  # noqa: E731
    assert ids(AuditQuery()) == [3, 2, 1]
    assert ids(AuditQuery(initiator="u2")) == [3]
    assert ids(AuditQuery(activity="policy")) == [2]  # category
    assert ids(AuditQuery(activity="peer.rename")) == [3]  # exact code
    assert ids(AuditQuery(target="G")) == [1]
    assert ids(AuditQuery(since=date(2026, 9, 15))) == [3, 2]
    assert ids(AuditQuery(until=date(2026, 9, 20))) == [2, 1]  # inclusive day
    assert ids(AuditQuery(q="admins")) == [1]  # resolved target name


def test_query_from_params_ignores_bad_dates():
    q = AuditQuery.from_params({"since": "2026-09-01", "until": "nope", "target": "G "})
    assert q == AuditQuery(target="G", since=date(2026, 9, 1))


def test_via_birdseye_matches_user_time_and_target():
    from birdseye_web.audit import via_birdseye
    from birdseye_web.context import WriteRecord

    at = datetime(2026, 9, 30, 10, 0, 5, tzinfo=UTC)
    writes = [WriteRecord("u1", "update group G", at), WriteRecord("u1", "create policy", at)]
    ev = lambda code, target, **x: parse_event(_ev(1, code, target, **x))  # noqa: E731
    assert via_birdseye(ev("group.update", "G"), writes)
    assert not via_birdseye(ev("group.update", "H"), writes)  # other object
    assert not via_birdseye(ev("group.update", "G", initiator_id="u2"), writes)  # other user
    assert not via_birdseye(ev("group.update", "G", ts="2026-09-30T11:00:00Z"), writes)  # later
    assert via_birdseye(ev("policy.add", "newid"), writes)  # create: ID unknown when logged


def test_date_filter_uses_viewers_time_zone():
    # 23:30 UTC on the 30th is 01:30 on Oct 1st in CEST (getTimezoneOffset = -120)
    ev = [parse_event(_ev(1, "group.add", "G", ts="2026-09-30T23:30:00Z"))]
    ids = lambda q: [e.id for e in filter_events(ev, q, SNAP)]  # noqa: E731
    assert ids(AuditQuery(since=date(2026, 10, 1))) == []
    assert ids(AuditQuery(since=date(2026, 10, 1), tz_offset=-120)) == [1]
    q = AuditQuery.from_params({"tz": "-120"})
    assert q.tz_offset == -120
    assert AuditQuery.from_params({"tz": "9999"}).tz_offset == 0  # out of range


def test_peer_target_given_as_ip_resolves():
    # NetBird logs some peer events (e.g. peer.ssh.enable) with the peer's IP as target
    e = parse_event(_ev(1, "peer.ssh.enable", "100.64.0.1", meta={"name": "p1"}))
    assert target_info(e, SNAP) == ("p1", "/peers/p1")
