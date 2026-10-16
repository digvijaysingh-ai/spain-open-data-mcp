"""Census population by census tract: age bands and sex for the latest year, totals for five.

Live queries to INE table 65034 at tract level take about 19 s per 100 tracts, so the tract
rows of its bulk file are published as a release asset (census-v1, built by
scripts/build_census_tracts.py) and read locally. A district is the sum of its tracts.

Each row is one tract: {"total": {year: n}, "male": n, "female": n, "bands": {"0-4": n, ...}}
with sex and bands for the latest year. Tracts follow that year's boundaries; INE redraws
some every year, so a tract code from an older income atlas may be missing here.
"""

from __future__ import annotations

import json
import sqlite3
from functools import cached_property
from pathlib import Path
from typing import Any

from ...config import PROJECT_URL
from ...core.assets import AssetCorrupt, AssetSpec, ReleaseStore
from .tempus import Point, Series

SCHEMA_VERSION = 1
DB_NAME = "census_tracts.sqlite"
MANIFEST_NAME = "census_manifest.json"
RELEASE_TAG = f"census-v{SCHEMA_VERSION}"
RELEASE_URL = f"{PROJECT_URL}/releases/download/{RELEASE_TAG}/"
SCHEMA_SQL = """
CREATE TABLE tract (code TEXT PRIMARY KEY, data TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
"""
BANDS = [f"{lo}-{lo + 4}" for lo in range(0, 100, 5)] + ["100+"]


def band_label(band: str) -> str:
    """'0-4' -> 'De 0 a 4 años', '100+' -> '100 y más años' (INE's wording)."""
    if band.endswith("+"):
        return f"{band[:-1]} y más años"
    lo, hi = band.split("-")
    return f"De {lo} a {hi} años"


class CensusTracts:
    def __init__(self, path: Path, *, stale_reason: str | None = None) -> None:
        self.path = path
        self.stale_reason = stale_reason
        self._db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        try:
            if self._db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise AssetCorrupt("integrity check failed")
            if "year" not in self.meta:
                raise AssetCorrupt("metadata missing: year")
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
    def year(self) -> int:
        return int(self.meta["year"])

    def close(self) -> None:
        self._db.close()

    def tracts(self, prefix: str) -> dict[str, dict[str, Any]]:
        """Tracts whose code starts with `prefix` (a district, municipality or tract code)."""
        rows = self._db.execute(
            "SELECT code, data FROM tract WHERE code >= ? AND code < ?", (prefix, prefix + "￿")
        ).fetchall()
        return {code: json.loads(data) for code, data in rows}

    def series(self, prefix: str) -> list[Series]:
        """The area's census as INE-style series (sex x age), summed over its tracts, so the
        code that reads table 65034 reads this too. Empty if no tract matches."""
        rows = self.tracts(prefix)
        if not rows:
            return []
        year = self.year
        # Only years every tract has: INE redraws tracts, and a partial sum would look like decline.
        years = set.intersection(*({int(y) for y in r["total"]} for r in rows.values()))
        totals: dict[int, float] = {}
        male = female = 0.0
        bands = dict.fromkeys(BANDS, 0.0)
        for r in rows.values():
            for y, v in r["total"].items():
                if int(y) in years:
                    totals[int(y)] = totals.get(int(y), 0) + v
            male += r.get("male", 0)
            female += r.get("female", 0)
            for b, v in r.get("bands", {}).items():
                bands[b] += v

        def series(sex: str, age: str, points: dict[int, float]) -> Series:
            return Series(
                code="",
                name=f"{prefix}. {sex}. {age}",
                unit="Personas",
                dims={"Sexo": ("", sex), "Edad": ("", age)},
                points=[Point(year=y, period="A", date=f"{y}-01-01", value=v, status="ok", provisional=False) for y, v in sorted(points.items())],
            )  # fmt: skip

        out = [series("Total", "Todas las edades", totals)]
        out += [
            series("Hombres", "Todas las edades", {year: male}),
            series("Mujeres", "Todas las edades", {year: female}),
        ]
        out += [series("Total", band_label(b), {year: v}) for b, v in bands.items()]
        return out


SPEC = AssetSpec(
    label="census population by tract",
    db_name=DB_NAME,
    manifest_name=MANIFEST_NAME,
    schema_version=SCHEMA_VERSION,
    release_url=RELEASE_URL,
    env_var="SPAIN_MCP_CENSUS_DB",
    subdir="census",
    build_script="scripts/build_census_tracts.py",
)


class CensusStore(ReleaseStore[CensusTracts]):
    def __init__(self, directory: Path | None = None, **kwargs: Any) -> None:
        super().__init__(
            SPEC, lambda path, stale: CensusTracts(path, stale_reason=stale), directory, **kwargs
        )
