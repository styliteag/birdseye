import re

import pytest
from fastapi.testclient import TestClient

from birdseye_web.app import create_app
from tests.fakes import SETTINGS, FakeNetBird, FakeOIDC


@pytest.fixture
def nb():
    return FakeNetBird()


@pytest.fixture
def client(nb):
    oidc = FakeOIDC()
    app = create_app(SETTINGS, oidc=oidc, api_factory=nb.api)
    c = TestClient(app, follow_redirects=False)
    c.oidc = oidc
    return c


def login(client, next_url="/matrix"):
    r = client.get(f"/login?next={next_url}")
    assert r.status_code == 303 and r.headers["location"].startswith("https://nb.test/oauth2/")
    r = client.get(f"/auth/callback?code=good-code&state={client.oidc.last_state}")
    assert r.status_code == 303, r.text
    return r


def csrf(client, path="/groups/A"):
    html = client.get(path).text
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def test_unauthenticated_redirects_to_login(client):
    r = client.get("/matrix")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/matrix"


def test_htmx_unauthenticated_gets_hx_redirect(client):
    r = client.get("/matrix", headers={"HX-Request": "true"})
    assert r.headers["HX-Redirect"].startswith("/login")


def test_login_flow_sets_cookie_and_uses_bearer_token(client, nb):
    r = login(client)
    assert r.headers["location"] == "/matrix"
    assert "httponly" in r.headers["set-cookie"].lower()
    page = client.get("/matrix")
    assert page.status_code == 200 and "Alice" in page.text
    assert "access-jwt" in nb.tokens


def test_callback_rejects_wrong_state(client):
    client.get("/login")
    r = client.get("/auth/callback?code=good-code&state=forged")
    assert r.status_code == 400


def test_login_next_cannot_leave_site(client):
    client.get("/login?next=//evil.example/x")
    r = client.get(f"/auth/callback?code=good-code&state={client.oidc.last_state}")
    assert r.headers["location"] == "/"


def test_session_id_rotates_on_login(client):
    client.get("/login")
    before = client.cookies.get("birdseye_sid")
    client.get(f"/auth/callback?code=good-code&state={client.oidc.last_state}")
    assert client.cookies.get("birdseye_sid") != before


def test_matrix_renders_group_cell(client):
    login(client)
    html = client.get("/matrix?view=groups").text
    assert "Admins" in html and "Servers" in html and ">22<" in html


@pytest.mark.parametrize("view", ["peers", "resources", "resources-peers", "users"])
def test_every_view_renders(client, view):
    login(client)
    assert client.get(f"/matrix?view={view}").status_code == 200


def test_cell_detail_names_policy(client):
    login(client)
    r = client.get("/matrix/cell?row=g:A&col=g:B&view=groups")
    assert r.status_code == 200 and "p1" in r.text


def test_reach_query(client):
    login(client)
    yes = client.get("/reach?src=p:a1&dst=p:b1&proto=tcp&port=22")
    no = client.get("/reach?src=p:a1&dst=p:b1&proto=tcp&port=443")
    assert "can reach" in yes.text and "No policy allows" in no.text


def test_write_without_csrf_is_rejected(client, nb):
    login(client)
    r = client.post("/groups/A", data={"name": "Admins", "peers": ["a1"]})
    assert r.status_code == 403 and nb.writes == []


def test_group_update_preserves_resources(client, nb):
    login(client)
    token = csrf(client, "/groups/B")
    r = client.post("/groups/B", data={"csrf": token, "name": "Servers", "peers": ["b1", "a1"]})
    assert r.status_code == 303
    method, path, body = nb.writes[-1]
    assert (method, path) == ("PUT", "groups/B")
    assert body == {
        "name": "Servers",
        "peers": ["a1", "b1"],
        "resources": [{"id": "r1", "type": "host"}],
    }


