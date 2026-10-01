from datetime import UTC, datetime

from birdseye_web.anomalies import CHECKS, find_anomalies
from birdseye_web.hygiene import HygieneOptions, parse_version
from birdseye_web.models import build_snapshot
from tests.factory import group, peer, policy, rule, user

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _peer(pid, **extra):
    return {**peer(pid, ["G"], user=extra.pop("user", "")), "connected": False, **extra}


def _by(findings, check):
    return [f for f in findings if f.check == check]


def _find(opts=None, **parts):
    s = build_snapshot(**parts)
    return find_anomalies(s, now=NOW, options=opts or HygieneOptions())


def test_every_finding_has_a_check():
    keys = {c.key for c in CHECKS}
    for k in (
        "stale-peer",
        "login-expired",
        "outdated-client",
        "approval-pending",
        "key-unused",
        "key-no-expiry",
        "key-unlimited",
        "key-expired",
        "user-no-device",
        "posture-unused",
    ):
        assert k in keys


def test_stale_peer_uses_threshold_and_ignores_online():
    hits = _by(
        _find(
            HygieneOptions(stale_days=30),
            peers=[
                _peer("old", last_seen="2026-08-01T00:00:00Z"),
                _peer("fresh", last_seen="2026-09-25T00:00:00Z"),
                _peer("online", last_seen="2026-01-01T00:00:00Z", connected=True),
                _peer("never", last_seen="0001-01-01T00:00:00Z"),
            ],
        ),
        "stale-peer",
    )
    assert [(h.peer_id, h.link) for h in hits] == [("old", "/peers/old")]
    assert "61 days" in hits[0].detail


def test_login_expired_and_approval():
    found = _find(
        peers=[_peer("x", login_expired=True), _peer("y", approval_required=True)],
    )
    assert [h.link for h in _by(found, "login-expired")] == ["/peers/x"]
    assert [h.link for h in _by(found, "approval-pending")] == ["/peers/y"]


def test_parse_version():
    assert parse_version("0.36.5") == (0, 36, 5)
    assert parse_version("v0.40.0-rc1") == (0, 40, 0)
    assert parse_version("development") is None and parse_version("") is None


def test_outdated_clients_against_newest_seen_or_minimum():
    peers = [_peer("a", version="0.40.1"), _peer("b", version="0.39.0"), _peer("c", version="dev")]
    assert [h.peer_id for h in _by(_find(peers=peers), "outdated-client")] == ["b"]
    hits = _by(_find(HygieneOptions(min_version="0.41.0"), peers=peers), "outdated-client")
    assert sorted(h.peer_id for h in hits) == ["a", "b"] and "0.41.0" in hits[0].detail


def _key(kid, **extra):
    return {
        "id": kid,
        "name": kid,
        "type": "reusable",
        "expires": "2027-01-01T00:00:00Z",
        "revoked": False,
        "used_times": 1,
        "usage_limit": 5,
        "auto_groups": [],
        **extra,
    }


def test_setup_key_findings():
    found = _find(
        setup_keys=[
            _key("ok"),
            _key("unused", used_times=0),
            _key("forever", expires="0001-01-01T00:00:00Z"),
            _key("unlimited", usage_limit=0),
            _key("dead", expires="2026-01-01T00:00:00Z"),
            _key("gone", expires="2026-01-01T00:00:00Z", revoked=True, used_times=0),
        ]
    )
    assert [h.link for h in _by(found, "key-unused")] == ["/setup-keys/unused"]
    assert [h.link for h in _by(found, "key-no-expiry")] == ["/setup-keys/forever"]
    assert [h.link for h in _by(found, "key-unlimited")] == ["/setup-keys/unlimited"]
    assert [h.link for h in _by(found, "key-expired")] == ["/setup-keys/dead"]


def test_users_without_device_skip_service_and_blocked():
    found = _find(
        users=[
            user("u1", "Has"),
            user("u2", "None"),
            {**user("svc"), "is_service_user": True},
            {**user("blk"), "is_blocked": True},
        ],
        peers=[_peer("p", user="u1")],
    )
    assert [h.link for h in _by(found, "user-no-device")] == ["/users/u2"]


def test_unused_posture_checks():
    found = _find(
        posture_checks=[{"id": "pc1", "name": "used"}, {"id": "pc2", "name": "spare"}],
        groups=[group("G")],
        policies=[policy("p", rule(["G"], ["G"]), posture=["pc1"])],
    )
    assert [h.title for h in _by(found, "posture-unused")] == ["spare"]
