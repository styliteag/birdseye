import json

import pytest
from fastapi.testclient import TestClient

from birdseye_web.app import create_app
from tests.factory import group, policy, rule
from tests.fakes import ME, SETTINGS, FakeNetBird, FakeOIDC
from tests.test_routes import csrf, login

OLD, NEW = "20260901T000000Z", "20260930T000000Z"


def _write(base, stamp, **kinds):
    d = base / "history" / stamp
    d.mkdir(parents=True)
    for slug, data in kinds.items():
        (d / f"{slug}.json").write_text(json.dumps(data))


@pytest.fixture
def jobs(tmp_path):
    # p1 used to allow everything; group "gone" was deleted since
    _write(
        tmp_path,
        OLD,
        policies=[policy("p1", rule(["A"], ["B"]))],
        groups=[group("A", "Admins", peers=["a1"]), group("gone", "Old", peers=["b1"])],
        peers=[{"id": "a1", "name": "a1", "connected": True}],
    )
    _write(
        tmp_path,
        NEW,
        policies=[policy("p1", rule(["A"], ["B"], protocol="tcp", ports=["22"]))],
        groups=[group("A", "Admins", peers=["a1"])],
        peers=[{"id": "a1", "name": "a1", "connected": False}],
    )
    return tmp_path


@pytest.fixture
def nb():
    return FakeNetBird()


@pytest.fixture
def client(nb, jobs):
    oidc = FakeOIDC()
    settings = SETTINGS.__class__(**{**SETTINGS.__dict__, "jobs_dir": str(jobs)})
    c = TestClient(create_app(settings, oidc=oidc, api_factory=nb.api), follow_redirects=False)
    c.oidc = oidc
    return c


def test_diff_page_shows_config_changes_only(client):
    login(client)
    html = client.get("/history").text
    assert "Policies" in html and "Groups" in html and "Peers" not in html.split("<main")[1]
    assert "removed" in html and 'href="/history/20260901T000000Z/gone"' in html


def test_object_timeline(client):
    login(client)
    html = client.get("/history/object/p1").text
    assert "changed" in html and "first seen" in html


def test_restore_preview_and_put(client, nb):
    login(client)
    page = client.get(f"/history/{OLD}/p1")
    assert page.status_code == 200 and "Replaces the current" in page.text
    token = csrf(client, f"/history/{OLD}/p1")
    r = client.post(f"/history/{OLD}/p1/preview", headers={"X-CSRF-Token": token})
    assert "gained" in r.text and nb.writes == []
    r = client.post(f"/history/{OLD}/p1/restore", data={"csrf": token})
    assert r.status_code == 303 and r.headers["location"] == "/policies/p1?saved=1"
    method, path, body = nb.writes[-1]
    assert (method, path) == ("PUT", "policies/p1") and body["rules"][0]["protocol"] == "all"


def test_restore_deleted_group_creates(client, nb):
    login(client)
    token = csrf(client, f"/history/{OLD}/gone")
    r = client.post(f"/history/{OLD}/gone/restore", data={"csrf": token})
    assert r.status_code == 303
    assert nb.writes[-1] == ("POST", "groups", {"name": "Old", "peers": ["b1"], "resources": []})


def test_restore_blocked_on_missing_group(client, nb, jobs):
    _write(jobs, "20260801T000000Z", policies=[policy("p1", rule(["Z"], ["B"]))])
    login(client)
    token = csrf(client, "/history/20260801T000000Z/p1")
    assert "no longer exists" in client.get("/history/20260801T000000Z/p1").text
    r = client.post("/history/20260801T000000Z/p1/restore", data={"csrf": token})
    assert r.status_code == 409 and nb.writes == []


def test_history_admin_only(client, nb, monkeypatch):
    monkeypatch.setitem(nb.data, "users/current", {**ME, "role": "user"})
    login(client)
    assert client.get("/history").status_code == 403
    assert client.get(f"/history/{OLD}/p1").status_code == 403
    assert 'href="/history"' not in client.get("/matrix").text


def test_bad_stamp_rejected(client):
    login(client)
    assert client.get("/history/..%2F..%2Fetc/p1").status_code == 404
    assert client.get("/history/2026/p1").status_code in (404, 422)


def test_history_not_configured(nb):
    oidc = FakeOIDC()
    c = TestClient(create_app(SETTINGS, oidc=oidc, api_factory=nb.api), follow_redirects=False)
    c.oidc = oidc
    login(c)
    r = c.get("/history")
    assert r.status_code == 404 and "not set up" in r.text


def test_demoted_admin_loses_history_access(client, nb):
    login(client)  # signed in as admin
    nb.data["users/current"] = {**ME, "role": "user"}  # demoted in NetBird since
    assert client.get("/history").status_code == 403


def test_corrupt_snapshot_files_are_skipped(client, jobs):
    (jobs / "history" / NEW / "groups.json").write_text("{not json")
    (jobs / "history" / "20241399T000000Z").mkdir()
    login(client)
    assert client.get("/history").status_code == 200


def test_diff_is_readable_field_by_field(client):
    login(client)
    html = client.get("/history").text
    assert "rules › r1 › protocol" in html and ">all</span> → <span" in html
    assert "+ 22" in html and "{&#34;action&#34;" not in html


def _registry(jobs, enabled=True, state=None):
    (jobs / "registry.json").write_text(
        json.dumps({"jobs": [{"key": "history", "label": "history", "enabled": enabled}]})
    )
    if state:
        (jobs / "state").mkdir(exist_ok=True)
        (jobs / "state" / "history.json").write_text(json.dumps(state))


def _change(nb, ts="2026-09-30T12:00:00Z", code="group.update"):
    nb.data["events/audit"] = [
        {"id": "1", "timestamp": ts, "activity": code, "activity_code": code, "initiator_id": "u1"}
    ]


def test_status_pending_after_change(client, nb, jobs):
    _registry(jobs)
    _change(nb)
    login(client)
    html = client.get("/history").text
    assert "changed at 12:00:00 UTC" in html and 'hx-get="/history/status?waiting=1"' in html


def test_status_unscheduled_and_login_noise(client, nb, jobs):
    _registry(jobs, enabled=False)
    _change(nb)
    login(client)
    assert "not scheduled" in client.get("/history").text
    _change(nb, code="user.peer.login")
    assert "changed at" not in client.get("/history").text


def test_status_running_and_reload_when_done(client, nb, jobs):
    _registry(jobs, state={"running": True, "started": "2026-09-30T12:00:30+00:00"})
    _change(nb)
    login(client)
    assert "being taken right now" in client.get("/history/status").text
    _registry(
        jobs, state={"running": False, "started": "2026-09-30T12:00:30+00:00", "exit_code": 0}
    )
    r = client.get("/history/status?waiting=1")
    assert r.headers.get("HX-Refresh") == "true"
