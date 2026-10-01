import re

import pytest
from fastapi.testclient import TestClient

from birdseye_web.app import create_app
from tests.fakes import SETTINGS, FakeNetBird, FakeOIDC
from tests.test_routes import csrf, login


@pytest.fixture
def nb():
    return FakeNetBird()


@pytest.fixture
def client(nb):
    oidc = FakeOIDC()
    c = TestClient(create_app(SETTINGS, oidc=oidc, api_factory=nb.api), follow_redirects=False)
    c.oidc = oidc
    return c


BOB = "/users/u2"


def _post(client, data, path=BOB):
    token = csrf(client, path)
    return client.post(path, data={"csrf": token, "user_field": "1", **data})


def _puts(nb):
    return [(p, b) for m, p, b in nb.writes if m == "PUT"]


def test_user_list_hides_service_users_and_counts_drift(client, nb):
    nb.data["users"][1]["auto_groups"] = ["A"]  # Bob's c1 is not in A
    login(client)
    html = client.get("/users").text
    assert "Bob" in html and "Robot" not in html
    assert 'href="/users/u2"><span class="pill sev-warning">1<' in html


def test_user_editor_shows_drift(client, nb):
    nb.data["users"][1]["auto_groups"] = ["A"]
    login(client)
    html = client.get(BOB).text
    assert "+ Admins" in html and 'name="align" value="c1"' in html


def test_service_user_is_not_editable(client):
    login(client)
    assert client.get("/users/svc").status_code == 404


def test_save_auto_groups(client, nb):
    login(client)
    r = _post(client, {"role": "user", "groups_field": "1", "groups": ["A"]})
    assert r.status_code == 303, r.text
    assert _puts(nb) == [("users/u2", {"role": "user", "auto_groups": ["A"], "is_blocked": False})]


def test_unchanged_form_writes_nothing(client, nb):
    login(client)
    _post(client, {"role": "user", "groups_field": "1"})
    assert nb.writes == []


def test_group_assigned_elsewhere_is_kept(client, nb):
    nb.data["users"][1]["auto_groups"] = ["B"]  # not in groups_initial: added after page load
    login(client)
    _post(client, {"role": "user", "groups_field": "1", "groups": ["A"]})
    assert _puts(nb)[0][1]["auto_groups"] == ["A", "B"]


def test_change_role_and_block(client, nb):
    login(client)
    _post(client, {"role": "auditor", "blocked": "1"})
    assert _puts(nb) == [("users/u2", {"role": "auditor", "auto_groups": [], "is_blocked": True})]


def test_owner_role_cannot_be_given(client, nb):
    login(client)
    r = _post(client, {"role": "owner"})
    assert r.status_code == 400 and "cannot be changed" in r.text
    assert nb.writes == []


def test_own_role_and_block_are_ignored(client, nb):
    login(client)
    html = client.get("/users/u1").text
    assert "this is you" in html
    _post(client, {"role": "user", "blocked": "1"}, path="/users/u1")
    assert nb.writes == []


def test_fix_device_groups(client, nb):
    nb.data["users"][1]["auto_groups"] = ["A"]
    nb.data["peers"][2]["groups"] = [{"id": "B"}]
    nb.data["groups"][2]["peers"] = [{"id": "b1"}, {"id": "c1"}]
    login(client)
    _post(
        client,
        {
            "role": "user",
            "groups_field": "1",
            "groups": ["A"],
            "groups_initial": ["A"],
            "align": ["c1"],
        },
    )
    puts = dict(_puts(nb))
    assert "users/u2" not in puts  # nothing changed on the user
    assert puts["groups/A"]["peers"] == ["a1", "c1"]
    assert puts["groups/B"] == {
        "name": "Servers",
        "peers": ["b1"],
        "resources": [{"id": "r1", "type": "host"}],
    }


def test_fix_ignores_other_users_devices(client, nb):
    login(client)
    _post(client, {"role": "user", "align": ["a1"]})  # a1 belongs to Alice
    assert nb.writes == []


def test_user_saved_before_devices_are_fixed(client, nb):
    login(client)
    _post(client, {"role": "user", "groups_field": "1", "groups": ["A"], "align": ["c1"]})
    assert [p for p, _ in _puts(nb)] == ["users/u2", "groups/A"]


def test_preview_shows_gain(client):
    login(client)
    token = csrf(client, BOB)
    r = client.post(
        f"{BOB}/preview",
        data={"groups_field": "1", "groups": ["A"]},
        headers={"X-CSRF-Token": token},
    )
    assert "2 gained" in r.text  # c1 -> b1 and -> r1, through propagation


@pytest.mark.parametrize("propagation,gain", [(True, True), (False, False)])
def test_preview_follows_propagation_setting(client, nb, propagation, gain):
    nb.data["accounts"][0]["settings"]["groups_propagation_enabled"] = propagation
    login(client)
    token = csrf(client, BOB)
    r = client.post(
        f"{BOB}/preview",
        data={"groups_field": "1", "groups": ["A"]},
        headers={"X-CSRF-Token": token},
    )
    assert ("gained" in r.text) is gain


def test_preview_with_fix_without_propagation(client, nb):
    nb.data["accounts"][0]["settings"]["groups_propagation_enabled"] = False
    login(client)
    token = csrf(client, BOB)
    data = {"groups_field": "1", "groups": ["A"], "align": ["c1"]}
    r = client.post(f"{BOB}/preview", data=data, headers={"X-CSRF-Token": token})
    assert "2 gained" in r.text


def test_anomaly_links_to_user_editor(client, nb):
    nb.data["users"][1]["auto_groups"] = ["A"]
    login(client)
    assert 'href="/users/u2"' in client.get("/anomalies").text


def test_editor_hides_controls_without_permission(client, nb):
    nb.data["users"][1]["auto_groups"] = ["A"]
    nb.data["users/current"] = {**nb.data["users/current"], "permissions": {"modules": {}}}
    login(client)
    html = client.get(BOB).text
    assert 'name="align"' not in html and 'name="groups_field"' not in html
    assert re.search(r'<select name="role"[^>]*disabled', html)


def test_fix_ignored_without_groups_permission(client, nb):
    nb.data["users"][1]["auto_groups"] = ["A"]
    nb.data["users/current"] = {
        **nb.data["users/current"],
        "permissions": {"modules": {"users": {"update": True}}},
    }
    login(client)
    _post(client, {"role": "user", "align": ["c1"]})
    assert nb.writes == []
