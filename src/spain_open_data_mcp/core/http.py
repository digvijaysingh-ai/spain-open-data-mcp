"""Polite, cached HTTP for upstream APIs.

- On-disk SQLite cache keyed by URL. The original fetch time is kept, so every
  figure can report when it was actually retrieved, even when served from cache.
- Per-host concurrency and minimum spacing between requests.
- Retries with backoff on timeouts, 429 and 5xx; honours Retry-After.
- If upstream is down and an expired cache entry exists, it is served and marked stale.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from ..config import settings
from .errors import UpstreamError

DAY = 24 * 3600


@dataclass(frozen=True)
class HostPolicy:
    max_concurrency: int = 2
    min_interval_s: float = 0.25
    max_attempts: int = 3
    max_pending_wait_s: float = 90.0  # total time to poll an HTTP 202 'still processing'


POLICIES: dict[str, HostPolicy] = {
    "servicios.ine.es": HostPolicy(max_concurrency=3, min_interval_s=0.2),
    "www.ine.es": HostPolicy(max_concurrency=2, min_interval_s=0.5),
    "datos.gob.es": HostPolicy(max_concurrency=2, min_interval_s=0.3),
}


@dataclass(frozen=True)
class Fetched:
    data: Any
    fetched_at: datetime
    from_cache: bool
    stale: bool = False


class _Cache:
    def __init__(self, path: Path, max_bytes: int = 0) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS http (key TEXT PRIMARY KEY, fetched_at REAL NOT NULL, body BLOB NOT NULL)"
        )
        self._db.commit()
        self._max = max_bytes
        self._size = self._db.execute("SELECT coalesce(sum(length(body)), 0) FROM http").fetchone()[0]

    def get(self, key: str) -> tuple[float, bytes] | None:
        row = self._db.execute("SELECT fetched_at, body FROM http WHERE key = ?", (key,)).fetchone()
        return (row[0], row[1]) if row else None

    def put(self, key: str, fetched_at: float, body: bytes) -> None:
        old = self._db.execute("SELECT length(body) FROM http WHERE key = ?", (key,)).fetchone()
        self._db.execute(
            "INSERT OR REPLACE INTO http (key, fetched_at, body) VALUES (?, ?, ?)", (key, fetched_at, body)
        )
        self._size += len(body) - (old[0] if old else 0)
        if self._max and self._size > self._max:
            self._evict(int(self._max * 0.8))
        self._db.commit()

    def _evict(self, target: int) -> None:
        """Drop the oldest responses until the cache holds at most `target` bytes."""
        rows = self._db.execute("SELECT key, length(body) FROM http ORDER BY fetched_at").fetchall()
        drop = []
        for key, size in rows:
            if self._size <= target:
                break
            drop.append((key,))
            self._size -= size
        self._db.executemany("DELETE FROM http WHERE key = ?", drop)


class HttpClient:
    def __init__(
        self,
        cache_path: Path | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        offline: bool | None = None,
    ) -> None:
        self._cache = _Cache(
            cache_path or settings.cache_dir / "http.sqlite", settings.http_cache_mb * 1_000_000
        )
        self._offline = settings.offline if offline is None else offline
        self._client = httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
            timeout=httpx.Timeout(settings.http_timeout_s, connect=15),
            follow_redirects=True,
            transport=transport,
        )
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def cache_key(url: str, params: dict[str, Any] | list[tuple[str, Any]] | None) -> str:
        if not params:
            return url
        items = params.items() if isinstance(params, dict) else params
        return f"{url}?{urlencode(sorted((str(k), str(v)) for k, v in items))}"

    async def get_json(
        self,
        url: str,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        *,
        ttl_s: float = 7 * DAY,
        source_name: str = "upstream",
        accept: str | None = None,
    ) -> Fetched:
        key = self.cache_key(url, params)
        cached = self._cache.get(key)
        now = time.time()
        if cached and (now - cached[0] < ttl_s or self._offline):
            return Fetched(json.loads(cached[1]), _utc(cached[0]), from_cache=True)
        if self._offline:
            raise UpstreamError(
                problem=f"No cached data for {source_name} and the server is in offline mode.",
                cause=f"SPAIN_MCP_OFFLINE=1 and {key} is not in the cache.",
                fix="Unset SPAIN_MCP_OFFLINE or warm the cache by running the request once online.",
            )
        try:
            body = await self._fetch(url, params, source_name, accept)
        except UpstreamError:
            if cached:  # serve stale rather than fail
                return Fetched(json.loads(cached[1]), _utc(cached[0]), from_cache=True, stale=True)
            raise
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise UpstreamError(
                problem=f"{source_name} returned a response that is not JSON.",
                cause=f"Unexpected content from {url}: {body[:120]!r}",
                fix="The request parameters are probably invalid for this table; check codes and filters.",
            ) from e
        self._cache.put(key, now, body)
        return Fetched(data, _utc(now), from_cache=False)

    async def _fetch(self, url: str, params: Any, source_name: str, accept: str | None = None) -> bytes:
        host = urlsplit(url).hostname or ""
        policy = POLICIES.get(host, HostPolicy())
        sem = self._sems.setdefault(host, asyncio.Semaphore(policy.max_concurrency))
        lock = self._locks.setdefault(host, asyncio.Lock())
        last_error = ""
        attempt = 0
        pending_waited = 0.0
        while attempt < policy.max_attempts:
            status: int | None = None
            retry_after = ""
            async with sem:
                async with lock:  # space out request starts per host
                    wait = self._last.get(host, 0) + policy.min_interval_s - time.monotonic()
                    if wait > 0:
                        await asyncio.sleep(wait)
                    self._last[host] = time.monotonic()
                try:
                    r = await self._client.get(
                        url, params=params, headers={"Accept": accept} if accept else None
                    )
                except httpx.TimeoutException:
                    last_error = f"timed out after {settings.http_timeout_s:.0f} s"
                except httpx.HTTPError as e:
                    last_error = f"network error: {type(e).__name__}"
                else:
                    if r.status_code == 200:
                        return r.content
                    status = r.status_code
                    retry_after = r.headers.get("Retry-After", "")
            # Waits happen outside the semaphore so other requests to the host can proceed.
            if status == 202 and pending_waited < policy.max_pending_wait_s:
                # INE builds large tables in the background and answers 'Petición en proceso'.
                delay = min(3.0 + pending_waited / 2, 20.0)
                pending_waited += delay
                last_error = "kept answering 'request in process' (HTTP 202)"
                await asyncio.sleep(delay)
                continue
            if status is not None:
                last_error = f"HTTP {status}"
                if status not in (429, 500, 502, 503, 504):
                    break
            if retry_after.isdigit():
                await asyncio.sleep(min(int(retry_after), 60))
            else:
                await asyncio.sleep(1.5 * 2**attempt)
            attempt += 1
        raise UpstreamError(
            problem=f"Could not get data from {source_name}.",
            cause=f"{host} {last_error}.",
            fix="Try again in a minute. If it keeps failing, the upstream service may be down; "
            "check its status page.",
        )


def _utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=UTC)