def test_all_group_is_read_only(client, nb):
    login(client)
    token = csrf(client, "/groups/A")
    r = client.post("/groups/ALL", data={"csrf": token, "name": "All", "peers": []})
    assert r.status_code == 400 and nb.writes == []


def test_group_preview_shows_loss(client):
    login(client)
    token = csrf(client)
    r = client.post("/groups/A/preview", data={"peers": []}, headers={"X-CSRF-Token": token})
    assert "2 lost" in r.text  # a1->b1 and a1->r1 (resource in B)


def test_policy_create_uses_raw_payload(client, nb):
    login(client)
    token = csrf(client, "/policies/new")
    r = client.post(
        "/policies",
        data={
            "csrf": token,
            "name": "ssh",
            "enabled": "on",
            "rule_idx": "0",
            "rules-0-sources": "A",
            "rules-0-destinations": "B",
            "rules-0-protocol": "netbird-ssh",
            "rules-0-enabled": "on",
        },
    )
    assert r.status_code == 303
    method, path, body = nb.writes[-1]
    assert (method, path) == ("POST", "policies")
    assert body["rules"][0]["protocol"] == "netbird-ssh"
    assert body["rules"][0]["sources"] == ["A"]


def test_policy_invalid_input_rerenders_with_error(client, nb):
    login(client)
    token = csrf(client, "/policies/new")
    r = client.post(
        "/policies",
        data={
            "csrf": token,
            "name": "x",
            "rule_idx": "0",
            "rules-0-sources": "A",
            "rules-0-destinations": "B",
            "rules-0-protocol": "icmp",
            "rules-0-ports": "22",
        },
    )
    assert r.status_code == 400 and "ports only apply" in r.text and nb.writes == []


def test_policy_preview(client):
    login(client)
    token = csrf(client, "/policies/p1")
    r = client.post(
        "/policies/preview",
        headers={"X-CSRF-Token": token},
        data={
            "pid": "p1",
            "name": "p1",
            "enabled": "on",
            "rule_idx": "0",
            "rules-0-sources": "A",
            "rules-0-destinations": "B",
            "rules-0-protocol": "tcp",
            "rules-0-ports": "443",
            "rules-0-enabled": "on",
        },
    )
    assert "2 gained" in r.text and "2 lost" in r.text  # b1 and resource r1


def test_security_headers(client):
    r = client.get("/healthz")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]


def test_logout_drops_session(client):
    login(client)
    client.post("/logout", data={"csrf": csrf(client)})
    assert client.get("/matrix").status_code == 303


# --- security review fixes ------------------------------------------------------------


def test_login_next_length_capped(client):
    client.get("/login?next=/" + "a" * 5000)
    r = client.get(f"/auth/callback?code=good-code&state={client.oidc.last_state}")
    assert r.headers["location"] == "/"


def test_idp_error_needs_valid_state(client):
    client.get("/login")
    r = client.get("/auth/callback?error=x&error_description=Call+0800-SCAM&state=forged")
    assert "SCAM" not in r.text and r.status_code == 400


def test_login_rejected_without_user_id(client, nb):
    nb.data["users/current"] = {"name": "ghost", "role": "user"}
    client.get("/login")
    r = client.get(f"/auth/callback?code=good-code&state={client.oidc.last_state}")
    assert r.status_code == 403


def test_logout_requires_csrf(client):
    login(client)
    assert client.post("/logout").status_code == 403
    assert client.get("/matrix").status_code == 200


def test_logout_offers_idp_signout(client):
    login(client)
    token = csrf(client)
    r = client.post("/logout", data={"csrf": token})
    assert r.status_code == 200
    assert client.get("/matrix").status_code == 303


def test_non_ascii_csrf_is_403_not_500(client):
    login(client)
    r = client.post("/groups/A", data={"csrf": "äöü", "name": "x"})
    assert r.status_code == 403


def test_delete_preview_route_never_deletes(client, nb):
    login(client)
    token = csrf(client, "/policies/p1")
    r = client.post("/policies/p1/delete-preview", headers={"X-CSRF-Token": token})
    assert r.status_code == 200 and "lost" in r.text and nb.writes == []


