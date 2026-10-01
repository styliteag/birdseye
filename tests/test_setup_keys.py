from datetime import UTC, datetime

import pytest

from birdseye_web.models import build_snapshot, parse_setup_key
from birdseye_web.payloads import PayloadError, setup_key_create_payload, setup_key_update_payload

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _key(**extra):
    return {
        "id": "k1",
        "key": "A616****",
        "name": "office",
        "type": "reusable",
        "expires": "2027-01-01T00:00:00Z",
        "valid": True,
        "revoked": False,
        "used_times": 2,
        "usage_limit": 0,
        "auto_groups": ["G"],
        "ephemeral": False,
        "last_used": "2026-09-30T10:00:00Z",
        "state": "valid",
        **extra,
    }


def test_parse_setup_key_never_keeps_the_key_value():
    k = parse_setup_key(_key())
    assert not hasattr(k, "key")
    assert k.name == "office" and k.type == "reusable" and k.used_times == 2
    assert k.auto_groups == frozenset({"G"}) and k.expires == datetime(2027, 1, 1, tzinfo=UTC)
    assert k.last_used == datetime(2026, 9, 30, 10, tzinfo=UTC)


@pytest.mark.parametrize(
    "extra, state",
    [
        ({}, "valid"),
        ({"revoked": True, "state": "revoked"}, "revoked"),
        ({"expires": "2026-01-01T00:00:00Z", "state": "expired", "valid": False}, "expired"),
        ({"type": "one-off", "used_times": 1, "state": "overused", "valid": False}, "exhausted"),
        ({"usage_limit": 3, "used_times": 3, "state": ""}, "exhausted"),
        ({"state": ""}, "valid"),
    ],
)
def test_setup_key_state(extra, state):
    assert parse_setup_key(_key(**extra)).state_at(NOW) == state


def test_snapshot_holds_setup_keys():
    s = build_snapshot(setup_keys=[_key()])
    assert s.setup_keys["k1"].name == "office" and s.setup_key_group_ids == frozenset({"G"})


def test_create_payload():
    body = setup_key_create_payload(
        name=" lab ",
        type="one-off",
        expires_days=7,
        usage_limit=0,
        auto_groups=["G", "G"],
        ephemeral=True,
    )
    assert body == {
        "name": "lab",
        "type": "one-off",
        "expires_in": 7 * 86400,
        "usage_limit": 0,
        "auto_groups": ["G"],
        "ephemeral": True,
        "allow_extra_dns_labels": False,
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": ""},
        {"type": "forever"},
        {"expires_days": 0},
        {"expires_days": 366},
        {"usage_limit": -1},
    ],
)
def test_create_payload_rejects(kwargs):
    base = dict(name="x", type="reusable", expires_days=30, usage_limit=0, auto_groups=[])
    with pytest.raises(PayloadError):
        setup_key_create_payload(**{**base, **kwargs})


def test_update_payload_carries_over():
    fresh = _key(auto_groups=[{"id": "G", "name": "G"}], revoked=False)
    assert setup_key_update_payload(fresh) == {"auto_groups": ["G"], "revoked": False}
    assert setup_key_update_payload(fresh, auto_groups=["H"], revoked=True) == {
        "auto_groups": ["H"],
        "revoked": True,
    }


def test_revoked_key_cannot_be_unrevoked():
    with pytest.raises(PayloadError):
        setup_key_update_payload(_key(revoked=True), revoked=False)
