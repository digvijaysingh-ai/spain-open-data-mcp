"""A tiny tract geometry file: square tracts around the mini snapshot's places, for offline tests.

The mini snapshot puts every place at (-4.7, 41.6). Four 0.02° squares meet there, named
with Valladolid tract codes that exist in the recorded income-atlas fixtures.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from shapely import to_wkb
from shapely.geometry import box

from spain_open_data_mcp.sources.geo.geometry import INDEX_SQL, SCHEMA_SQL, SCHEMA_VERSION

CENTRE = (-4.7, 41.6)
TRACTS = {
    "4718601001": (-4.72, 41.60, -4.70, 41.62),
    "4718601003": (-4.70, 41.60, -4.68, 41.62),
    "4718604001": (-4.72, 41.58, -4.70, 41.60),
    "4718610001": (-4.70, 41.58, -4.68, 41.60),
}


def make_geometry(path: Path) -> Path:
    db = sqlite3.connect(path)
    db.executescript(SCHEMA_SQL)
    for code, (minx, miny, maxx, maxy) in TRACTS.items():
        db.execute(
            "INSERT INTO tract VALUES (?, ?, ?, ?, ?, ?, ?)",
            (code, minx, miny, maxx, maxy, 3.7, to_wkb(box(minx, miny, maxx, maxy))),
        )
    meta = {
        "schema_version": str(SCHEMA_VERSION),
        "tracts_year": "2024",
        "built_at": "2026-10-16T00:00:00+00:00",
    }
    db.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
    db.executescript(INDEX_SQL)
    db.commit()
    db.close()
    return path