@pytest.mark.parametrize("path", ["/groups/..", "/groups/a%3Fb", "/policies/a%23b"])
def test_bad_object_ids_rejected(client, nb, path):
    login(client)
    token = csrf(client)
    r = client.post(path, data={"csrf": token, "name": "x"})
    # 405: the client normalises `..` away, so no handler (and no API call) is reached
    assert r.status_code in (404, 405, 422) and nb.writes == []


def test_hsts_and_host_cookie_on_https(nb):
    from dataclasses import replace

    oidc = FakeOIDC()
    app = create_app(replace(SETTINGS, base_url="https://app.test"), oidc=oidc, api_factory=nb.api)
    c = TestClient(app, base_url="https://app.test", follow_redirects=False)
    r = c.get("/login")
    assert r.headers["set-cookie"].startswith("__Host-birdseye_sid=")
    assert "secure" in r.headers["set-cookie"].lower()
    assert "max-age=" in r.headers["Strict-Transport-Security"]


def test_audit_log_escapes_newlines(client, caplog):
    import logging

    login(client)
    token = csrf(client)
    with caplog.at_level(logging.INFO, logger="birdseye_web.audit"):
        client.post("/groups", data={"csrf": token, "name": "evil\nFAKE LINE", "peers": []})
    assert all("\nFAKE LINE" not in r.getMessage() for r in caplog.records)


def test_matrix_build_is_memoised():
    from birdseye_web.matrix import MatrixFilter
    from birdseye_web.routes_matrix import VIEWS, build
    from tests.factory import group, peer, policy, rule, snap

    s = snap(
        groups=[group("A", peers=["a"]), group("B", peers=["b"])],
        peers=[peer("a"), peer("b")],
        policies=[policy("p", rule(["A"], ["B"]))],
    )
    assert build(s, VIEWS[0], MatrixFilter()) is build(s, VIEWS[0], MatrixFilter())


def test_anomalies_page(client, nb):
    nb.data["policies"] = [
        *nb.data["policies"],
        {
            "id": "p9",
            "name": "direct",
            "enabled": True,
            "source_posture_checks": [],
            "rules": [
                {
                    "id": "r9",
                    "name": "direct",
                    "enabled": True,
                    "action": "accept",
                    "protocol": "all",
                    "sources": [{"id": "A"}],
                    "destinationResource": {"id": "b1", "type": "peer"},
                }
            ],
        },
    ]
    login(client)
    html = client.get("/anomalies").text
    assert "Rule targets a single peer" in html and "destination is peer b1" in html
    assert "direct" in client.get("/anomalies?min=warning").text
    assert "Unused group" not in client.get("/anomalies?min=warning").text


def test_group_update_assigns_user_auto_groups(client, nb):
    login(client)
    token = csrf(client, "/groups/A")
    data = {"csrf": token, "name": "Admins", "peers": ["a1"], "users_field": "1", "users": ["u2"]}
    r = client.post("/groups/A", data=data)
    assert r.status_code == 303, r.text
    puts = [(p, b) for m, p, b in nb.writes if m == "PUT"]
    assert [p for p, _ in puts] == ["groups/A", "users/u2"]  # group first, then users
    assert puts[1][1] == {"role": "user", "auto_groups": ["A"], "is_blocked": False}
    # u1 (admin, no auto groups) untouched; the service user keeps A although not submitted


def test_group_update_removes_user_auto_group(client, nb):
    nb.data["users"][1]["auto_groups"] = ["A", "B"]
    login(client)
    token = csrf(client, "/groups/A")
    data = {"csrf": token, "name": "Admins", "peers": ["a1"], "users_field": "1"}
    client.post("/groups/A", data={**data, "users_initial": ["u2"]})
    assert nb.writes[-1] == (
        "PUT",
        "users/u2",
        {"role": "user", "auto_groups": ["B"], "is_blocked": False},
    )


