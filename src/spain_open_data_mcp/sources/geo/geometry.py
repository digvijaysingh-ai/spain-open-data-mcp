"""Census tract shapes, for catchments: which tracts a circle touches, and how much of each.

The file (built by scripts/build_geometry.py, published on the geometry-v1 release) holds
each tract's bounding box as indexed columns and its shape as WKB. A catchment query reads
only the tracts whose box touches the circle (usually a few dozen), so memory stays flat
however large the country is. Shapes are kept in a small cache between calls.

Areas and overlaps are measured in a local equirectangular projection around the circle's
centre (metres east and north). Over a few kilometres its error is far below the 10 m the
shapes are simplified to.
"""

from __future__ import annotations

import math
import sqlite3
from collections import OrderedDict
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

from ...config import PROJECT_URL
from ...core.assets import AssetCorrupt, AssetSpec, ReleaseStore

SCHEMA_VERSION = 1
DB_NAME = "tracts_geometry.sqlite"
MANIFEST_NAME = "geometry_manifest.json"
RELEASE_TAG = f"geometry-v{SCHEMA_VERSION}"
RELEASE_URL = f"{PROJECT_URL}/releases/download/{RELEASE_TAG}/"
SCHEMA_SQL = """
CREATE TABLE tract (code TEXT PRIMARY KEY, minx REAL NOT NULL, miny REAL NOT NULL, maxx REAL NOT NULL,
                    maxy REAL NOT NULL, area_km2 REAL NOT NULL, wkb BLOB NOT NULL) WITHOUT ROWID;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
"""
INDEX_SQL = """
CREATE INDEX tract_box ON tract (minx, maxx, miny, maxy);
ANALYZE;
"""
EARTH_M_PER_DEG_LAT = 110_574.0
CACHE_SHAPES = 2_000
MIN_RADIUS_M, MAX_RADIUS_M = 100, 10_000


@dataclass(frozen=True)
class TractShare:
    code: str
    share: float  # share of the tract's area inside the circle, 0-1
    area_km2: float


class Geometry:
    def __init__(self, path: Path, *, stale_reason: str | None = None) -> None:
        self.path = path
        self.stale_reason = stale_reason
        self._db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        self._shapes: OrderedDict[str, Any] = OrderedDict()
        try:
            if self._db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise AssetCorrupt("integrity check failed")
            if "tracts_year" not in self.meta:
                raise AssetCorrupt("metadata missing: tracts_year")
        except sqlite3.DatabaseError as e:
            self._db.close()
            raise AssetCorrupt(str(e)) from e
        except AssetCorrupt:
            self._db.close()
            raise

    @cached_property
    def meta(self) -> dict[str, str]:
        return dict(self._db.execute("SELECT key, value FROM meta").fetchall())

    @property
    def tracts_year(self) -> int:
        return int(self.meta["tracts_year"])

    def _shape(self, code: str, blob: bytes) -> Any:
        from shapely import wkb  # imported here: most calls never touch geometry

        if code in self._shapes:
            self._shapes.move_to_end(code)
            return self._shapes[code]
        shape = wkb.loads(blob)
        self._shapes[code] = shape
        if len(self._shapes) > CACHE_SHAPES:
            self._shapes.popitem(last=False)
        return shape

    def circle_shares(self, lon: float, lat: float, radius_m: float) -> list[TractShare]:
        """Tracts touched by the circle, with the share of each tract's area inside it."""
        from shapely.geometry import Point

        return self._shares(lon, lat, radius_m, Point(0.0, 0.0).buffer(radius_m, quad_segs=32))

    def area_shares(self, shape_wgs84: Any, lon: float, lat: float, reach_m: float) -> list[TractShare]:
        """Tracts touched by any area (e.g. a walking area), centred near (lon, lat)."""
        import shapely

        m_lon = EARTH_M_PER_DEG_LAT * math.cos(math.radians(lat))

        def local(xy: Any) -> Any:
            out = xy.copy()
            out[:, 0] = (xy[:, 0] - lon) * m_lon
            out[:, 1] = (xy[:, 1] - lat) * EARTH_M_PER_DEG_LAT
            return out

        return self._shares(lon, lat, reach_m, shapely.transform(shape_wgs84, local))

    def _shares(self, lon: float, lat: float, reach_m: float, area_local: Any) -> list[TractShare]:
        import shapely

        m_per_deg_lon = EARTH_M_PER_DEG_LAT * math.cos(math.radians(lat))
        dlon, dlat = reach_m / m_per_deg_lon, reach_m / EARTH_M_PER_DEG_LAT
        rows = self._db.execute(
            "SELECT code, area_km2, wkb FROM tract WHERE minx <= ? AND maxx >= ? AND miny <= ? AND maxy >= ?",
            (lon + dlon, lon - dlon, lat + dlat, lat - dlat),
        ).fetchall()

        def local(xy: Any) -> Any:
            out = xy.copy()
            out[:, 0] = (xy[:, 0] - lon) * m_per_deg_lon
            out[:, 1] = (xy[:, 1] - lat) * EARTH_M_PER_DEG_LAT
            return out

        shares = []
        for code, area_km2, blob in rows:
            shape = shapely.transform(self._shape(code, blob), local)
            if shape.area <= 0 or not shape.intersects(area_local):
                continue
            share = shape.intersection(area_local).area / shape.area
            if share > 0.001:
                shares.append(TractShare(code, min(share, 1.0), area_km2))
        return sorted(shares, key=lambda t: -t.share)

    def tract_at(self, lon: float, lat: float) -> str | None:
        """The tract containing a point, or None (sea, abroad)."""
        from shapely.geometry import Point

        p = Point(lon, lat)
        for code, blob in self._db.execute(
            "SELECT code, wkb FROM tract WHERE minx <= ? AND maxx >= ? AND miny <= ? AND maxy >= ?",
            (lon, lon, lat, lat),
        ).fetchall():
            if self._shape(code, blob).covers(p):
                return code
        return None

    def close(self) -> None:
        self._db.close()


SPEC = AssetSpec(
    label="census tract geometry",
    db_name=DB_NAME,
    manifest_name=MANIFEST_NAME,
    schema_version=SCHEMA_VERSION,
    release_url=RELEASE_URL,
    env_var="SPAIN_MCP_GEOMETRY_DB",
    subdir="geometry",
    build_script="scripts/build_geometry.py",
)


class GeometryStore(ReleaseStore[Geometry]):
    def __init__(self, directory: Path | None = None, **kwargs: Any) -> None:
        super().__init__(SPEC, lambda path, stale: Geometry(path, stale_reason=stale), directory, **kwargs)
