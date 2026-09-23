import httpx
import pytest
import respx

from birdseye_web.nbapi import NetBirdAPI, NetBirdError
from birdseye_web.snapshot import SnapshotCache, load_snapshot
from tests.factory import group, peer, policy, rule

BASE = "https://nb.test"


@pytest.fixture
def api():
    return NetBirdAPI(BASE, "jwt-token")


@respx.mock
async def test_bearer_header_and_json(api):
    route = respx.get(f"{BASE}/api/groups").mock(return_value=httpx.Response(200, json=[]))
    assert await api.get("groups") == []
    assert route.calls.last.request.headers["Authorization"] == "Bearer jwt-token"


@respx.mock
async def test_error_carries_status_and_message(api):
    respx.put(f"{BASE}/api/groups/x").mock(
        return_value=httpx.Response(403, json={"message": "permission denied"})
    )
    with pytest.raises(NetBirdError) as exc:
        await api.put("groups/x", {"name": "x"})
    assert exc.value.forbidden and exc.value.message == "permission denied"


@respx.mock
async def test_delete_empty_body(api):
    respx.delete(f"{BASE}/api/groups/x").mock(return_value=httpx.Response(200))
    assert await api.delete("groups/x") is None


@respx.mock
async def test_load_snapshot_tolerates_forbidden_modules(api):
    respx.get(f"{BASE}/api/peers").mock(return_value=httpx.Response(200, json=[peer("a", ["A"])]))
    respx.get(f"{BASE}/api/groups").mock(
        return_value=httpx.Response(200, json=[group("A", peers=["a"])])
    )
    respx.get(f"{BASE}/api/users").mock(return_value=httpx.Response(403, json={}))
    respx.get(f"{BASE}/api/policies").mock(
        return_value=httpx.Response(200, json=[policy("p", rule(["A"], ["A"]))])
    )
    respx.get(f"{BASE}/api/posture-checks").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{BASE}/api/networks").mock(
        return_value=httpx.Response(200, json=[{"id": "n1", "name": "N"}])
    )
    respx.get(f"{BASE}/api/networks/n1/resources").mock(
        return_value=httpx.Response(
            200, json=[{"id": "r1", "name": "R", "type": "host", "address": "1.2.3.4"}]
        )
    )
    respx.get(f"{BASE}/api/networks/n1/routers").mock(return_value=httpx.Response(200, json=[]))
    snap = await load_snapshot(api)
    assert set(snap.peers) == {"a"} and snap.users == {}
    assert snap.resources["r1"].network_id == "n1"


@respx.mock
async def test_load_snapshot_raises_on_unauthorized(api):
    respx.get(url__startswith=f"{BASE}/api/").mock(return_value=httpx.Response(401, json={}))
    with pytest.raises(NetBirdError):
        await load_snapshot(api)


async def test_cache_ttl_and_invalidate():
    now = [0.0]
    cache = SnapshotCache(ttl=10, clock=lambda: now[0])
    calls = []

    async def loader():
        calls.append(1)
        from birdseye_web.models import Snapshot

        return Snapshot()

    await cache.get("u", loader)
    await cache.get("u", loader)
    assert len(calls) == 1
    now[0] = 11
    await cache.get("u", loader)
    assert len(calls) == 2
    cache.invalidate()
    await cache.get("u", loader)
    assert len(calls) == 3
