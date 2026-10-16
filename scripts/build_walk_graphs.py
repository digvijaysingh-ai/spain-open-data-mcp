"""Build walkable street graphs per province, for walking-time catchments.

    uv run --group snapshot python scripts/build_walk_graphs.py --geometry build/geometry/tracts_geometry.sqlite

Steps:
1. From the Geofabrik extracts, keep ways a person can walk along (footways, paths,
   pedestrian streets, residential and other roads without foot=no or access=no; motorways
   and trunk roads excluded) and the nodes they use. The extract is pre-filtered first, as in
   the business snapshot, to keep memory down.
2. Simplify: keep only nodes where ways meet or end; each edge is the walking length between
   two kept nodes along the way.
3. Assign kept nodes to provinces with the census tract shapes; an edge belongs to the
   province of each of its ends, so graphs overlap at borders.
4. Write one compressed numpy file per province (walk_XX.npz: node lon/lat, adjacency in CSR
   form, edge lengths in metres) and walk_manifest.json (checksum and bounding box per
   province). Published on the walk-v<schema> release; the server fetches a province only
   when a catchment needs it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
import sys
import time
from array import array
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import osmium
import shapely

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from spain_open_data_mcp.sources.geo.walk import MANIFEST_NAME, SCHEMA_VERSION, file_name  # noqa: E402

log = logging.getLogger("build_walk_graphs")
EXTRACTS = {"spain": "spain-latest.osm.pbf", "canary-islands": "canary-islands-latest.osm.pbf"}
WALKABLE = {
    "footway", "pedestrian", "path", "steps", "living_street", "residential", "service", "unclassified",
    "tertiary", "tertiary_link", "secondary", "secondary_link", "primary", "primary_link", "track",
    "cycleway", "bridleway", "corridor", "road",
}  # fmt: skip
NO_FOOT = {"no", "private"}


def walkable(tags: osmium.osm.TagList) -> bool:
    hw = tags.get("highway")
    if hw not in WALKABLE:
        return False
    if tags.get("foot") in NO_FOOT:
        return False
    if tags.get("access") in NO_FOOT and tags.get("foot") not in ("yes", "designated", "permissive"):
        return False
    return tags.get("area") != "yes"


def prefilter(pbf: Path, out: Path) -> Path:
    if out.exists() and out.stat().st_mtime >= pbf.stat().st_mtime:
        return out
    tmp = out.with_suffix(".part.pbf")
    tmp.unlink(missing_ok=True)
    with osmium.BackReferenceWriter(str(tmp), ref_src=str(pbf), overwrite=True) as writer:
        for obj in osmium.FileProcessor(str(pbf), osmium.osm.WAY).with_filter(
            osmium.filter.KeyFilter("highway")
        ):
            writer.add(obj)
    tmp.rename(out)
    log.info("%s: filtered to %.0f MB", pbf.name, out.stat().st_size / 1e6)
    return out


def read_ways(pbf: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Node refs, lon, lat of every walkable way's nodes, and way start offsets."""
    # Typed arrays, not lists: Spain has about 60 million node references (8 bytes each here,
    # about 36 as Python objects).
    refs, lons, lats, starts = array("q"), array("d"), array("d"), array("q")
    ways = (
        osmium.FileProcessor(str(pbf), osmium.osm.NODE | osmium.osm.WAY)
        .with_locations()
        .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY))  # nodes feed locations, never Python
    )
    for w in ways:
        if not walkable(w.tags):
            continue
        nodes = [n for n in w.nodes if n.location.valid()]
        if len(nodes) < 2:
            continue
        starts.append(len(refs))
        for n in nodes:
            refs.append(n.ref)
            lons.append(n.lon)
            lats.append(n.lat)
    return (
        np.frombuffer(refs, dtype=np.int64),
        np.frombuffer(lons, dtype=np.float64),
        np.frombuffer(lats, dtype=np.float64),
        np.frombuffer(starts, dtype=np.int64),
    )


def simplify(refs: np.ndarray, lons: np.ndarray, lats: np.ndarray, starts: np.ndarray):
    """Keep nodes where ways meet or end; return kept node ids, coords and edges with lengths."""
    uniq, inverse, counts = np.unique(refs, return_inverse=True, return_counts=True)
    keep = counts[inverse] > 1
    ends = np.zeros(len(refs), dtype=bool)
    ends[starts] = True
    ends[np.r_[starts[1:] - 1, len(refs) - 1]] = True
    keep |= ends
    # segment lengths between consecutive nodes of the same way (equirectangular, metres)
    lat_mid = np.radians((lats[1:] + lats[:-1]) / 2)
    dx = np.radians(lons[1:] - lons[:-1]) * np.cos(lat_mid) * 6_371_000
    dy = np.radians(lats[1:] - lats[:-1]) * 6_371_000
    seg = np.hypot(dx, dy)
    same_way = np.ones(len(refs) - 1, dtype=bool)
    same_way[starts[1:] - 1] = False
    us: list[int] = []
    vs: list[int] = []
    ls: list[float] = []
    acc, prev = 0.0, -1
    for i in range(len(refs)):
        if i == 0 or not same_way[i - 1]:
            acc, prev = 0.0, i
            continue
        acc += float(seg[i - 1])
        if keep[i]:
            us.append(int(inverse[prev]))
            vs.append(int(inverse[i]))
            ls.append(acc)
            acc, prev = 0.0, i
    kept = np.unique(np.r_[us, vs]).astype(np.int64)
    remap = -np.ones(len(uniq), dtype=np.int64)
    remap[kept] = np.arange(len(kept))
    first = np.zeros(len(uniq), dtype=np.int64)
    first[inverse[::-1]] = np.arange(len(refs))[::-1]
    node_lon, node_lat = lons[first[kept]], lats[first[kept]]
    return node_lon, node_lat, remap[np.asarray(us)], remap[np.asarray(vs)], np.asarray(ls, dtype=np.float32)


