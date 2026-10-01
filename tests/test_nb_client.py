import pytest

import nb_client


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(nb_client, "load_dotenv", lambda: None)
    monkeypatch.setenv("NB_API_KEY", "t")


@pytest.mark.parametrize(
    "url, base",
    [
        ("https://nb.example.com", "https://nb.example.com/api"),
        ("nb.example.com", "https://nb.example.com/api"),
        ("http://netbird.localhost:58080/", "http://netbird.localhost:58080/api"),
    ],
)
def test_client_keeps_explicit_scheme(monkeypatch, url, base):
    monkeypatch.setenv("NB_URL", url)
    assert nb_client.client_from_env().base_url == base
