"""A small, hand-made OSM snapshot with known counts, for offline tests."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from spain_open_data_mcp.sources.osm.snapshot import INDEX_SQL, SCHEMA_SQL, SCHEMA_VERSION

# (tract, category, tag, brand, how many)
PLACES: list[tuple[str, str | None, str, str | None, int]] = [
    # Valladolid (47186): 6 gyms, 2 of them Basic-Fit
    ("4718601001", "gym", "leisure=fitness_centre", "Basic-Fit", 2),
    ("4718601001", "gym", "leisure=fitness_centre", None, 1),
    ("4718604001", "gym", "leisure=fitness_centre", None, 1),
    ("4718612001", "gym", "leisure=fitness_centre", None, 2),
    ("4718601001", "restaurant", "amenity=restaurant", None, 4),
    ("4718601003", "bar", "amenity=bar", None, 6),
    ("4718610001", "cafe", "amenity=cafe", None, 2),
    ("4718601001", "school", "amenity=school", None, 1),  # not in 'all businesses'
    ("4718604001", None, "shop=gift", None, 1),
    # One pharmacy per district, so the map completeness check has something to measure
    ("4718601001", "pharmacy", "amenity=pharmacy", None, 1),
    ("4718604001", "pharmacy", "amenity=pharmacy", None, 1),
    ("4718610001", "pharmacy", "amenity=pharmacy", None, 1),
    ("4718612001", "pharmacy", "amenity=pharmacy", None, 1),
    # Elsewhere in the province of Valladolid
    ("4700101001", "gym", "leisure=fitness_centre", None, 1),
    # Madrid and Las Palmas
    ("2807901001", "gym", "leisure=fitness_centre", "Basic-Fit", 10),
    ("3501601001", "restaurant", "amenity=restaurant", None, 5),
]
TRACTS: dict[str, float] = {
    "4718601001": 0.5,
    "4718601003": 0.25,
    "4718604001": 1.0,
    "4718605001": 2.0,  # no places
    "4718610001": 0.75,
    "4718612001": 1.5,
    "4700101001": 30.0,
    "2807901001": 0.4,
    "3501601001": 0.6,
}


def make_snapshot(path: Path, *, osm_date: datetime | None = None) -> Path:
    osm_date = osm_date or datetime.now(UTC) - timedelta(days=5)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA_SQL)
    n = 0
    for tract, category, tag, brand, count in PLACES:
        for _ in range(count):
            n += 1
            db.execute(
                "INSERT INTO poi VALUES (?, ?, ?, ?, ?, ?, ?)",
                (tract, category, tag, brand, -4.7, 41.6, f"n{n}"),
            )
    db.executemany("INSERT INTO tract VALUES (?, ?)", TRACTS.items())
    meta = {
        "schema_version": str(SCHEMA_VERSION),
        "osm_timestamp": osm_date.isoformat(),
        "tracts_year": "2025",
        "tracts_url": "https://www.ine.es/prodyser/cartografia/seccionado_2025.zip",
        "built_at": osm_date.isoformat(),
    }
    db.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
    db.executescript(INDEX_SQL)
    db.commit()
    db.close()
    return path