def province_polygons(geometry_db: Path) -> dict[str, object]:
    con = sqlite3.connect(f"file:{geometry_db}?mode=ro", uri=True)
    shapes: dict[str, list] = {}
    for code, blob in con.execute("SELECT code, wkb FROM tract"):
        shapes.setdefault(code[:2], []).append(shapely.from_wkb(blob))
    con.close()
    return {p: shapely.union_all(s) for p, s in shapes.items()}


def assign(node_lon: np.ndarray, node_lat: np.ndarray, polys: dict[str, object]) -> np.ndarray:
    codes = sorted(polys)
    tree = shapely.STRtree([polys[c] for c in codes])
    points = shapely.points(node_lon, node_lat)
    out = np.full(len(points), -1, dtype=np.int16)
    pi, ti = tree.query(points, predicate="within")
    out[pi] = [int(codes[t]) for t in ti]
    return out


def write_province(out_dir: Path, prov: str, node_lon, node_lat, u, v, length, node_prov) -> dict:
    mask = (node_prov[u] == int(prov)) | (node_prov[v] == int(prov))
    eu, ev, el = u[mask], v[mask], length[mask]
    nodes = np.unique(np.r_[eu, ev])
    remap = -np.ones(len(node_lon), dtype=np.int64)
    remap[nodes] = np.arange(len(nodes))
    a, b = remap[eu], remap[ev]
    src, dst, w = np.r_[a, b], np.r_[b, a], np.r_[el, el]  # walkable both ways
    order = np.argsort(src, kind="stable")
    src, dst, w = src[order], dst[order], w[order]
    indptr = np.zeros(len(nodes) + 1, dtype=np.int64)
    np.add.at(indptr, src + 1, 1)
    indptr = np.cumsum(indptr)
    lon, lat = node_lon[nodes].astype(np.float32), node_lat[nodes].astype(np.float32)
    path = out_dir / file_name(prov)
    np.savez_compressed(
        path, lon=lon, lat=lat, indptr=indptr.astype(np.int32), indices=dst.astype(np.int32), length=w
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "file": path.name,
        "sha256": digest,
        "bytes": path.stat().st_size,
        "nodes": len(nodes),
        "edges": len(el),
        "bbox": [float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max())],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--geometry", type=Path, required=True, help="tracts_geometry.sqlite")
    ap.add_argument("--work", type=Path, default=ROOT / "build" / "downloads")
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "walk")
    ap.add_argument("--extract", action="append", choices=sorted(EXTRACTS))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr)
    args.out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    polys = province_polygons(args.geometry)
    log.info("%d province shapes", len(polys))
    parts = []
    stamps = []
    for name in args.extract or sorted(EXTRACTS):
        pbf = args.work / EXTRACTS[name]
        header = osmium.io.Reader(str(pbf), osmium.osm.osm_entity_bits.NOTHING).header()
        stamps.append(header.get("osmosis_replication_timestamp") or "")
        filtered = prefilter(pbf, args.work / f"{name}-highways.osm.pbf")
        refs, lons, lats, starts = read_ways(filtered)
        log.info("%s: %d walkable ways, %d node references", name, len(starts), len(refs))
        parts.append(simplify(refs, lons, lats, starts))
    offset = 0
    node_lon_l, node_lat_l, u_l, v_l, len_l = [], [], [], [], []
    for node_lon, node_lat, u, v, length in parts:
        node_lon_l.append(node_lon)
        node_lat_l.append(node_lat)
        u_l.append(u + offset)
        v_l.append(v + offset)
        len_l.append(length)
        offset += len(node_lon)
    node_lon, node_lat = np.concatenate(node_lon_l), np.concatenate(node_lat_l)
    u, v, length = np.concatenate(u_l), np.concatenate(v_l), np.concatenate(len_l)
    node_prov = assign(node_lon, node_lat, polys)
    log.info(
        "%d nodes, %d edges, %d nodes outside any province", len(node_lon), len(u), int((node_prov < 0).sum())
    )
    files = {}
    for prov in sorted({f"{p:02d}" for p in np.unique(node_prov) if p >= 0}):
        files[prov] = write_province(args.out, prov, node_lon, node_lat, u, v, length, node_prov)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "osm_timestamp": min(s for s in stamps if s) if any(stamps) else "",
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "provinces": files,
    }
    (args.out / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    total = sum(f["bytes"] for f in files.values())
    log.info("wrote %d provinces, %.1f MB, in %.0f s", len(files), total / 1e6, time.monotonic() - started)


if __name__ == "__main__":
    main()
