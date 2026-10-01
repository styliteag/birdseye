import pytest
from fastapi.testclient import TestClient

from birdseye_web.app import create_app
from birdseye_web.nbapi import NetBirdError
from tests.fakes import SETTINGS, FakeAPI, FakeNetBird, FakeOIDC
from tests.test_routes import login


def _ev(eid, code, target, ts, **extra):
    return {
        "id": str(eid),
        "timestamp": ts,
        "activity": code,
        "activity_code": code,
        "initiator_id": "u1",
        "initiator_name": "Alice",
        "target_id": target,
        "meta": {"name": "x"},
        **extra,
    }


@pytest.fixture
def nb():
    n = FakeNetBird()
    n.data["events/audit"] = [
        _ev(1, "group.update", "A", "2026-09-01T10:00:00Z"),
        _ev(2, "policy.update", "p1", "2026-09-02T10:00:00Z"),
        _ev(3, "group.delete", "zz", "2026-09-03T10:00:00Z", meta={"name": "Old <b>"}),
    ]
    return n


@pytest.fixture
def client(nb):
    oidc = FakeOIDC()
    c = TestClient(create_app(SETTINGS, oidc=oidc, api_factory=nb.api), follow_redirects=False)
    c.oidc = oidc
    return c


def test_audit_page_links_targets_newest_first(client):
    login(client)
    html = client.get("/audit").text
    assert 'href="/groups/A">Admins</a>' in html and 'href="/policies/p1"' in html
    assert "Old &lt;b&gt; (deleted)" in html  # escaped, no link
    body = html[html.index('id="audit-body"') :]
    assert body.index("group.delete") < body.index("policy.update") < body.index("group.update")


def test_audit_filter_by_target_partial(client):
    login(client)
    rows = client.get("/audit?target=A", headers={"HX-Target": "audit-body"}).text
    assert "<html" not in rows and "group.update" in rows and "policy.update" not in rows


def test_audit_forbidden_role(client, monkeypatch):
    async def deny(self, path):
        raise NetBirdError(403, "forbidden", path)

    login(client)
    monkeypatch.setattr(FakeAPI, "get", deny)
    r = client.get("/audit")
    assert r.status_code == 403 and "may not read the audit log" in r.text


def test_audit_paging(client, nb):
    nb.data["events/audit"] = [
        _ev(i, "group.update", "A", f"2026-09-01T10:{i // 60:02d}:{i % 60:02d}Z")
        for i in range(150)
    ]
    login(client)
    first = client.get("/audit").text
    assert "page 1 of 2" in first and "older" in first
    assert "page 2 of 2" in client.get("/audit?page=2").text


def test_history_links_on_editors(client):
    login(client)
    for path, oid in (
        ("/groups/A", "A"),
        ("/policies/p1", "p1"),
        ("/users/u2", "u2"),
        ("/peers/a1", "a1"),
    ):
        assert f'href="/audit?target={oid}"' in client.get(path).text


def test_changes_made_here_are_marked(client, nb):
    from datetime import UTC, datetime

    from birdseye_web.context import RECENT_WRITES, WriteRecord

    now = datetime.now(UTC)
    nb.data["events/audit"] = [_ev(9, "group.update", "A", now.strftime("%Y-%m-%dT%H:%M:%SZ"))]
    RECENT_WRITES.append(WriteRecord("u1", "update group A", now))
    try:
        login(client)
        assert 'title="Made through birdseye-web"' in client.get("/audit").text
    finally:
        RECENT_WRITES.clear()
