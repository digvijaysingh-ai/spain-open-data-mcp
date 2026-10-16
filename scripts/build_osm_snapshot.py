"""Build the OpenStreetMap business snapshot used by the `businesses` tool.

    uv run --group snapshot python scripts/build_osm_snapshot.py --out build/osm

Steps:
1. Download the Geofabrik extracts for Spain and the Canary Islands (Geofabrik's Spain
   extract leaves the Canaries out) and INE's census tract boundaries.
2. Keep shops, food and drink, offices, crafts, health, fitness and accommodation (see
   sources/osm/categories.py). Ways and multipolygons become one point at their centre.
3. Put every place in the INE census tract that contains it. Places just offshore
   (piers, beach bars) go to the nearest tract within 500 m; the rest are dropped.
4. Write osm_businesses.sqlite plus a manifest with the OSM data date and a checksum.

Only tags are kept, never names, phone numbers or addresses: the tools return counts
and brands, not lists of individual premises.

A run over all of Spain takes about 5 minutes after the 1.5 GB download, and 1 GB of memory.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import sqlite3
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np
import osmium
import shapefile
import shapely
from pyproj import Geod, Transformer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from spain_open_data_mcp.categories import (  # noqa: E402
    CATEGORIES_VERSION,
    LISTED_KEYS,
    OPEN_KEYS,
    classify,
)
from spain_open_data_mcp.config import settings  # noqa: E402
from spain_open_data_mcp.core.text import clean_text  # noqa: E402
from spain_open_data_mcp.sources.osm.snapshot import (  # noqa: E402
    DB_NAME,
    INDEX_SQL,
    MANIFEST_NAME,
    SCHEMA_SQL,
    SCHEMA_VERSION,
)

log = logging.getLogger("build_osm_snapshot")

EXTRACTS = {
    "spain": "https://download.geofabrik.de/europe/spain-latest.osm.pbf",
    "canary-islands": "https://download.geofabrik.de/africa/canary-islands-latest.osm.pbf",
}
TRACTS_URL = "https://www.ine.es/prodyser/cartografia/seccionado_{year}.zip"
OFFSHORE_M = 500


@dataclass
class Poi:
    lon: float
    lat: float
    tag: str
    category: str | None
    brand: str | None
    osm: str  # 'n123', 'w123' or 'r123'


def download(url: str, dest: Path) -> Path:
    if dest.exists():
        log.info("using %s", dest)
        return dest
    log.info("downloading %s", url)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with httpx.stream(
        "GET", url, headers={"User-Agent": settings.user_agent}, follow_redirects=True, timeout=600
    ) as r:
        r.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
    tmp.rename(dest)
    return dest


def replication_timestamp(pbf: Path) -> datetime:
    header = osmium.io.Reader(str(pbf), osmium.osm.osm_entity_bits.NOTHING).header()
    ts = header.get("osmosis_replication_timestamp")
    if not ts:
        raise SystemExit(f"{pbf.name}: no replication timestamp in the file header")
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _tags(obj: osmium.osm.OSMObject) -> dict[str, str]:
    return {t.k: t.v for t in obj.tags}


def prefilter(pbf: Path, out: Path) -> Path:
    """Copy only objects with business keys, plus the ways and nodes they reference.

    The Spain extract has about 200 million nodes; indexing all their locations needs
    more memory than a laptop has. The filtered file is around 2% of the size.
    """
    if out.exists() and out.stat().st_mtime >= pbf.stat().st_mtime:
        log.info("using %s", out)
        return out
    keys = (*OPEN_KEYS, *LISTED_KEYS)
    tmp = out.with_suffix(".part.pbf")
    tmp.unlink(missing_ok=True)
    with osmium.BackReferenceWriter(str(tmp), ref_src=str(pbf), overwrite=True) as writer:
        for obj in osmium.FileProcessor(str(pbf)).with_filter(osmium.filter.KeyFilter(*keys)):
            writer.add(obj)
    tmp.rename(out)
    log.info("%s: filtered to %.1f MB", pbf.name, out.stat().st_size / 1e6)
    return out


def read_pois(pbf: Path) -> list[Poi]:
    """Tagged nodes, ways (mean of their nodes) and multipolygon relations (mean of outer rings)."""
    keys = (*OPEN_KEYS, *LISTED_KEYS)
    fp = (
        osmium.FileProcessor(str(pbf))
        .with_locations()
        .with_areas(osmium.filter.KeyFilter(*keys))
        .with_filter(osmium.filter.KeyFilter(*keys))
    )
    pois: list[Poi] = []
    skipped = 0
    for obj in fp:
        if obj.is_relation():
            continue  # handled through its assembled area
        if obj.is_area():
            if obj.from_way():
                continue  # the way itself is handled below
            coords = [(n.lon, n.lat) for ring in obj.outer_rings() for n in ring if n.location.valid()]
            osm = f"r{obj.orig_id()}"
        elif obj.is_way():
            coords = [(n.lon, n.lat) for n in obj.nodes if n.location.valid()]
            osm = f"w{obj.id}"
        else:
            if not obj.location.valid():
                skipped += 1
                continue
            coords = [(obj.location.lon, obj.location.lat)]
            osm = f"n{obj.id}"
        if not coords:
            skipped += 1
            continue
        tags = _tags(obj)
        found = classify(tags)
        if found is None:
            continue
        tag, category = found
        lon = sum(c[0] for c in coords) / len(coords)
        lat = sum(c[1] for c in coords) / len(coords)
        brand = clean_text(tags.get("brand"))
        pois.append(Poi(lon, lat, tag, category, brand, osm))
    log.info("%s: %d places, %d without coordinates", pbf.name, len(pois), skipped)
    return pois


def read_tracts(zip_path: Path) -> tuple[list[str], np.ndarray, list[float]]:
    """Tract codes, polygons in ETRS89 / UTM 30N (as published), and geodesic areas in km²."""
    z = zipfile.ZipFile(zip_path)
    parts = {n.rsplit(".", 1)[1].lower(): n for n in z.namelist() if "." in n}
    reader = shapefile.Reader(
        shp=io.BytesIO(z.read(parts["shp"])),
        shx=io.BytesIO(z.read(parts["shx"])),
        dbf=io.BytesIO(z.read(parts["dbf"])),
        encoding=z.read(parts["cpg"]).decode().strip() if "cpg" in parts else "latin-1",
    )
    to_wgs84 = Transformer.from_crs(25830, 4326, always_xy=True)
    geod = Geod(ellps="GRS80")
    codes: list[str] = []
    geoms = []
    areas: list[float] = []
    for sr in reader.iterShapeRecords():
        code = sr.record["CUSEC"]
        geom = shapely.geometry.shape(sr.shape.__geo_interface__)
        if not geom.is_valid:
            geom = shapely.make_valid(geom)
        wgs = shapely.transform(geom, lambda xy: np.column_stack(to_wgs84.transform(xy[:, 0], xy[:, 1])))
        area_m2, _ = geod.geometry_area_perimeter(wgs)
        codes.append(code)
        geoms.append(geom)
        areas.append(abs(area_m2) / 1e6)
    log.info("%d census tracts", len(codes))
    return codes, np.array(geoms, dtype=object), areas


def assign_tracts(pois: list[Poi], codes: list[str], geoms: np.ndarray) -> tuple[list[str | None], int, int]:
    to_utm = Transformer.from_crs(4326, 25830, always_xy=True)
    x, y = to_utm.transform([p.lon for p in pois], [p.lat for p in pois])
    points = shapely.points(np.asarray(x), np.asarray(y))
    tree = shapely.STRtree(geoms)
    out: list[str | None] = [None] * len(pois)
    pi, ti = tree.query(points, predicate="intersects")
    for p, t in zip(pi.tolist(), ti.tolist(), strict=True):
        if out[p] is None:
            out[p] = codes[t]
    missing = np.array([i for i, c in enumerate(out) if c is None], dtype=np.int64)
    nearby = 0
    if missing.size:
        pi, ti = tree.query_nearest(points[missing], max_distance=OFFSHORE_M, all_matches=False)
        for p, t in zip(pi.tolist(), ti.tolist(), strict=True):
            out[int(missing[p])] = codes[t]
            nearby += 1
    dropped = sum(1 for c in out if c is None)
    return out, nearby, dropped


def write_db(
    path: Path,
    pois: list[Poi],
    tracts: list[str | None],
    codes: list[str],
    areas: list[float],
    meta: dict[str, str],
) -> None:
    path.unlink(missing_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA_SQL)
    rows = sorted(
        (
            (t, p.category, p.tag, p.brand, round(p.lon, 6), round(p.lat, 6), p.osm)
            for p, t in zip(pois, tracts, strict=True)
            if t
        ),
        key=lambda r: (r[1] or "", r[0]),
    )
    db.executemany("INSERT INTO poi VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    db.executemany(
        "INSERT INTO tract VALUES (?, ?)", [(c, round(a, 4)) for c, a in zip(codes, areas, strict=True)]
    )
    db.executemany("INSERT INTO meta VALUES (?, ?)", sorted(meta.items()))
    db.executescript(INDEX_SQL)
    db.commit()
    db.execute("VACUUM")
    db.close()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "osm")
    ap.add_argument("--work", type=Path, default=ROOT / "build" / "downloads", help="download directory")
    ap.add_argument("--tracts-year", type=int, default=2025)
    ap.add_argument(
        "--extract", action="append", choices=sorted(EXTRACTS), help="limit to some extracts (default: all)"
    )
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr)
    args.out.mkdir(parents=True, exist_ok=True)
    args.work.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    tracts_zip = download(
        TRACTS_URL.format(year=args.tracts_year), args.work / f"seccionado_{args.tracts_year}.zip"
    )
    codes, geoms, areas = read_tracts(tracts_zip)

    pois: list[Poi] = []
    stamps: dict[str, datetime] = {}
    for name in args.extract or sorted(EXTRACTS):
        pbf = download(EXTRACTS[name], args.work / f"{name}-latest.osm.pbf")
        stamps[name] = replication_timestamp(pbf)
        pois.extend(read_pois(prefilter(pbf, args.work / f"{name}-business.osm.pbf")))
    tracts, nearby, dropped = assign_tracts(pois, codes, geoms)
    log.info(
        "%d places: %d in a tract, %d offshore moved to the nearest tract, %d dropped",
        len(pois),
        len(pois) - dropped,
        nearby,
        dropped,
    )

    osm_date = min(stamps.values())
    meta = {
        "schema_version": str(SCHEMA_VERSION),
        "categories_version": str(CATEGORIES_VERSION),
        "osm_timestamp": osm_date.isoformat(),
        "osm_timestamps": json.dumps({k: v.isoformat() for k, v in stamps.items()}),
        "extracts": json.dumps({k: EXTRACTS[k] for k in stamps}),
        "tracts_year": str(args.tracts_year),
        "tracts_url": TRACTS_URL.format(year=args.tracts_year),
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "places": str(len(pois) - dropped),
        "places_offshore_assigned": str(nearby),
        "places_dropped": str(dropped),
    }
    db_path = args.out / DB_NAME
    write_db(db_path, pois, tracts, codes, areas, meta)
    manifest = {**meta, "file": DB_NAME, "bytes": db_path.stat().st_size, "sha256": sha256(db_path)}
    (args.out / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    log.info(
        "wrote %s (%.1f MB) in %.0f s", db_path, db_path.stat().st_size / 1e6, time.monotonic() - started
    )


if __name__ == "__main__":
    main()
