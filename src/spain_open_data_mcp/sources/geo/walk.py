"""Walking-time catchments: the area reachable on foot in N minutes along the street network.

Graphs are built per province by scripts/build_walk_graphs.py and published on the
walk-v<schema> release. The server downloads a province's graph (a few MB) the first time a
walking catchment needs it, checks it against the manifest's SHA-256, and keeps it.

    point ──▶ nearest street node (must be within 300 m) ──▶ shortest walking distances along
    streets up to speed × minutes ──▶ reached streets, buffered by 30 m ──▶ walking area

Where a province's graph is not available yet, the caller falls back to a circle of
speed × minutes / 1.3 (streets are rarely straight), labelled as an estimate.
"""

from __future__ import annotations

import asyncio
import hashlib
import heapq
import json
import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ...config import PROJECT_URL, settings
from ...core.errors import PlaceNotFound, UpstreamError

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MANIFEST_NAME = "walk_manifest.json"
RELEASE_TAG = f"walk-v{SCHEMA_VERSION}"
RELEASE_URL = f"{PROJECT_URL}/releases/download/{RELEASE_TAG}/"
WALK_M_PER_MIN = 75.0  # 4.5 km/h
DETOUR_FACTOR = 1.3  # straight-line fallback: street distance is about 1.3 x straight distance
SNAP_MAX_M = 300.0
BUFFER_M = 30.0
CHECK_EVERY_S = 24 * 3600
M_PER_DEG_LAT = 110_574.0


def file_name(province: str) -> str:
    return f"walk_{province}.npz"


@dataclass
class Graph:
    lon: Any  # numpy float32 arrays
    lat: Any
    indptr: Any
    indices: Any
    length: Any


@dataclass(frozen=True)
class WalkArea:
    shape: Any  # shapely polygon in WGS84
    reached_nodes: int
    street_km: float
    snap_m: float
    area_km2: float


class WalkGraphs:
    """Province walking graphs, fetched on demand. One per process."""

    def __init__(
        self,
        directory: Path | None = None,
        *,
        release_url: str = RELEASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        offline: bool | None = None,
    ) -> None:
        self.directory = directory or settings.cache_dir / "walk"
        self.release_url = release_url
        self.transport = transport
        self.offline = settings.offline if offline is None else offline
        self._manifest: dict[str, Any] | None = None
        self._checked_at = 0.0
        self._graphs: dict[str, Graph] = {}
        self._lock = asyncio.Lock()

    async def manifest(self) -> dict[str, Any] | None:
        """The published manifest, re-read at most daily; None if unavailable (no graphs yet)."""
        local = self.directory / MANIFEST_NAME
        if self._manifest and time.time() - self._checked_at < CHECK_EVERY_S:
            return self._manifest
        if not self.offline:
            try:
                async with self._client() as client:
                    r = await client.get(self.release_url + MANIFEST_NAME)
                    r.raise_for_status()
                    m = json.loads(r.text)
                if int(m.get("schema_version", 0)) == SCHEMA_VERSION:
                    self.directory.mkdir(parents=True, exist_ok=True)
                    local.write_text(json.dumps(m), encoding="utf-8")
                    self._manifest, self._checked_at = m, time.time()
                    return m
            except (httpx.HTTPError, ValueError) as e:
                log.warning("walk manifest unavailable: %s", e)
        if local.exists():
            self._manifest = json.loads(local.read_text(encoding="utf-8"))
            self._checked_at = time.time()
        return self._manifest

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
            timeout=httpx.Timeout(settings.http_timeout_s, read=300),
            transport=self.transport,
        )

    async def provinces_near(self, lon: float, lat: float, reach_m: float) -> list[str]:
        m = await self.manifest()
        if not m:
            return []
        dlon = reach_m / (M_PER_DEG_LAT * math.cos(math.radians(lat)))
        dlat = reach_m / M_PER_DEG_LAT
        out = []
        for prov, info in m.get("provinces", {}).items():
            x0, y0, x1, y1 = info["bbox"]
            if x0 <= lon + dlon and x1 >= lon - dlon and y0 <= lat + dlat and y1 >= lat - dlat:
                out.append(prov)
        return sorted(out)

    async def graph(self, province: str) -> Graph:
        async with self._lock:
            if province in self._graphs:
                return self._graphs[province]
            m = await self.manifest()
            info = (m or {}).get("provinces", {}).get(province)
            if info is None:
                raise UpstreamError(
                    problem=f"No walking graph for province {province}.",
                    cause="The walking graphs have not been published for it.",
                    fix="Use radius_m instead of walk_minutes.",
                )
            path = self.directory / info["file"]
            if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != info["sha256"]:
                if self.offline:
                    raise UpstreamError(
                        problem=f"The walking graph for province {province} is not on this machine.",
                        cause="Offline mode does not download it.",
                        fix="Unset SPAIN_MCP_OFFLINE once, or use radius_m.",
                    )
                self.directory.mkdir(parents=True, exist_ok=True)
                async with self._client() as client:
                    r = await client.get(self.release_url + info["file"])
                    r.raise_for_status()
                if hashlib.sha256(r.content).hexdigest() != info["sha256"]:
                    raise UpstreamError(
                        problem=f"The walking graph for province {province} failed its checksum.",
                        cause="The download was corrupted or the release changed mid-way.",
                        fix="Try again in a minute.",
                    )
                path.write_bytes(r.content)
            self._graphs[province] = await asyncio.to_thread(_load, path)
            return self._graphs[province]


