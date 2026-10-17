"""Build area_names.json.gz: neighbourhood names for INE districts and census tracts.

    uv run --group snapshot python scripts/build_area_names.py

INE numbers districts and tracts ('Valladolid distrito 11') but does not name them. This puts
every named neighbourhood, quarter, suburb, village and city district from OpenStreetMap
(place=* points and areas, and administrative boundaries below municipality level) in the
census tract that contains it, then labels:
- each district with up to three names inside it, broadest kind first
  (district boundaries and suburbs before neighbourhoods), then by tract population;
- each tract with up to two names inside it, or the nearest name in its district
  within 1.5 km ('near ...').

Only municipalities with more than one tract are covered. The file also lists every name
with its tract, so place names such as 'Parquesol, Valladolid' resolve to a district.

Inputs (reuses the OSM snapshot's downloads): build/downloads/{spain,canary-islands}-latest.osm.pbf,
seccionado_<year>.zip, and optionally build/census/census_tracts.sqlite for tract populations.
Data: © OpenStreetMap contributors, ODbL 1.0; INE tract boundaries, CC BY 4.0.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import re
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import osmium
import shapely
from pyproj import Transformer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from build_osm_snapshot import (  # noqa: E402
    EXTRACTS,
    TRACTS_URL,
    download,
    read_tracts,
    replication_timestamp,
)

from spain_open_data_mcp.core.text import clean_text  # noqa: E402
from spain_open_data_mcp.places.area_names import FILE_NAME, KIND_RANK  # noqa: E402
from spain_open_data_mcp.places.resolver import (  # noqa: E402
    _municipalities,
    name_variants,
    normalize,
)

log = logging.getLogger("build_area_names")
PLACE_KINDS = {"suburb", "quarter", "neighbourhood", "town", "village", "hamlet", "borough"}
NEAR_M = 1_500
# 'Distrito Macarena' -> 'Macarena', "Barri de l'Eixample" stays as written only if nothing is left.
_PREFIX = re.compile(r"(?i)^(distrito|districte|barrio|barri|auzoa)\s+(?:(?:de|del|d')\s*)?(?=\S)")


@dataclass
class Named:
    name: str
    kind: str
    lon: float
    lat: float


def kind_of(tags: dict[str, str]) -> str | None:
    if tags.get("boundary") == "administrative" and tags.get("admin_level") in ("9", "10"):
        return f"admin{tags['admin_level']}"
    place = tags.get("place")
    return place if place in PLACE_KINDS else None


def prefilter(pbf: Path, out: Path) -> Path:
    """Only place=* objects and administrative boundaries, with the ways and nodes they use."""
    if out.exists() and out.stat().st_mtime >= pbf.stat().st_mtime:
        log.info("using %s", out)
        return out
    tmp = out.with_suffix(".part.pbf")
    tmp.unlink(missing_ok=True)
    flt = osmium.filter.TagFilter(("boundary", "administrative"))
    with osmium.BackReferenceWriter(str(tmp), ref_src=str(pbf), overwrite=True) as writer:
        for obj in osmium.FileProcessor(str(pbf)).with_filter(osmium.filter.KeyFilter("place")):
            writer.add(obj)
        for obj in osmium.FileProcessor(str(pbf)).with_filter(flt):
            if obj.is_relation() and obj.tags.get("admin_level") in ("9", "10"):
                writer.add(obj)
    tmp.rename(out)
    log.info("%s: filtered to %.1f MB", pbf.name, out.stat().st_size / 1e6)
    return out


def read_named(pbf: Path) -> list[Named]:
    fp = osmium.FileProcessor(str(pbf)).with_locations().with_areas()
    out: list[Named] = []
    for obj in fp:
        tags = {t.k: t.v for t in obj.tags}
        kind = kind_of(tags)
        name = clean_text(tags.get("name"))
        if name:
            name = _PREFIX.sub("", name).strip() or name
            name = name[0].upper() + name[1:]
        if not kind or not name:
            continue
        if obj.is_area():
            rings = []
            for outer in obj.outer_rings():
                coords = [(n.lon, n.lat) for n in outer if n.location.valid()]
                if len(coords) >= 4:
                    rings.append(shapely.Polygon(coords))
            if not rings:
                continue
            geom = shapely.make_valid(shapely.MultiPolygon(rings) if len(rings) > 1 else rings[0])
            pt = geom.point_on_surface()
            out.append(Named(name, kind, pt.x, pt.y))
        elif obj.is_node() and obj.location.valid():
            out.append(Named(name, kind, obj.location.lon, obj.location.lat))
        # plain ways and relations come through as areas above
    log.info("%s: %d named places and boundaries", pbf.name, len(out))
    return out


def tract_populations(path: Path) -> dict[str, int]:
    if not path.exists():
        log.warning("%s missing: districts rank names by kind only", path)
        return {}
    db = sqlite3.connect(path)
    out = {}
    for code, data in db.execute("SELECT code, data FROM tract"):
        totals = json.loads(data)["total"]
        out[code] = int(totals[max(totals)]) if totals else 0
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", type=Path, default=ROOT / "build" / "downloads")
    ap.add_argument("--census", type=Path, default=ROOT / "build" / "census" / "census_tracts.sqlite")
    ap.add_argument("--tracts-year", type=int, default=2025)
    ap.add_argument("--out", type=Path, default=ROOT / "src" / "spain_open_data_mcp" / "data" / FILE_NAME)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr)
    args.work.mkdir(parents=True, exist_ok=True)

    tracts_zip = download(
        TRACTS_URL.format(year=args.tracts_year), args.work / f"seccionado_{args.tracts_year}.zip"
    )
    codes, geoms, _areas = read_tracts(tracts_zip)
    tracts_per_muni: dict[str, int] = defaultdict(int)
    for c in codes:
        tracts_per_muni[c[:5]] += 1

    named: list[Named] = []
    stamps = {}
    for name in sorted(EXTRACTS):
        pbf = download(EXTRACTS[name], args.work / f"{name}-latest.osm.pbf")
        stamps[name] = replication_timestamp(pbf)
        named.extend(read_named(prefilter(pbf, args.work / f"{name}-places.osm.pbf")))

    to_utm = Transformer.from_crs(4326, 25830, always_xy=True)
    x, y = to_utm.transform([n.lon for n in named], [n.lat for n in named])
    points = shapely.points(np.asarray(x), np.asarray(y))
    tree = shapely.STRtree(geoms)
    pi, ti = tree.query(points, predicate="intersects")
    tract_of: dict[int, str] = {}
    for p, t in zip(pi.tolist(), ti.tolist(), strict=True):
        tract_of.setdefault(p, codes[t])

    munis = _municipalities()
    muni_names = {c: name_variants(m.name) for c, m in munis.items()}
    # One entry per (municipality, name): the broadest kind wins.
    best: dict[tuple[str, str], tuple[int, str, str, int]] = {}
    for i, n in enumerate(named):
        tract = tract_of.get(i)
        if not tract or tracts_per_muni[tract[:5]] < 2:
            continue
        key = normalize(n.name)
        if not key or key in muni_names.get(tract[:5], set()):
            continue  # the municipality's own name
        rank = KIND_RANK[n.kind]
        prev = best.get((tract[:5], key))
        if prev is None or rank < prev[0]:
            best[(tract[:5], key)] = (rank, n.name, tract, i)
    log.info("%d distinct names in %d municipalities", len(best), len({m for m, _ in best}))

    pop = tract_populations(args.census)
    by_district: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    by_tract: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for (_muni, _key), (rank, name, tract, _i) in best.items():
        by_district[tract[:7]].append((rank, -pop.get(tract, 0), name))
        by_tract[tract].append((rank, name))

    labels: dict[str, list[str]] = {}

    def top(names: list[str], n: int) -> list[str]:
        seen: set[str] = set()
        out = []
        for name in names:
            key = re.sub(r"^(el|la|los|las|l|els|les|o|a|os|as) ", "", normalize(name))
            if key not in seen:
                seen.add(key)
                out.append(name)
        return out[:n]

    for district, items in by_district.items():
        labels[district] = top([n for _r, _p, n in sorted(items)], 3)
    for tract, items in by_tract.items():
        labels[tract] = top([n for _r, n in sorted(items)], 2)

    # Tracts with no name inside: the nearest name in the same district, if close.
    by_district_pts: dict[str, list[int]] = defaultdict(list)
    for (_m, _k), (_r, _n, tract, i) in best.items():
        by_district_pts[tract[:7]].append(i)
    near: dict[str, str] = {}
    code_index = {c: k for k, c in enumerate(codes)}
    for c in codes:
        idx = by_district_pts.get(c[:7])
        if c in labels or not idx:
            continue
        dists = shapely.distance(geoms[code_index[c]], points[idx])
        j = int(np.argmin(dists))
        if dists[j] <= NEAR_M:
            near[c] = named[idx[j]].name

    entries = sorted([name, tract] for (_m, _k), (_r, name, tract, _i) in best.items())
    osm_date = min(stamps.values())
    data = {
        "meta": {
            "osm_timestamp": osm_date.isoformat(),
            "tracts_year": str(args.tracts_year),
            "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "licence": "© OpenStreetMap contributors, ODbL 1.0; INE tract boundaries, CC BY 4.0",
        },
        "labels": dict(sorted(labels.items())),
        "near": dict(sorted(near.items())),
        "places": entries,
    }
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    args.out.write_bytes(gzip.compress(raw, mtime=0))
    log.info(
        "wrote %s (%.0f kB): %d districts and %d tracts labelled, %d near-labels, %d names",
        args.out,
        args.out.stat().st_size / 1e3,
        sum(1 for k in labels if len(k) == 7),
        sum(1 for k in labels if len(k) == 10),
        len(near),
        len(entries),
    )


if __name__ == "__main__":
    main()
