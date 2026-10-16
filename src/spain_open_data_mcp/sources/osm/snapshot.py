"""The OpenStreetMap business snapshot: where it lives, how it is fetched, how it is queried.

The snapshot is a SQLite file built monthly by scripts/build_osm_snapshot.py and
published as a GitHub release asset. Each place is stored with its census tract, so a
count for any municipality, district or tract is an indexed range scan on the code.

Lookup order:
1. SPAIN_MCP_OSM_DB, a path to the .sqlite file (the hosted server bakes it into the image).
2. <cache dir>/osm/osm_businesses.sqlite, downloaded on first use and checked against the
   published manifest's SHA-256. The manifest is re-read at most once a day; a newer
   snapshot replaces the local one. If the download fails, the local copy is used and
   reported as stale. A local copy that fails its integrity check is deleted and
   downloaded once more.

Each schema version has its own release (osm-snapshot-v1, -v2, ...), so a format change
leaves older installs on their last good snapshot instead of breaking them.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from pathlib import Path
from typing import Any

from ...categories import NOT_IN_TOTAL
from ...config import PROJECT_URL
from ...core.assets import AssetCorrupt, AssetSpec, ReleaseStore
from ...core.text import clean_text

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DB_NAME = "osm_businesses.sqlite"
MANIFEST_NAME = "osm_manifest.json"
RELEASE_TAG = f"osm-snapshot-v{SCHEMA_VERSION}"
RELEASE_URL = f"{PROJECT_URL}/releases/download/{RELEASE_TAG}/"
RELEASE_PAGE = f"{PROJECT_URL}/releases/tag/{RELEASE_TAG}"
CHECK_EVERY_S = 24 * 3600
MIN_BRAND_PLACES = 3

# One row per place, with the census tract it lies in. Only tags are kept, never names,
# phone numbers or addresses.
SCHEMA_SQL = """
CREATE TABLE poi (tract TEXT NOT NULL, category TEXT, tag TEXT NOT NULL, brand TEXT,
                  lon REAL NOT NULL, lat REAL NOT NULL, osm TEXT NOT NULL);
