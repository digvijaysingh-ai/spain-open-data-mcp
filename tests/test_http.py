from pathlib import Path

import httpx
import pytest

from spain_open_data_mcp.core import http as http_mod
from spain_open_data_mcp.core.errors import UpstreamError
from spain_open_data_mcp.core.http import HostPolicy, HttpClient

URL = "https://example.test/data"


@pytest.fixture(autouse=True)
def fast_policy(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(
        http_mod.POLICIES, "example.test", HostPolicy(min_interval_s=0, max_attempts=2, max_pending_wait_s=5)
    )

    async def no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(http_mod.asyncio, "sleep", no_sleep)


def client(tmp_path: Path, handler, offline: bool = False) -> HttpClient:
    return HttpClient(tmp_path / "c.sqlite", transport=httpx.MockTransport(handler), offline=offline)


async def test_cache_keeps_original_fetch_time(tmp_path: Path):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, json={"ok": 1})

    c = client(tmp_path, handler)
    first = await c.get_json(URL, {"a": 1})
    second = await c.get_json(URL, {"a": 1})
    assert len(calls) == 1
    assert second.from_cache and not first.from_cache
    assert second.fetched_at == first.fetched_at
    await c.aclose()


async def test_polls_while_ine_says_request_in_process(tmp_path: Path):
    responses = iter(
        [httpx.Response(202, json={"status": "Petición en proceso"}), httpx.Response(200, json=[1, 2])]
    )
    c = client(tmp_path, lambda _req: next(responses))
    assert (await c.get_json(URL)).data == [1, 2]
    await c.aclose()


async def test_serves_stale_cache_when_upstream_fails(tmp_path: Path):
    ok = client(tmp_path, lambda _r: httpx.Response(200, json={"v": 1}))
    await ok.get_json(URL, ttl_s=0)
    await ok.aclose()
    down = client(tmp_path, lambda _r: httpx.Response(503))
    got = await down.get_json(URL, ttl_s=0)
    assert got.stale and got.data == {"v": 1}
    await down.aclose()


async def test_error_says_problem_cause_fix(tmp_path: Path):
    c = client(tmp_path, lambda _r: httpx.Response(404))
    with pytest.raises(UpstreamError) as e:
        await c.get_json(URL, source_name="INE table 1")
    text = str(e.value)
    assert "Problem:" in text and "Cause:" in text and "HTTP 404" in text and "Fix:" in text
    await c.aclose()


async def test_offline_mode_never_hits_network(tmp_path: Path):
    def handler(_r: httpx.Request) -> httpx.Response:
        raise AssertionError("network used")

    c = client(tmp_path, handler, offline=True)
    with pytest.raises(UpstreamError, match="offline"):
        await c.get_json(URL)
    await c.aclose()


def test_cache_key_is_order_independent():
    assert HttpClient.cache_key(URL, [("b", 2), ("a", 1)]) == HttpClient.cache_key(URL, {"a": 1, "b": 2})
