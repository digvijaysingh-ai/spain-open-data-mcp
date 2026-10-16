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

import asyncio
import gzip
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from pathlib import Path

import httpx

from ...config import PROJECT_URL, settings
from ...core.errors import UpstreamError
from ...core.text import clean_text
from .categories import NOT_IN_TOTAL

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


class SnapshotCorrupt(Exception):
    """The snapshot file is unreadable, truncated or not a snapshot."""


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

    def tags_like(self, pattern: str, limit: int = 8) -> list[str]:
        rows = self._db.execute(
            "SELECT tag FROM poi WHERE tag LIKE ? GROUP BY tag ORDER BY count(*) DESC LIMIT ?",
            (pattern, limit),
        ).fetchall()
        return [r[0] for r in rows]

    def close(self) -> None:
        self._db.close()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class SnapshotStore:
    """Finds, downloads and refreshes the snapshot. One per process."""

    def __init__(
        self,
        directory: Path | None = None,
        *,
        fixed_path: Path | None = None,
        release_url: str = RELEASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        offline: bool | None = None,
    ) -> None:
        env_path = os.environ.get("SPAIN_MCP_OSM_DB")
        self.fixed_path = fixed_path or (Path(env_path) if env_path else None)
        self.directory = directory or settings.cache_dir / "osm"
        self.release_url = release_url
        self.transport = transport
        self.offline = settings.offline if offline is None else offline
        self._snapshot: Snapshot | None = None
        self._checked_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def db_path(self) -> Path:
        return self.directory / DB_NAME

    async def get(self) -> Snapshot:
        async with self._lock:
            if self.fixed_path:
                if self._snapshot is None:
                    if not self.fixed_path.exists():
                        raise UpstreamError(
                            problem="The OpenStreetMap snapshot is missing.",
                            cause=f"SPAIN_MCP_OSM_DB points to {self.fixed_path}, which does not exist.",
                            fix="Fix the path, or unset SPAIN_MCP_OSM_DB to download the published snapshot.",
                        )
                    try:
                        self._snapshot = Snapshot(self.fixed_path)
                    except SnapshotCorrupt as e:
                        raise UpstreamError(
                            problem="The OpenStreetMap snapshot file is damaged.",
                            cause=f"{self.fixed_path}: {e}.",
                            fix="Rebuild it with scripts/build_osm_snapshot.py, or unset SPAIN_MCP_OSM_DB "
                            "to download the published snapshot.",
                        ) from e
                return self._snapshot
            if self._snapshot is None or time.time() - self._checked_at > CHECK_EVERY_S:
                try:
                    await self._refresh()
                except SnapshotCorrupt as e:
                    # Damaged local copy (interrupted write, full disk): start again, once.
                    log.warning("snapshot %s is damaged (%s); downloading it again", self.db_path, e)
                    self._discard()
                    try:
                        await self._refresh()
                    except SnapshotCorrupt as e2:
                        self._discard()
                        raise UpstreamError(
                            problem="The OpenStreetMap snapshot is damaged, and so was a fresh download.",
                            cause=str(e2),
                            fix="Check free disk space in the cache directory and try again, or set "
                            "SPAIN_MCP_OSM_DB to a snapshot you built.",
                        ) from e2
            assert self._snapshot is not None
            return self._snapshot

    def _discard(self) -> None:
        if self._snapshot is not None:
            self._snapshot.close()
            self._snapshot = None
        for name in (DB_NAME, MANIFEST_NAME, "checked_at"):
            (self.directory / name).unlink(missing_ok=True)

    async def _refresh(self) -> None:
        have = self.db_path.exists()
        marker = self.directory / "checked_at"
        if have and self.offline:
            self._open(None)
            return
        if have and marker.exists() and time.time() - marker.stat().st_mtime < CHECK_EVERY_S:
            self._checked_at = marker.stat().st_mtime
            self._open(None)
            return
        try:
            await self._download_if_newer()
            self.directory.mkdir(parents=True, exist_ok=True)
            marker.touch()
            self._checked_at = time.time()
            self._open(None)
        except (httpx.HTTPError, OSError, ValueError, UpstreamError) as e:
            if not have:
                if isinstance(e, UpstreamError):
                    raise
                raise UpstreamError(
                    problem="The OpenStreetMap business snapshot could not be downloaded.",
                    cause=f"{type(e).__name__}: {e}",
                    fix="Try again in a minute. If it keeps failing, download osm_businesses.sqlite.gz "
                    f"from {self.release_url}, unzip it and set SPAIN_MCP_OSM_DB to its path.",
                ) from e
            log.warning("snapshot refresh failed, using local copy: %s", e)
            self._checked_at = time.time()
            self._open(f"the check for a newer snapshot failed ({type(e).__name__})")

    def _open(self, stale_reason: str | None) -> None:
        if self._snapshot is not None and self._snapshot.path == self.db_path:
            self._snapshot.stale_reason = stale_reason
            return
        self._snapshot = Snapshot(self.db_path, stale_reason=stale_reason)

    async def _download_if_newer(self) -> None:
        if self.offline:
            raise UpstreamError(
                problem="The OpenStreetMap snapshot is not on this machine.",
                cause="Offline mode (SPAIN_MCP_OFFLINE=1) does not download it.",
                fix="Unset SPAIN_MCP_OFFLINE once to download it, or set SPAIN_MCP_OSM_DB.",
            )
        async with httpx.AsyncClient(
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
            timeout=httpx.Timeout(settings.http_timeout_s, read=300),
            transport=self.transport,
        ) as client:
            r = await client.get(self.release_url + MANIFEST_NAME)
            r.raise_for_status()
            manifest = json.loads(r.text)
            if int(manifest.get("schema_version", 0)) != SCHEMA_VERSION:
                raise UpstreamError(
                    problem="The published OpenStreetMap snapshot has a different format.",
                    cause=f"It is schema {manifest.get('schema_version')}; this server reads schema "
                    f"{SCHEMA_VERSION}.",
                    fix="Upgrade spain-open-data-mcp to the latest version.",
                )
            local = self.directory / MANIFEST_NAME
            if (
                self.db_path.exists()
                and local.exists()
                and json.loads(local.read_text(encoding="utf-8")).get("sha256") == manifest["sha256"]
            ):
                return
            self.directory.mkdir(parents=True, exist_ok=True)
            tmp = self.directory / (DB_NAME + ".part")
            gz = self.directory / (DB_NAME + ".gz.part")
            log.info("downloading OSM snapshot %s", manifest.get("osm_timestamp"))
            async with client.stream("GET", self.release_url + manifest["file"] + ".gz") as resp:
                resp.raise_for_status()
                with gz.open("wb") as f:
                    async for chunk in resp.aiter_bytes(1 << 20):
                        f.write(chunk)
            await asyncio.to_thread(_gunzip, gz, tmp)
            gz.unlink()
            digest = await asyncio.to_thread(_sha256, tmp)
            if digest != manifest["sha256"]:
                tmp.unlink()
                raise ValueError("downloaded snapshot does not match the manifest checksum")
            if self._snapshot is not None:
                self._snapshot.close()
                self._snapshot = None
            tmp.replace(self.db_path)
            local.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _gunzip(src: Path, dest: Path) -> None:
    with gzip.open(src, "rb") as fin, dest.open("wb") as fout:
        shutil.copyfileobj(fin, fout, 1 << 20)


def area_for_codes(codes: Iterable[str]) -> Area:
    return Area(tuple(sorted(set(codes))))
