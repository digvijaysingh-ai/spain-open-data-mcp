"""A tiny census-by-tract file: two tracts of Valladolid's district 01, one with a missing year."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from spain_open_data_mcp.sources.ine.census_tracts import BANDS, SCHEMA_SQL, SCHEMA_VERSION


def _tract(total: dict[str, int], male: int) -> str:
    latest = total["2025"]
    bands = dict.fromkeys(BANDS, 0)
    bands["0-4"], bands["40-44"], bands["80-84"] = (
        latest // 10,
        latest // 2,
        latest - latest // 10 - latest // 2,
    )
    return json.dumps({"total": total, "male": male, "female": latest - male, "bands": bands})


TRACTS = {
    "4718601001": _tract({"2021": 1000, "2022": 1010, "2023": 1020, "2024": 1030, "2025": 1040}, 500),
    "4718601003": _tract({"2023": 700, "2024": 720, "2025": 760}, 380),  # created in 2023
}


def make_census(path: Path) -> Path:
    db = sqlite3.connect(path)
    db.executescript(SCHEMA_SQL)
    db.executemany("INSERT INTO tract VALUES (?, ?)", TRACTS.items())
    meta = {"schema_version": str(SCHEMA_VERSION), "year": "2025", "built_at": "2026-10-16T00:00:00+00:00",
            "source_url": "https://www.ine.es/jaxiT3/Tabla.htm?t=65034"}  # fmt: skip
    db.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
    db.commit()
    db.close()
    return path
