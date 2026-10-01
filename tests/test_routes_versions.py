import json

import pytest
from fastapi.testclient import TestClient

from birdseye_web import versions
from birdseye_web.app import create_app
from tests.fakes import SETTINGS, FakeNetBird, FakeOIDC
from tests.test_routes import login


@pytest.fixture
def nb():
    n = FakeNetBird()
    n.data["instance/version"] = {
        "management_current_version": "0.80.0",
        "management_available_version": "0.81.0",
        "management_update_available": True,
    }
    return n


def _client(nb, **settings):
    versions.reset_cache()
    oidc = FakeOIDC()
    s = SETTINGS.__class__(**{**SETTINGS.__dict__, **settings})
    c = TestClient(create_app(s, oidc=oidc, api_factory=nb.api), follow_redirects=False)
    c.oidc = oidc
    return c


def test_footer_loads_versions(nb, tmp_path, monkeypatch):
    monkeypatch.setenv("BIRDSEYE_VERSION", "0.9.0")
    monkeypatch.setenv("BIRDSEYE_REVISION", "abcdef1234567")
    (tmp_path / "registry.json").write_text(json.dumps({"version": "0.8.9", "jobs": []}))
    c = _client(nb, jobs_dir=str(tmp_path))
    login(c)
    assert 'hx-get="/versions"' in c.get("/matrix").text
    html = c.get("/versions").text
    assert "birdseye-web 0.9.0" in html and "abcdef1" in html and "abcdef12" not in html
    assert "birdseye 0.8.9" in html
    assert "NetBird 0.80.0" in html and "0.81.0 available" in html


def test_versions_tolerate_missing_sources(nb, monkeypatch):
    monkeypatch.delenv("BIRDSEYE_VERSION", raising=False)
    del nb.data["instance/version"]  # older NetBird or no permission: 404
    c = _client(nb)
    login(c)
    html = c.get("/versions").text
    assert "birdseye-web dev" in html and "NetBird" not in html and "birdseye 0" not in html


def test_netbird_version_is_cached(nb):
    c = _client(nb)
    login(c)
    c.get("/versions")
    nb.data["instance/version"]["management_current_version"] = "0.99.0"
    assert "0.80.0" in c.get("/versions").text