CREATE TABLE tract (code TEXT PRIMARY KEY, area_km2 REAL NOT NULL) WITHOUT ROWID;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
"""
INDEX_SQL = """
CREATE INDEX poi_category ON poi (category, tract);
CREATE INDEX poi_tag ON poi (tag, tract);
ANALYZE;
"""

LICENCE = "ODbL 1.0"
ATTRIBUTION = "© OpenStreetMap contributors, https://www.openstreetmap.org/copyright"


@dataclass(frozen=True)
class Area:
    """A set of census tracts: every tract whose code starts with one of `prefixes`.

    An empty tuple means all of Spain.
    """

    prefixes: tuple[str, ...]

    def where(self, column: str = "tract") -> tuple[str, list[str]]:
        if not self.prefixes:
            return "1", []
        # ':' sorts right after '9', so [p, p + ':') is every code starting with p.
        clause = " OR ".join(f"({column} >= ? AND {column} < ?)" for _ in self.prefixes)
        args = [x for p in self.prefixes for x in (p, p + ":")]
        return f"({clause})", args


@dataclass(frozen=True)
class Selection:
    """Which places to count: an SQL condition on the poi table and its parameters."""

    sql: str
    args: tuple[str, ...] = ()

    @classmethod
    def categories(cls, keys: Sequence[str]) -> Selection:
        return cls(f"category IN ({','.join('?' * len(keys))})", tuple(keys))

    @classmethod
    def tags(cls, tags: Sequence[str]) -> Selection:
        return cls(f"tag IN ({','.join('?' * len(tags))})", tuple(tags))

    @classmethod
    def all_businesses(cls) -> Selection:
        """Everything except categories that are mostly public services."""
        keys = sorted(NOT_IN_TOTAL)
        return cls(f"(category IS NULL OR category NOT IN ({','.join('?' * len(keys))}))", tuple(keys))


SnapshotCorrupt = AssetCorrupt  # the snapshot file is unreadable, truncated or not a snapshot


class Snapshot:
    """Read-only queries over one snapshot file. Raises SnapshotCorrupt if the file is damaged."""

    def __init__(self, path: Path, *, stale_reason: str | None = None) -> None:
        self.path = path
        self.stale_reason = stale_reason
        self._db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        try:
            ok = self._db.execute("PRAGMA quick_check").fetchone()[0]
            if ok != "ok":
                raise SnapshotCorrupt(f"integrity check failed: {ok}")
            missing = {"schema_version", "osm_timestamp", "tracts_year"} - set(self.meta)
            if missing:
                raise SnapshotCorrupt(f"metadata missing: {', '.join(sorted(missing))}")
        except sqlite3.DatabaseError as e:
            self._db.close()
            raise SnapshotCorrupt(str(e)) from e
        except SnapshotCorrupt:
            self._db.close()
            raise

    @cached_property
    def meta(self) -> dict[str, str]:
        return dict(self._db.execute("SELECT key, value FROM meta").fetchall())

    @property
    def osm_date(self) -> datetime:
        return datetime.fromisoformat(self.meta["osm_timestamp"])

    @property
    def tracts_year(self) -> int:
        return int(self.meta["tracts_year"])

    def _where(self, area: Area, sel: Selection | None) -> tuple[str, list[str]]:
        where, args = area.where()
        if sel is not None:
            where += f" AND {sel.sql}"
            args += list(sel.args)
        return where, args

    def count(self, area: Area, sel: Selection | None = None) -> int:
        where, args = self._where(area, sel)
        return self._db.execute(f"SELECT count(*) FROM poi WHERE {where}", args).fetchone()[0]

    def count_by_unit(self, area: Area, code_length: int, sel: Selection | None = None) -> dict[str, int]:
        """Counts grouped by the first `code_length` characters of the tract code."""
        where, args = self._where(area, sel)
        rows = self._db.execute(
            f"SELECT substr(tract, 1, {int(code_length)}) AS u, count(*) FROM poi WHERE {where} GROUP BY u",
            args,
        ).fetchall()
        return dict(rows)

    def count_by_category(self, area: Area) -> dict[str | None, int]:
        where, args = area.where()
        rows = self._db.execute(
            f"SELECT category, count(*) FROM poi WHERE {where} GROUP BY category", args
        ).fetchall()
        return dict(rows)

    def brands(self, area: Area, sel: Selection | None, limit: int = 10) -> tuple[list[tuple[str, int]], int]:
        """Most common brands and the number of branded places.

        Brands are free text anyone can edit, so only brands on MIN_BRAND_PLACES or more
        places in the area are listed (a one-off edit never reaches the model), and each is
        cleaned again on the way out.
        """
        where, args = self._where(area, sel)
        rows = self._db.execute(
            f"SELECT brand, count(*) AS n FROM poi WHERE {where} AND brand IS NOT NULL "
            "GROUP BY brand ORDER BY n DESC, brand",
            args,
        ).fetchall()
        listed = [(clean, n) for b, n in rows if n >= MIN_BRAND_PLACES and (clean := clean_text(b))]
        return listed[:limit], sum(n for _b, n in rows)

    def area_km2(self, area: Area) -> float:
        where, args = area.where("code")
        return self._db.execute(
            f"SELECT coalesce(sum(area_km2), 0) FROM tract WHERE {where}", args
        ).fetchone()[0]

    def area_km2_by_unit(self, area: Area, code_length: int) -> dict[str, float]:
        where, args = area.where("code")
        rows = self._db.execute(
            f"SELECT substr(code, 1, {int(code_length)}) AS u, sum(area_km2) FROM tract WHERE {where} GROUP BY u",
            args,
        ).fetchall()
        return dict(rows)

    def within(
        self, lon: float, lat: float, radius_m: float, sel: Selection | None = None
    ) -> list[tuple[str | None, str | None]]:
        """(category, brand) of each place within radius_m of a point."""
        m_lat = 110_574.0
        m_lon = m_lat * math.cos(math.radians(lat))
        dlon, dlat = radius_m / m_lon, radius_m / m_lat
        where = "lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?"
        args: list[object] = [lon - dlon, lon + dlon, lat - dlat, lat + dlat]
        if sel is not None:
            where += f" AND {sel.sql}"
            args += list(sel.args)
        rows = self._db.execute(f"SELECT category, brand, lon, lat FROM poi WHERE {where}", args).fetchall()
        r2 = radius_m * radius_m
        return [(c, b) for c, b, x, y in rows if ((x - lon) * m_lon) ** 2 + ((y - lat) * m_lat) ** 2 <= r2]

    def tags_like(self, pattern: str, limit: int = 8) -> list[str]:
        rows = self._db.execute(
            "SELECT tag FROM poi WHERE tag LIKE ? GROUP BY tag ORDER BY count(*) DESC LIMIT ?",
            (pattern, limit),
        ).fetchall()
        return [r[0] for r in rows]

    def close(self) -> None:
        self._db.close()


SPEC = AssetSpec(
    label="OpenStreetMap snapshot",
    db_name=DB_NAME,
    manifest_name=MANIFEST_NAME,
    schema_version=SCHEMA_VERSION,
    release_url=RELEASE_URL,
    env_var="SPAIN_MCP_OSM_DB",
    subdir="osm",
    build_script="scripts/build_osm_snapshot.py",
)


class SnapshotStore(ReleaseStore[Snapshot]):
    """Finds, downloads and refreshes the OpenStreetMap snapshot (see core/assets.py)."""

    def __init__(self, directory: Path | None = None, **kwargs: Any) -> None:
        super().__init__(SPEC, lambda path, stale: Snapshot(path, stale_reason=stale), directory, **kwargs)


def area_for_codes(codes: Iterable[str]) -> Area:
    return Area(tuple(sorted(set(codes))))