def test_group_update_without_users_field_leaves_users_alone(client, nb):
    nb.data["users"][1]["auto_groups"] = ["A"]
    login(client)
    token = csrf(client, "/groups/A")
    client.post("/groups/A", data={"csrf": token, "name": "Admins", "peers": ["a1"]})
    assert [p for _, p, _ in nb.writes] == ["groups/A"]


def test_group_editor_shows_users_only_with_permission(client, nb):
    login(client)
    assert 'name="users"' in client.get("/groups/A").text
    nb.data["users/current"] = {**nb.data["users/current"], "permissions": {"modules": {}}}
    client.cookies.clear()
    login(client)  # new session reads the new permissions
    assert 'name="users"' not in client.get("/groups/A").text


def test_group_preview_includes_user_propagation(client):
    login(client)
    token = csrf(client)
    r = client.post(
        "/groups/A/preview",
        data={"peers": ["a1"], "users_field": "1", "users": ["u2"]},
        headers={"X-CSRF-Token": token},
    )
    assert "2 gained" in r.text  # c1 (Bob's peer) -> b1 and -> r1


def test_group_create_assigns_users(client, nb):
    login(client)
    token = csrf(client, "/groups/new")
    data = {"csrf": token, "name": "New", "users_field": "1", "users": ["u2"]}
    r = client.post("/groups", data=data)
    assert r.status_code == 303
    method, path, body = nb.writes[-1]
    assert (method, path) == ("PUT", "users/u2") and body["auto_groups"] == ["new-1"]


def test_group_update_leaves_users_assigned_elsewhere_alone(client, nb):
    # Bob got group A after this form was rendered (not in users_initial).
    nb.data["users"][1]["auto_groups"] = ["A"]
    login(client)
    token = csrf(client, "/groups/A")
    data = {"csrf": token, "name": "Admins", "peers": ["a1"], "users_field": "1"}
    client.post("/groups/A", data=data)
    assert [p for _, p, _ in nb.writes] == ["groups/A"]


def test_group_editor_renders_initial_users(client, nb):
    nb.data["users"][1]["auto_groups"] = ["A"]
    login(client)
    assert 'name="users_initial" value="u2"' in client.get("/groups/A").text


@pytest.mark.parametrize(
    "accounts,text",
    [
        ([{"id": "acc", "settings": {"groups_propagation_enabled": True}}], "join or leave it now"),
        ([{"id": "acc", "settings": {"groups_propagation_enabled": False}}], "propagation is off"),
        ([], "Devices they add later join it."),
    ],
)
def test_group_editor_propagation_note(client, nb, accounts, text):
    nb.data["accounts"] = accounts
    login(client)
    assert text in client.get("/groups/A").text


def test_group_preview_without_propagation_shows_no_gain(client, nb):
    nb.data["accounts"][0]["settings"]["groups_propagation_enabled"] = False
    login(client)
    token = csrf(client)
    r = client.post(
        "/groups/A/preview",
        data={"peers": ["a1"], "users_field": "1", "users": ["u2"]},
        headers={"X-CSRF-Token": token},
    )
    assert "gained" not in r.text


def test_new_group_prefills_peer(client):
    login(client)
    html = client.get("/groups/new?peer=c1&peer=nope").text
    assert re.search(r'name="peers" value="c1" checked', html)
    assert 'value="nope"' not in html


def test_new_policy_prefills_resource(client, nb):
    nb.data["networks"] = [{"id": "n1", "name": "net"}]
    nb.data["networks/n1/resources"] = [
        {"id": "res1", "name": "nas", "address": "10.0.0.5/32", "type": "host"}
    ]
    nb.data["networks/n1/routers"] = []
    login(client)
    html = client.get("/policies/new?dst_resource=res1").text
    assert '<option value="host:res1" selected>' in html
    assert 'value="Access to nas"' in html
