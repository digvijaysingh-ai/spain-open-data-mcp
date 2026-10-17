"""The hosted HTTP server: Streamable HTTP (stateless, JSON responses) behind host checks and
rate limits, plus a health check.

    request ─▶ RateLimit ─▶ /health        (200 "ok", never limited)
                         └▶ /mcp            (MCP; Host and Origin checked against settings)

Settings (environment):
- SPAIN_MCP_ALLOWED_HOSTS: comma-separated Host values the server answers, e.g.
  'spain-open-data-mcp-xyz.a.run.app,mcp.example.com'. Local hosts are always allowed.
- SPAIN_MCP_ALLOWED_ORIGINS: comma-separated browser origins allowed (none by default; MCP
  clients such as Claude call from servers, without an Origin header).
- SPAIN_MCP_RATE_PER_MIN (default 60): requests per minute per client address.
- SPAIN_MCP_SHARED_RATE_PER_MIN (default 600): per address in SHARED_NETWORKS. Claude's
  connectors reach remote MCP servers from 160.79.104.0/21, so one address there carries many
  people's requests.
- SPAIN_MCP_GLOBAL_RATE_PER_MIN (default 1200): all requests to one instance, to protect the
  upstream services (INE, CartoCiudad) from bursts.
- SPAIN_MCP_PROXY_HOPS (default 1): how many proxies sit in front (Cloud Run adds one). The client
  address is the entry that many places from the end of X-Forwarded-For.
"""

from __future__ import annotations

import ipaddress
import json
import os
import time
from collections import OrderedDict
from dataclasses import dataclass

from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

SHARED_NETWORKS = [ipaddress.ip_network("160.79.104.0/21")]
LOCAL_HOSTS = ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "[::1]", "[::1]:*"]
MAX_TRACKED = 10_000  # client addresses remembered per instance


def _env_list(name: str) -> list[str]:
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


def transport_security() -> TransportSecuritySettings:
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[*LOCAL_HOSTS, *_env_list("SPAIN_MCP_ALLOWED_HOSTS")],
        allowed_origins=_env_list("SPAIN_MCP_ALLOWED_ORIGINS"),
    )


@dataclass
class Bucket:
    """Token bucket: `rate` requests per minute, bursts up to `rate`."""

    rate: float
    tokens: float = -1.0  # full on first use
    updated: float = 0.0

    def take(self, now: float) -> float:
        """0 if allowed, else seconds until a token is free."""
        if self.tokens < 0:
            self.tokens, self.updated = self.rate, now
        self.tokens = min(self.rate, self.tokens + (now - self.updated) * self.rate / 60)
        self.updated = now
        if self.tokens >= 1:
            self.tokens -= 1
            return 0.0
        return (1 - self.tokens) * 60 / self.rate


def client_address(scope: Scope, proxy_hops: int) -> str:
    if proxy_hops > 0:
        for name, value in scope.get("headers") or []:
            if name == b"x-forwarded-for":
                hops = [h.strip() for h in value.decode("latin-1").split(",") if h.strip()]
                if hops:
                    return hops[-min(proxy_hops, len(hops))]
    client = scope.get("client")
    return client[0] if client else "unknown"


def is_shared(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in net for net in SHARED_NETWORKS)


class RateLimit:
    """ASGI middleware: per-address and per-instance request limits, and GET /health."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        per_min: float | None = None,
        shared_per_min: float | None = None,
        global_per_min: float | None = None,
        proxy_hops: int | None = None,
    ) -> None:
        self.app = app
        self.per_min = per_min or float(os.environ.get("SPAIN_MCP_RATE_PER_MIN", "60"))
        self.shared_per_min = shared_per_min or float(os.environ.get("SPAIN_MCP_SHARED_RATE_PER_MIN", "600"))
        self.proxy_hops = (
            proxy_hops if proxy_hops is not None else int(os.environ.get("SPAIN_MCP_PROXY_HOPS", "1"))
        )
        self.all = Bucket(global_per_min or float(os.environ.get("SPAIN_MCP_GLOBAL_RATE_PER_MIN", "1200")))
        self.buckets: OrderedDict[str, Bucket] = OrderedDict()

    def _bucket(self, address: str) -> Bucket:
        b = self.buckets.pop(address, None) or Bucket(
            self.shared_per_min if is_shared(address) else self.per_min
        )
        self.buckets[address] = b  # most recent last
        while len(self.buckets) > MAX_TRACKED:
            self.buckets.popitem(last=False)
        return b

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["path"] == "/health":
            await _respond(send, 200, b"ok", "text/plain")
            return
        now = time.monotonic()
        wait = self._bucket(client_address(scope, self.proxy_hops)).take(now) or self.all.take(now)
        if wait:
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32000, "message": f"Too many requests; retry in {wait:.0f} s."},
                }
            ).encode()
            await _respond(
                send, 429, body, "application/json", [(b"retry-after", str(max(1, round(wait))).encode())]
            )
            return
        await self.app(scope, receive, send)


async def _respond(
    send: Send, status: int, body: bytes, content_type: str, extra: list[tuple[bytes, bytes]] | None = None
) -> None:
    headers = [
        (b"content-type", content_type.encode()),
        (b"content-length", str(len(body)).encode()),
        *(extra or []),
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def build_app() -> ASGIApp:
    from .server import mcp

    app = mcp.streamable_http_app(
        stateless_http=True, json_response=True, transport_security=transport_security()
    )
    return RateLimit(app)


def serve(host: str, port: int) -> None:
    import uvicorn

    # Cloud Run sends SIGTERM and allows 10 s to finish; requests here are short.
    uvicorn.run(
        build_app(),
        host=host,
        port=port,
        proxy_headers=False,
        timeout_graceful_shutdown=8,
        log_level="warning",
    )