def _load(path: Path) -> Graph:
    import numpy as np  # imported here: only walking catchments need it

    with np.load(path) as z:
        return Graph(z["lon"], z["lat"], z["indptr"], z["indices"], z["length"])


def walk_area(graphs: list[Graph], lon: float, lat: float, minutes: float) -> WalkArea:
    """Area reachable on foot from (lon, lat) in `minutes`, over the given graphs."""
    import numpy as np
    import shapely

    budget = minutes * WALK_M_PER_MIN
    m_lon = M_PER_DEG_LAT * math.cos(math.radians(lat))
    best: tuple[float, int, int] | None = None  # (distance m, graph index, node)
    for gi, g in enumerate(graphs):
        dx = (g.lon.astype(np.float64) - lon) * m_lon
        dy = (g.lat.astype(np.float64) - lat) * M_PER_DEG_LAT
        d2 = dx * dx + dy * dy
        i = int(np.argmin(d2))
        d = float(math.sqrt(d2[i]))
        if best is None or d < best[0]:
            best = (d, gi, i)
    if best is None or best[0] > SNAP_MAX_M:
        raise PlaceNotFound(
            problem="No walkable street near this point.",
            cause=f"The nearest mapped street is over {SNAP_MAX_M:.0f} m away.",
            fix="Check the address, or use radius_m.",
        )
    snap, gi, start = best
    g = graphs[gi]
    dist = {start: snap}
    heap = [(snap, start)]
    while heap:
        d, n = heapq.heappop(heap)
        if d > dist.get(n, math.inf):
            continue
        for k in range(int(g.indptr[n]), int(g.indptr[n + 1])):
            nb = int(g.indices[k])
            nd = d + float(g.length[k])
            if nd <= budget and nd < dist.get(nb, math.inf):
                dist[nb] = nd
                heapq.heappush(heap, (nd, nb))
    lines = []
    street_m = 0.0
    for n in dist:
        for k in range(int(g.indptr[n]), int(g.indptr[n + 1])):
            nb = int(g.indices[k])
            if nb in dist and n < nb:
                lines.append(((g.lon[n], g.lat[n]), (g.lon[nb], g.lat[nb])))
                street_m += float(g.length[k])
    if not lines:  # an isolated node: fall back to a small disc around it
        lines.append(((g.lon[start], g.lat[start]), (g.lon[start] + 1e-6, g.lat[start])))

    def to_local(xy: Any) -> Any:
        out = xy.copy()
        out[:, 0] = (xy[:, 0] - lon) * m_lon
        out[:, 1] = (xy[:, 1] - lat) * M_PER_DEG_LAT
        return out

    def to_wgs(xy: Any) -> Any:
        out = xy.copy()
        out[:, 0] = xy[:, 0] / m_lon + lon
        out[:, 1] = xy[:, 1] / M_PER_DEG_LAT + lat
        return out

    multi = shapely.transform(shapely.multilinestrings([list(seg) for seg in lines]), to_local)
    area_local = shapely.buffer(multi, BUFFER_M, quad_segs=4)
    return WalkArea(
        shapely.transform(area_local, to_wgs), len(dist), street_m / 1000, snap, area_local.area / 1e6
    )


def fallback_radius_m(minutes: float) -> float:
    """Straight-line radius roughly equal to `minutes` of walking along streets."""
    return minutes * WALK_M_PER_MIN / DETOUR_FACTOR
