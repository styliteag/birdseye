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
    login(c)
    c.headers["X-CSRF-Token"] = csrf(c)
    return c


def test_filled_cell_offers_revoke_actions(client):
    html = client.get("/matrix/cell?row=g:A&col=g:B&view=groups").text
    assert "/matrix/revoke" in html
    assert "Remove “Admins” from this rule" in html
    assert "Delete the whole policy" in html


def test_peer_view_popover_is_read_only(client):
    html = client.get("/matrix/cell?row=p:a1&col=p:b1&view=peers").text
    assert "/matrix/revoke" not in html and "Switch to the Group" in html


def test_empty_cell_offers_allow_form(client):
    html = client.get("/matrix/cell?row=g:B&col=g:A&view=groups").text
    assert "No access" in html and "/matrix/allow" in html


def test_allow_preview_then_confirm(client, nb):
    form = {"row": "g:B", "col": "g:A", "protocol": "tcp", "ports": "443"}
    preview = client.post("/matrix/allow", data={**form, "preview": "1"})
    assert "1 gained" in preview.text and 'name="row" value="g:B"' in preview.text
    assert nb.writes == []

    done = client.post("/matrix/allow", data=form)
    assert done.headers["HX-Trigger"] == "matrix-changed"
    method, path, body = nb.writes[-1]
    assert (method, path) == ("POST", "policies")
    assert body["name"] == "Servers -> Admins"
    assert body["rules"][0]["ports"] == ["443"]


def test_allow_bad_ports_shows_error_and_writes_nothing(client, nb):
    r = client.post("/matrix/allow", data={"row": "g:B", "col": "g:A", "protocol": "icmp",
                                           "ports": "22", "preview": "1"})
    assert "ports only apply" in r.text and nb.writes == []


def test_revoke_preview_then_delete(client, nb):
    form = {"policy_id": "p1", "rule_id": "r1", "mode": "remove_source", "target": "A"}
    preview = client.post("/matrix/revoke", data={**form, "preview": "1"})
    assert "lost" in preview.text and "Delete policy" in preview.text and nb.writes == []

    client.post("/matrix/revoke", data=form)
    assert nb.writes[-1] == ("DELETE", "policies/p1", None)


def test_revoke_disable_puts_full_policy(client, nb):
    client.post("/matrix/revoke", data={"policy_id": "p1", "rule_id": "r1", "mode": "disable"})
    method, path, body = nb.writes[-1]
    assert (method, path) == ("PUT", "policies/p1") and body["enabled"] is False


def test_revoke_unknown_policy(client, nb):
    r = client.post("/matrix/revoke", data={"policy_id": "nope", "rule_id": "r1", "mode": "disable"})
    assert "not found" in r.text and nb.writes == []


def test_quick_edit_requires_csrf(client, nb):
    del client.headers["X-CSRF-Token"]
    r = client.post("/matrix/allow", data={"row": "g:B", "col": "g:A"})
    assert r.status_code == 403 and nb.writes == []
