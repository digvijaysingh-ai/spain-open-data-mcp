"""The hosted HTTP app: health check, host checks, rate limits, and the HTTP cache cap."""

from pathlib import Path

import httpx
import pytest

from spain_open_data_mcp import hosting
from spain_open_data_mcp.core.http import _Cache
from spain_open_data_mcp.hosting import Bucket, RateLimit, client_address, is_shared

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "1"},
    },
}
HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def test_bucket_allows_bursts_then_waits():
    b = Bucket(rate=3)
    assert [b.take(0.0) for _ in range(3)] == [0.0, 0.0, 0.0]
    assert b.take(0.0) == pytest.approx(20.0)  # one token every 20 s
    assert b.take(20.0) == 0.0


def test_client_address_and_shared_networks():
    scope = {"headers": [(b"x-forwarded-for", b"6.6.6.6, 160.79.105.9")], "client": ("10.0.0.1", 1)}
    assert client_address(scope, 1) == "160.79.105.9"  # the entry the proxy added, not the spoofable first
    assert client_address(scope, 0) == "10.0.0.1"
    assert is_shared("160.79.105.9") and not is_shared("6.6.6.6") and not is_shared("garbage")


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SPAIN_MCP_ALLOWED_HOSTS", "mcp.example.test")

    def build(**limits):
        from spain_open_data_mcp.server import mcp

        inner = mcp.streamable_http_app(
            stateless_http=True, json_response=True, transport_security=hosting.transport_security()
        )
        return inner, RateLimit(inner, proxy_hops=0, **limits)

    return build


async def _client(asgi) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi), base_url="https://mcp.example.test")


async def test_health_and_mcp_initialize(app):
    inner, outer = app()
    async with inner.router.lifespan_context(inner), await _client(outer) as c:
        assert (await c.get("/health")).text == "ok"
        r = await c.post("/mcp", json=INIT, headers=HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["result"]["serverInfo"]["name"] == "spain-open-data"


async def test_unknown_host_and_origin_are_refused(app):
    inner, outer = app()
    async with inner.router.lifespan_context(inner), await _client(outer) as c:
        r = await c.post("/mcp", json=INIT, headers={**HEADERS, "host": "evil.test"})
        assert r.status_code == 421
        r = await c.post("/mcp", json=INIT, headers={**HEADERS, "origin": "https://evil.test"})
        assert r.status_code == 403


async def test_rate_limit_returns_429(app):
    inner, outer = app(per_min=2)
    async with inner.router.lifespan_context(inner), await _client(outer) as c:
        codes = [(await c.post("/mcp", json=INIT, headers=HEADERS)).status_code for _ in range(3)]
        assert codes == [200, 200, 429]
        r = await c.post("/mcp", json=INIT, headers=HEADERS)
        assert int(r.headers["retry-after"]) >= 1
        assert (await c.get("/health")).status_code == 200  # never limited


def test_http_cache_drops_oldest_past_its_cap(tmp_path: Path):
    cache = _Cache(tmp_path / "c.sqlite", max_bytes=1000)
    for i in range(5):
        cache.put(f"k{i}", float(i), b"x" * 300)
    assert cache.get("k0") is None and cache.get("k4") is not None
    assert cache._size <= 1000
    reopened = _Cache(tmp_path / "c.sqlite", max_bytes=1000)
    assert reopened._size == cache._size
