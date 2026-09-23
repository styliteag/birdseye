import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from birdseye_web.config import ConfigError, load_settings
from birdseye_web.oidc import OIDCClient, OIDCError, pkce_pair

ISS = "https://nb.test/oauth2"
DISCOVERY = {
    "authorization_endpoint": f"{ISS}/auth",
    "token_endpoint": f"{ISS}/token",
    "end_session_endpoint": f"{ISS}/logout",
}


def test_pkce_pair_is_s256():
    verifier, challenge = pkce_pair()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert challenge == expected.decode() and len(verifier) >= 43


@pytest.fixture
def oidc():
    return OIDCClient(ISS, "netbird-dashboard", "https://app.test/auth/callback")


@respx.mock
async def test_authorization_url(oidc):
    respx.get(f"{ISS}/.well-known/openid-configuration").mock(
        return_value=httpx.Response(200, json=DISCOVERY)
    )
    url = await oidc.authorization_url("st", "ch")
    q = parse_qs(urlparse(url).query)
    assert url.startswith(f"{ISS}/auth?")
    assert q["client_id"] == ["netbird-dashboard"]
    assert q["code_challenge_method"] == ["S256"] and q["state"] == ["st"]
    assert q["redirect_uri"] == ["https://app.test/auth/callback"]


@respx.mock
async def test_exchange_sends_verifier(oidc):
    respx.get(f"{ISS}/.well-known/openid-configuration").mock(
        return_value=httpx.Response(200, json=DISCOVERY)
    )
    route = respx.post(f"{ISS}/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "a", "refresh_token": "r", "expires_in": 60}
        )
    )
    t = await oidc.exchange("code", "verif")
    body = parse_qs(route.calls.last.request.content.decode())
    assert body["code_verifier"] == ["verif"] and body["grant_type"] == ["authorization_code"]
    assert t.access_token == "a" and t.refresh_token == "r"


@respx.mock
@pytest.mark.parametrize(
    "response",
    [httpx.Response(400, json={"error": "invalid_grant"}), httpx.Response(200, json={})],
)
async def test_token_errors(oidc, response):
    respx.get(f"{ISS}/.well-known/openid-configuration").mock(
        return_value=httpx.Response(200, json=DISCOVERY)
    )
    respx.post(f"{ISS}/token").mock(return_value=response)
    with pytest.raises(OIDCError):
        await oidc.refresh("r")


@respx.mock
async def test_token_timeout_is_oidc_error(oidc):
    respx.get(f"{ISS}/.well-known/openid-configuration").mock(
        return_value=httpx.Response(200, json=DISCOVERY)
    )
    respx.post(f"{ISS}/token").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(OIDCError, match="unreachable"):
        await oidc.refresh("r")


@respx.mock
async def test_discovery_failure(oidc):
    respx.get(f"{ISS}/.well-known/openid-configuration").mock(return_value=httpx.Response(404))
    with pytest.raises(OIDCError):
        await oidc.authorization_url("s", "c")


GOOD = {
    "NB_URL": "https://nb.test/",
    "WEB_BASE_URL": "https://app.test",
    "WEB_SESSION_SECRET": "s" * 40,
}


def test_settings_defaults():
    s = load_settings(GOOD)
    assert s.nb_url == "https://nb.test"
    assert s.oidc_issuer == "https://nb.test/oauth2"
    assert s.oidc_client_id == "netbird-dashboard"
    assert s.redirect_uri == "https://app.test/auth/callback"
    assert s.secure_cookies


def test_settings_missing_lists_names():
    with pytest.raises(ConfigError, match="WEB_BASE_URL.*WEB_SESSION_SECRET"):
        load_settings({"NB_URL": "https://nb.test"})


@pytest.mark.parametrize(
    "override", [{"WEB_SESSION_SECRET": "short"}, {"WEB_REDIRECT_PATH": "callback"}]
)
def test_settings_rejects(override):
    with pytest.raises(ConfigError):
        load_settings({**GOOD, **override})


def test_local_redirect_path_root():
    s = load_settings({**GOOD, "WEB_BASE_URL": "http://localhost:53000", "WEB_REDIRECT_PATH": "/"})
    assert s.redirect_uri == "http://localhost:53000/" and not s.secure_cookies
