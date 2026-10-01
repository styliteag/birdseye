import pytest
from fastapi.testclient import TestClient

from birdseye_web.app import create_app
from tests.fakes import SETTINGS, FakeNetBird, FakeOIDC
from tests.test_routes import csrf, login


@pytest.fixture
def nb():
    n = FakeNetBird()
    n.data["peers"][1].update(os="Windows 11", login_expired=True, last_seen="2026-09-01T10:00:00Z")
    return n


@pytest.fixture
def client(nb):
    oidc = FakeOIDC()
    c = TestClient(create_app(SETTINGS, oidc=oidc, api_factory=nb.api), follow_redirects=False)
    c.oidc = oidc
    return c


def _puts(nb):
    return [(p, b) for m, p, b in nb.writes if m == "PUT"]


def test_peer_list_and_filters(client):
    login(client)
    html = client.get("/peers").text
    assert 'href="/peers/a1"' in html and 'href="/peers/b1"' in html and "login expired" in html
    rows = client.get("/peers?expired=1", headers={"HX-Target": "peer-rows"}).text
    assert "<html" not in rows and "/peers/b1" in rows and "/peers/a1" not in rows
    assert "/peers/c1" in client.get("/peers?user=u2").text
    assert "/peers/a1" not in client.get("/peers?os=windows").text


def test_peer_editor_shows_effective_access(client):
    login(client)
    html = client.get("/peers/a1").text
    assert "This peer can access" in html
    assert 'href="/peers/b1"' in html and "tcp/22" in html


def test_peer_settings_put_from_fresh_get(client, nb):
    nb.data["peers"][0].update(ssh_enabled=False, inactivity_expiration_enabled=True)
    login(client)
    token = csrf(client, "/peers/a1")
    r = client.post(
        "/peers/a1",
        data={"csrf": token, "settings_field": "1", "name": "laptop", "ssh_enabled": "1"},
    )
    assert r.status_code == 303, r.text
    assert _puts(nb) == [
        (
            "peers/a1",
            {
                "name": "laptop",
                "ssh_enabled": True,
                "login_expiration_enabled": False,
                "inactivity_expiration_enabled": True,
            },
        )
    ]


def test_unchanged_settings_write_nothing(client, nb):
    login(client)
    token = csrf(client, "/peers/a1")
    client.post("/peers/a1", data={"csrf": token, "settings_field": "1", "name": "a1"})
    assert nb.writes == []


def test_peer_group_add_keeps_group_resources(client, nb):
    login(client)
    token = csrf(client, "/peers/a1")
    r = client.post(
        "/peers/a1",
        data={
            "csrf": token,
            "groups_field": "1",
            "groups_initial": ["A"],
            "groups": ["A", "B"],
        },
    )
    assert r.status_code == 303, r.text
    assert _puts(nb) == [
        (
            "groups/B",
            {"name": "Servers", "peers": ["a1", "b1"], "resources": [{"id": "r1", "type": "host"}]},
        )
    ]


def test_peer_group_remove(client, nb):
    login(client)
    token = csrf(client, "/peers/a1")
    client.post("/peers/a1", data={"csrf": token, "groups_field": "1", "groups_initial": ["A"]})
    assert _puts(nb) == [("groups/A", {"name": "Admins", "peers": [], "resources": []})]


def test_peer_cannot_leave_all_group(client, nb):
    login(client)
    token = csrf(client, "/peers/a1")
    client.post(
        "/peers/a1",
        data={"csrf": token, "groups_field": "1", "groups_initial": ["A", "ALL"], "groups": ["A"]},
    )
    assert nb.writes == []


def test_peer_preview_shows_gain(client):
    login(client)
    token = csrf(client, "/peers/b1")
    r = client.post(
        "/peers/b1/preview",
        data={"groups_field": "1", "groups_initial": ["B"], "groups": ["A", "B"]},
        headers={"X-CSRF-Token": token},
    )
    assert "gained" in r.text and "b1" in r.text


def test_peer_delete_preview_never_deletes(client, nb):
    login(client)
    token = csrf(client, "/peers/a1")
    r = client.post("/peers/a1/delete-preview", headers={"X-CSRF-Token": token})
    assert r.status_code == 200 and "lost" in r.text and nb.writes == []


def test_peer_delete(client, nb):
    login(client)
    token = csrf(client, "/peers/a1")
    r = client.post("/peers/a1/delete", data={"csrf": token})
    assert r.status_code == 303 and nb.writes == [("DELETE", "peers/a1", None)]


def test_peer_delete_needs_csrf(client, nb):
    login(client)
    r = client.post("/peers/a1/delete")
    assert r.status_code == 403 and nb.writes == []


def test_unknown_peer_404(client):
    login(client)
    assert client.get("/peers/nope").status_code == 404


def test_peer_links_from_user_editor_reach_and_matrix(client):
    login(client)
    assert 'href="/peers/c1"' in client.get("/users/u2").text
    reach = client.get("/reach?src=p:a1&dst=p:b1").text
    assert 'href="/peers/a1"' in reach and 'href="/peers/b1"' in reach
    matrix = client.get("/matrix?view=peers").text
    assert 'href="/peers/a1"' in matrix
    cell = client.get("/matrix/cell?view=peers&row=p:a1&col=p:b1").text
    assert 'href="/peers/a1"' in cell


def test_anomalies_link_peer_editor(client):
    login(client)
    html = client.get("/anomalies").text  # c1 is only in "All"
    assert 'href="/peers/c1">peer</a>' in html
