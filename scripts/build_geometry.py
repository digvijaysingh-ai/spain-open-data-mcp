"""Build the census tract geometry used for catchments (a circle around an address).

    uv run --group snapshot python scripts/build_geometry.py --year 2024

Reads INE's tract boundaries for the year the income atlas uses, simplifies them to about
10 m in ETRS89 / UTM 30N, converts them to WGS84 and writes tracts_geometry.sqlite: one row
per tract with its bounding box (indexed, so a catchment loads only nearby shapes), area and
shape as WKB. Published on the geometry-v<schema> release; the server downloads it the first
time a catchment is asked for.

The income atlas reports residents and income per tract on its own year's codes, so the
geometry must be the same year. INE redraws some tracts every year: about 97-99% of the
atlas's tract codes have a shape in the matching year's file, and the rest are reported in
answers as residents the catchment could not place.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sqlite3
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import shapefile
import shapely
from pyproj import Geod, Transformer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from spain_open_data_mcp.sources.geo.geometry import (  # noqa: E402
    DB_NAME,
    INDEX_SQL,
    MANIFEST_NAME,
    SCHEMA_SQL,
    SCHEMA_VERSION,
)

TRACTS_URL = "https://www.ine.es/prodyser/cartografia/seccionado_{year}.zip"
SIMPLIFY_M = 10.0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int, required=True, help="tract boundaries year (the income atlas's)")
    ap.add_argument("--work", type=Path, default=ROOT / "build" / "downloads")
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "geometry")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    zpath = args.work / f"seccionado_{args.year}.zip"
    if not zpath.exists():
        raise SystemExit(f"download {TRACTS_URL.format(year=args.year)} to {zpath} first")
    z = zipfile.ZipFile(zpath)
    parts = {n.rsplit(".", 1)[1].lower(): n for n in z.namelist() if "." in n}
    reader = shapefile.Reader(
        shp=io.BytesIO(z.read(parts["shp"])),
        shx=io.BytesIO(z.read(parts["shx"])),
        dbf=io.BytesIO(z.read(parts["dbf"])),
        encoding=z.read(parts["cpg"]).decode().strip() if "cpg" in parts else "latin-1",
    )
    to_wgs84 = Transformer.from_crs(25830, 4326, always_xy=True)
    geod = Geod(ellps="GRS80")
    db_path = args.out / DB_NAME
    db_path.unlink(missing_ok=True)
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA_SQL)
    rows = []
    for sr in reader.iterShapeRecords():
        geom = shapely.geometry.shape(sr.shape.__geo_interface__)
        if not geom.is_valid:
            geom = shapely.make_valid(geom)
        geom = geom.simplify(SIMPLIFY_M, preserve_topology=True)
        wgs = shapely.transform(geom, lambda xy: np.column_stack(to_wgs84.transform(xy[:, 0], xy[:, 1])))
        area_m2, _ = geod.geometry_area_perimeter(wgs)
        minx, miny, maxx, maxy = wgs.bounds
        rows.append(
            (sr.record["CUSEC"], minx, miny, maxx, maxy, round(abs(area_m2) / 1e6, 5), shapely.to_wkb(wgs))
        )
    db.executemany("INSERT INTO tract VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    meta = {
        "schema_version": str(SCHEMA_VERSION),
        "tracts_year": str(args.year),
        "tracts_url": TRACTS_URL.format(year=args.year),
        "simplify_m": str(SIMPLIFY_M),
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "tracts": str(len(rows)),
    }
    db.executemany("INSERT INTO meta VALUES (?, ?)", sorted(meta.items()))
    db.executescript(INDEX_SQL)
    db.commit()
    db.execute("VACUUM")
    db.close()
    digest = hashlib.sha256(db_path.read_bytes()).hexdigest()
    manifest = {**meta, "file": DB_NAME, "bytes": db_path.stat().st_size, "sha256": digest}
    (args.out / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {db_path} ({db_path.stat().st_size / 1e6:.1f} MB, {len(rows)} tracts)")


if __name__ == "__main__":
    main()
