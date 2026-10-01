"""Non-admin roles: auditors read everything but write nothing, regular users
only see their own devices (if NetBird lets them see anything)."""

import pytest
from fastapi.testclient import TestClient

from birdseye_web.app import create_app
from tests.fakes import ME, SETTINGS, FakeNetBird, FakeOIDC
from tests.test_routes import login

MODULES = ("peers", "groups", "policies", "networks", "users", "setup_keys", "events")


def _perms(read: bool) -> dict:
    ops = {"create": False, "update": False, "delete": False, "read": read}
    return {"modules": {m: dict(ops) for m in MODULES}}


AUDITOR = {**ME, "role": "auditor", "permissions": _perms(True)}
BOB = {"id": "u2", "name": "Bob", "role": "user", "permissions": _perms(False)}


@pytest.fixture
def nb():
    return FakeNetBird()


def _client(nb, me):
    nb.data["users/current"] = me
    oidc = FakeOIDC()
    c = TestClient(create_app(SETTINGS, oidc=oidc, api_factory=nb.api), follow_redirects=False)
    c.oidc = oidc
    login(c)
    return c


# --- auditor: read-only editors ----------------------------------------------


def test_auditor_group_editor_is_read_only(nb):
    html = _client(nb, AUDITOR).get("/groups/A").text
    assert ">Save<" not in html and ">Delete<" not in html
    assert "read-only" in html and 'name="peers" value="a1" checked disabled' in html


def test_auditor_policy_editor_and_list_are_read_only(nb):
    c = _client(nb, AUDITOR)
    html = c.get("/policies/p1").text
    assert ">Save<" not in html and ">Delete<" not in html and "read-only" in html
    assert 'hx-post="/policies/preview"' not in html  # a disabled form sends nothing
    assert "data-remove-rule" not in html
    rows = c.get("/policies").text
    assert "/policies/p1/toggle" not in rows


def test_auditor_keeps_full_navigation(nb):
    html = _client(nb, AUDITOR).get("/matrix").text
    assert 'href="/groups"' in html and 'href="/policies"' in html
    assert "only lets you see" not in html


# --- regular user: own devices only ------------------------------------------


def test_user_lands_on_my_access_and_sees_limited_nav(nb):
    c = _client(nb, BOB)
    r = c.get("/matrix")
    assert r.status_code == 303 and r.headers["location"] == "/my-access"
    html = c.get("/my-access").text
    assert 'href="/groups"' not in html and 'href="/policies"' not in html
    assert 'href="/my-access"' in html


def test_user_my_access_lists_accessible_peers(nb):
    nb.data["peers/c1/accessible-peers"] = [
        {"id": "a1", "name": "a1", "ip": "100.64.0.1", "connected": True},
        {"id": "b1", "name": "b1", "ip": "100.64.0.2", "connected": False},
    ]
    html = _client(nb, BOB).get("/my-access").text
    assert "c1" in html and "a1" in html and "b1" in html and "2 devices" in html


def test_user_without_visible_devices_gets_explanation(nb):
    nb.data["peers"] = []  # NetBird default: regular users see nothing
    html = _client(nb, BOB).get("/my-access").text
    assert "regular users" in html.lower() and "admin" in html.lower()


def test_user_gets_no_anomalies_from_partial_data(nb):
    html = _client(nb, BOB).get("/anomalies").text
    assert "User without any device" not in html and "only lets you see" in html


# --- admin: my access with services -----------------------------------------


def test_admin_my_access_shows_services(nb):
    html = _client(nb, ME).get("/my-access").text  # u1 owns a1: Admins -> Servers tcp/22
    assert "a1" in html and "tcp/22" in html
