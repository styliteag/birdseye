import json

import pytest

from birdseye_web.history import (
    diff_kind,
    list_snapshots,
    load_kind,
    normalize,
    object_timeline,
)
from tests.factory import group, peer, policy, rule

A, B, C = "20260901T000000Z", "20260915T000000Z", "20260930T000000Z"


def _write(base, stamp, **kinds):
    d = base / stamp
    d.mkdir(parents=True)
    for slug, data in kinds.items():
        (d / f"{slug}.json").write_text(json.dumps(data))


def test_list_snapshots_newest_first_and_safe(tmp_path):
    _write(tmp_path, A)
    _write(tmp_path, B)
    (tmp_path / ".tmp-20261001T000000Z").mkdir()
    assert [s.stamp for s in list_snapshots(tmp_path)] == [B, A]
    assert list_snapshots(tmp_path / "missing") == []


def test_load_kind_validates_stamp_and_slug(tmp_path):
    _write(tmp_path, A, groups=[group("G")], dns_settings={"disabled_management_groups": []})
    assert load_kind(tmp_path, A, "groups")[0]["id"] == "G"
    assert load_kind(tmp_path, A, "dns_settings") == [
        {"id": "dns_settings", "disabled_management_groups": []}
    ]
    assert load_kind(tmp_path, A, "policies") == []  # file missing
    with pytest.raises(ValueError):
        load_kind(tmp_path, "../etc", "groups")
    with pytest.raises(ValueError):
        load_kind(tmp_path, A, "../../secrets")


def test_normalize_drops_volatile_and_flattens_embedded():
    p = {**peer("p1", ["G"]), "connected": True, "last_seen": "x", "version": "0.1"}
    assert normalize("peers", p) == {
        "id": "p1",
        "name": "p1",
        "ip": "100.64.0.1",
        "user_id": "",
        "groups": ["G"],
    }
    pol = normalize("policies", {**policy("x", rule(["G"], ["H"])), "description": None})
    assert pol["rules"][0]["sources"] == ["G"] and pol["id"] == "x"


def test_diff_kind_added_removed_changed():
    old = [group("G", "Gee", peers=["a"]), group("X")]
    new = [group("G", "Gee", peers=["a", "b"]), group("N", "New")]
    d = diff_kind("groups", old, new)
    assert d.added == (("N", "New"),) and d.removed == (("X", "X"),)
    [ch] = d.changed
    assert ch.id == "G" and ch.fields == (("peers", ["a"], ["a", "b"]),)
    assert not diff_kind("groups", old, old).changes


def test_peer_noise_is_not_a_change():
    old = [{**peer("p"), "connected": True, "last_seen": "1"}]
    new = [{**peer("p"), "connected": False, "last_seen": "2"}]
    assert not diff_kind("peers", old, new).changes


def test_object_timeline(tmp_path):
    _write(tmp_path, A, groups=[group("G", "One")])
    _write(tmp_path, B, groups=[group("G", "One")])
    _write(tmp_path, C, groups=[group("G", "Two")])
    t = object_timeline(tmp_path, "G")
    assert [(v.stamp, v.slug, v.name, v.event) for v in t] == [
        (C, "groups", "Two", "changed"),
        (A, "groups", "One", "first seen"),
    ]
    assert t[0].fields == (("name", "One", "Two"),)


def test_normalize_tolerates_odd_policy():
    assert normalize("policies", {"id": "x"}) == {"id": "x"}
