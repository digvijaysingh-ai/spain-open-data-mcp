"""A synthetic walking graph: a 21 x 21 street grid, 100 m apart, centred on the mini snapshot."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import httpx
import numpy as np

from spain_open_data_mcp.sources.geo.walk import MANIFEST_NAME, SCHEMA_VERSION, file_name

CENTRE = (-4.7, 41.6)
STEP_M = 100.0
N = 21


def make_walk_graph(directory: Path, province: str = "47") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    m_lat = 110_574.0
    m_lon = m_lat * math.cos(math.radians(CENTRE[1]))
    half = N // 2
    lon = np.array(
        [CENTRE[0] + (i - half) * STEP_M / m_lon for j in range(N) for i in range(N)], dtype=np.float32
    )
    lat = np.array(
        [CENTRE[1] + (j - half) * STEP_M / m_lat for j in range(N) for i in range(N)], dtype=np.float32
    )
    adj: list[list[int]] = [[] for _ in range(N * N)]
    for j in range(N):
        for i in range(N):
            k = j * N + i
            if i + 1 < N:
                adj[k].append(k + 1)
                adj[k + 1].append(k)
            if j + 1 < N:
                adj[k].append(k + N)
                adj[k + N].append(k)
    indptr = np.cumsum([0] + [len(a) for a in adj]).astype(np.int32)
    indices = np.array([n for a in adj for n in a], dtype=np.int32)
    length = np.full(len(indices), STEP_M, dtype=np.float32)
    path = directory / file_name(province)
    np.savez_compressed(path, lon=lon, lat=lat, indptr=indptr, indices=indices, length=length)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "provinces": {
            province: {
                "file": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "bbox": [float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max())],
            }
        },
    }
    (directory / MANIFEST_NAME).write_text(json.dumps(manifest))
    return directory


def serve(directory: Path) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        p = directory / request.url.path.rsplit("/", 1)[1]
        return httpx.Response(200, content=p.read_bytes()) if p.exists() else httpx.Response(404)

    return httpx.MockTransport(handler)
