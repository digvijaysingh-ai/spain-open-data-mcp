"""Serve recorded INE fixtures through an httpx mock transport."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx

FIXTURES = Path(__file__).parent / "ine"


def fixture_name(table_id: int, params: list[tuple[str, Any]]) -> str:
    parts = [
        f"{k}{v}".replace(":", "-") for k, v in sorted((str(k), str(v)) for k, v in params) if k != "tip"
    ]
    return f"t{table_id}_{'_'.join(parts)}.json"


class IneFixtures:
    """httpx transport that answers DATOS_TABLA requests from tests/fixtures/ine/."""

    def __init__(self) -> None:
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        path = request.url.path
        if "/DATOS_TABLA/" not in path:
            return httpx.Response(404, text="no fixture")
        table_id = int(path.rsplit("/", 1)[1])
        params = parse_qsl(request.url.query.decode())
        f = FIXTURES / fixture_name(table_id, params)
        if not f.exists():
            return httpx.Response(404, text=f"missing fixture {f.name}")
        return httpx.Response(200, content=f.read_bytes(), headers={"Content-Type": "application/json"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))
