import pytest
from fastapi.testclient import TestClient

from birdseye_web.app import create_app
from tests.fakes import SETTINGS, FakeNetBird, FakeOIDC
from tests.test_routes import csrf, login

RES = {
    "id": "r1",
    "name": "nas",
    "address": "10.0.0.5/32",
    "type": "host",
    "groups": [{"id": "B", "name": "Servers"}],
    "enabled": True,
    "description": "",
}
ROUTER = {"id": "rt1", "peer": "b1", "metric": 100, "masquerade": True, "enabled": True}


@pytest.fixture
def nb():
    n = FakeNetBird()
    n.data["networks"] = [{"id": "n1", "name": "office", "description": ""}]
    n.data["networks/n1/resources"] = [RES]
    n.data["networks/n1/routers"] = [ROUTER]
    return n


@pytest.fixture
def client(nb):
    oidc = FakeOIDC()
    c = TestClient(create_app(SETTINGS, oidc=oidc, api_factory=nb.api), follow_redirects=False)
    c.oidc = oidc
    return c


def test_network_list(client):
    login(client)
    html = client.get("/networks").text
    assert (
        "office" in html
        and "/networks/n1/resources/r1" in html
        and "/networks/n1/routers/rt1" in html
    )


def test_network_create_rename_delete(client, nb):
    login(client)
    token = csrf(client, "/networks/new")
    r = client.post("/networks", data={"csrf": token, "name": "lab", "description": "x"})
    assert r.status_code == 303 and nb.writes[-1] == (
        "POST",
        "networks",
        {"name": "lab", "description": "x"},
    )
    r = client.post("/networks/n1", data={"csrf": token, "name": "hq"})
    assert nb.writes[-1] == ("PUT", "networks/n1", {"name": "hq", "description": ""})
    r = client.post("/networks/n1/delete-preview", headers={"X-CSRF-Token": token})
    assert "lost" in r.text and nb.writes[-1][0] == "PUT"
    r = client.post("/networks/n1/delete", data={"csrf": token})
    assert r.status_code == 303 and nb.writes[-1] == ("DELETE", "networks/n1", None)


def test_resource_create(client, nb):
    login(client)
    token = csrf(client, "/networks/n1/resources/new")
    r = client.post(
        "/networks/n1/resources",
        data={
            "csrf": token,
            "name": "web",
            "address": "web.example.com",
            "groups": ["B"],
            "enabled": "1",
        },
    )
    assert r.status_code == 303, r.text
    assert nb.writes[-1] == (
        "POST",
        "networks/n1/resources",
        {
            "name": "web",
            "address": "web.example.com",
            "groups": ["B"],
            "enabled": True,
            "description": "",
        },
    )


def test_resource_update_and_preview(client, nb):
    login(client)
    token = csrf(client, "/networks/n1/resources/r1")
    form = {"name": "nas", "address": "10.0.0.5/32", "groups": ["A"], "enabled": "1"}
    r = client.post("/networks/n1/resources/r1/preview", data=form, headers={"X-CSRF-Token": token})
    assert "lost" in r.text  # a1 reached r1 through Servers
    assert nb.writes == []
    r = client.post("/networks/n1/resources/r1", data={"csrf": token, **form})
    assert r.status_code == 303
    assert nb.writes == [
        (
            "PUT",
            "networks/n1/resources/r1",
            {**form, "groups": ["A"], "enabled": True, "description": ""},
        )
    ]


def test_resource_unchanged_writes_nothing(client, nb):
    login(client)
    token = csrf(client, "/networks/n1/resources/r1")
    client.post(
        "/networks/n1/resources/r1",
        data={
            "csrf": token,
            "name": "nas",
            "address": "10.0.0.5/32",
            "groups": ["B"],
            "enabled": "1",
        },
    )
    assert nb.writes == []


def test_resource_without_group_rejected(client, nb):
    login(client)
    token = csrf(client, "/networks/n1/resources/r1")
    r = client.post(
        "/networks/n1/resources/r1", data={"csrf": token, "name": "nas", "address": "x"}
    )
    assert r.status_code == 400 and nb.writes == []


def test_resource_delete(client, nb):
    login(client)
    token = csrf(client, "/networks/n1/resources/r1")
    r = client.post("/networks/n1/resources/r1/delete-preview", headers={"X-CSRF-Token": token})
    assert "lost" in r.text and nb.writes == []
    client.post("/networks/n1/resources/r1/delete", data={"csrf": token})
    assert nb.writes == [("DELETE", "networks/n1/resources/r1", None)]


def test_resource_link_redirects(client):
    login(client)
    r = client.get("/resources/r1")
    assert r.status_code == 303 and r.headers["location"] == "/networks/n1/resources/r1"
    assert client.get("/resources/nope").status_code == 404


def test_resource_in_other_network_404(client):
    login(client)
    assert client.get("/networks/n2/resources/r1").status_code == 404


def test_router_create_update_delete(client, nb):
    login(client)
    token = csrf(client, "/networks/n1/routers/new")
    r = client.post(
        "/networks/n1/routers",
        data={"csrf": token, "mode": "group", "peer_groups": ["A"], "metric": "50", "enabled": "1"},
    )
    assert r.status_code == 303
    assert nb.writes[-1] == (
        "POST",
        "networks/n1/routers",
        {"peer_groups": ["A"], "metric": 50, "masquerade": False, "enabled": True},
    )
    client.post(
        "/networks/n1/routers/rt1",
        data={"csrf": token, "mode": "peer", "peer": "b1", "metric": "100", "masquerade": "1"},
    )
    assert nb.writes[-1] == (
        "PUT",
        "networks/n1/routers/rt1",
        {"peer": "b1", "metric": 100, "masquerade": True, "enabled": False},
    )
    client.post("/networks/n1/routers/rt1/delete", data={"csrf": token})
    assert nb.writes[-1] == ("DELETE", "networks/n1/routers/rt1", None)


def test_router_both_peer_and_groups_rejected(client, nb):
    login(client)
    token = csrf(client, "/networks/n1/routers/new")
    r = client.post("/networks/n1/routers", data={"csrf": token, "mode": "peer", "metric": "1"})
    assert r.status_code == 400 and nb.writes == []


def test_group_put_keeps_resources_when_peers_change(client, nb):
    """Regression: a group that holds network resources keeps them on every group PUT path."""
    login(client)
    token = csrf(client, "/peers/a1")
    client.post(
        "/peers/a1",
        data={"csrf": token, "groups_field": "1", "groups_initial": ["A"], "groups": ["A", "B"]},
    )
    token = csrf(client, "/groups/B")
    client.post("/groups/B", data={"csrf": token, "name": "Servers", "peers": []})
    puts = [b for m, p, b in nb.writes if m == "PUT" and p == "groups/B"]
    assert len(puts) == 2 and all(b["resources"] == [{"id": "r1", "type": "host"}] for b in puts)


def test_anomaly_links_to_network_editors(client, nb):
    nb.data["networks/n1/routers"] = []
    login(client)
    html = client.get("/anomalies").text
    assert 'href="/networks/n1/routers/new"' in html and 'href="/resources/r1">resource' in html


def test_resource_list(client):
    login(client)
    html = client.get("/resources").text
    assert 'href="/networks/n1/resources/r1">nas</a>' in html
    assert 'href="/policies/p1"' in html  # p1 reaches r1 through Servers
    assert "/matrix?view=resources-peers&amp;cq=nas" in html
    rows = client.get("/resources?unreached=1", headers={"HX-Target": "resource-rows"}).text
    assert "<html" not in rows and "No resource found" in rows
