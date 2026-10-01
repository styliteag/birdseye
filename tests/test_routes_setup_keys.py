import logging

import pytest
from fastapi.testclient import TestClient

from birdseye_web.app import create_app
from tests.fakes import ME, SETTINGS, FakeNetBird, FakeOIDC
from tests.test_routes import csrf, login

SECRET = "4F0D2A63-1C2B-4C0E-9F55-SECRETVALUE1"


def _key(kid="k1", **extra):
    return {
        "id": kid,
        "key": "4F0D****",
        "name": "office",
        "type": "reusable",
        "expires": "2099-01-01T00:00:00Z",
        "valid": True,
        "revoked": False,
        "used_times": 0,
        "usage_limit": 0,
        "auto_groups": ["A"],
        "state": "valid",
        **extra,
    }


@pytest.fixture
def nb():
    n = FakeNetBird()
    n.data["setup-keys"] = [_key(), _key("k2", name="old", revoked=True, state="revoked")]
    n.post_extra["setup-keys"] = {"key": SECRET}
    return n


@pytest.fixture
def client(nb):
    oidc = FakeOIDC()
    c = TestClient(create_app(SETTINGS, oidc=oidc, api_factory=nb.api), follow_redirects=False)
    c.oidc = oidc
    return c


def test_list_shows_states(client):
    login(client)
    html = client.get("/setup-keys").text
    assert "office" in html and "key-revoked" in html and "4F0D" not in html
    assert "old" not in client.get("/setup-keys?state=valid").text


def test_create_shows_key_once_and_never_logs_it(client, nb, caplog):
    login(client)
    token = csrf(client, "/setup-keys/new")
    with caplog.at_level(logging.DEBUG):
        r = client.post(
            "/setup-keys",
            data={
                "csrf": token,
                "name": "lab",
                "type": "one-off",
                "expires_days": "7",
                "usage_limit": "0",
                "groups": ["A", "ALL"],  # "All" is not offered and is dropped
            },
        )
    assert r.status_code == 200 and SECRET in r.text
    assert r.headers["cache-control"] == "no-store"
    assert SECRET not in caplog.text
    assert nb.writes[-1] == (
        "POST",
        "setup-keys",
        {
            "name": "lab",
            "type": "one-off",
            "expires_in": 7 * 86400,
            "usage_limit": 0,
            "auto_groups": ["A"],
            "ephemeral": False,
            "allow_extra_dns_labels": False,
        },
    )
    # the list afterwards never has it
    assert SECRET not in client.get("/setup-keys").text


def test_create_invalid_rerenders(client, nb):
    login(client)
    token = csrf(client, "/setup-keys/new")
    r = client.post("/setup-keys", data={"csrf": token, "name": "", "type": "one-off"})
    assert r.status_code == 400 and nb.writes == []


def test_update_auto_groups_and_revoke(client, nb):
    login(client)
    token = csrf(client, "/setup-keys/k1")
    r = client.post(
        "/setup-keys/k1",
        data={
            "csrf": token,
            "groups_field": "1",
            "groups_initial": ["A"],
            "groups": ["B"],
            "revoke": "1",
        },
    )
    assert r.status_code == 303
    assert nb.writes == [("PUT", "setup-keys/k1", {"auto_groups": ["B"], "revoked": True})]


def test_update_unchanged_writes_nothing(client, nb):
    login(client)
    token = csrf(client, "/setup-keys/k1")
    client.post(
        "/setup-keys/k1",
        data={"csrf": token, "groups_field": "1", "groups_initial": ["A"], "groups": ["A"]},
    )
    assert nb.writes == []


def test_delete_only_revoked(client, nb):
    login(client)
    token = csrf(client, "/setup-keys/k1")
    assert client.post("/setup-keys/k1/delete", data={"csrf": token}).status_code == 400
    assert nb.writes == []
    r = client.post("/setup-keys/k2/delete", data={"csrf": token})
    assert r.status_code == 303 and nb.writes == [("DELETE", "setup-keys/k2", None)]


def test_hidden_for_roles_without_permission(client, nb, monkeypatch):
    monkeypatch.setitem(nb.data, "users/current", {**ME, "role": "user"})
    login(client)
    assert "/setup-keys" not in client.get("/matrix").text
    assert client.get("/setup-keys").status_code == 403
    assert client.get("/setup-keys/new").status_code == 403


def test_nav_for_admin(client):
    login(client)
    assert 'href="/setup-keys"' in client.get("/matrix").text
