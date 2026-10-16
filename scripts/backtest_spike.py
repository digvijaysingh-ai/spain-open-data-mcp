"""Spike: can OpenStreetMap changes between two snapshots tell real openings from late mapping?

    uv run --group snapshot python scripts/backtest_spike.py OLD.sqlite NEW.sqlite OLD_FILTERED.pbf NEW_FILTERED.pbf

For a few cities, finds places of a category in the new snapshot with no place of the same
category within 30 m in the old one ("appearances"), then reads their full OSM tags to sort
them by evidence:
- opened: the same object was vacant or disused in the old data, or its start_date or
  opening_date is in the old snapshot's year or later;
- catch_up: start_date before the old snapshot (the business existed, it was mapped late);
  this is the placebo set for the backtest;
- retagged: the object existed in the old data with another business tag;
- unknown: a new object with no date.
Prints the shares per city. Throwaway analysis for the design doc, not part of the server.
"""

from __future__ import annotations

import math
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

import osmium

CITIES = {"47186": "Valladolid", "41091": "Sevilla", "50297": "Zaragoza"}
MATCH_M = 30.0


def load(db: Path, prefix: str) -> list[tuple[str, str, float, float]]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT osm, category, lon, lat FROM poi WHERE category IS NOT NULL AND tract >= ? AND tract < ?",
        (prefix, prefix + ":"),
    ).fetchall()
    con.close()
    return rows


def metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    dx = (a[0] - b[0]) * 111_320 * math.cos(math.radians(a[1]))
    dy = (a[1] - b[1]) * 110_540
    return math.hypot(dx, dy)


def appearances(old: list, new: list) -> list[tuple[str, str]]:
    grid: dict[tuple[str, int, int], list[tuple[float, float]]] = defaultdict(list)
    for _osm, cat, lon, lat in old:
        grid[(cat, int(lon * 1000), int(lat * 1000))].append((lon, lat))
    out = []
    for osm, cat, lon, lat in new:
        gx, gy = int(lon * 1000), int(lat * 1000)
        near = (p for dx in (-1, 0, 1) for dy in (-1, 0, 1) for p in grid.get((cat, gx + dx, gy + dy), ()))
        if not any(metres((lon, lat), p) <= MATCH_M for p in near):
            out.append((osm, cat))
    return out


def tags_by_id(pbf: Path, wanted: set[str]) -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    for obj in osmium.FileProcessor(str(pbf)):
        if obj.is_area():
            continue
        key = f"{obj.type_str()}{obj.id}"
        if key in wanted:
            found[key] = {t.k: t.v for t in obj.tags}
    return found


def year_of(value: str | None) -> int | None:
    return int(value[:4]) if value and value[:4].isdigit() else None


def main() -> None:
    old_db, new_db, old_pbf, new_pbf = map(Path, sys.argv[1:5])
    old_year = 2025
    per_city: dict[str, list[tuple[str, str]]] = {}
    for code, name in CITIES.items():
        per_city[name] = appearances(load(old_db, code), load(new_db, code))
        print(
            f"{name}: {len(load(old_db, code))} -> {len(load(new_db, code))} places, "
            f"{len(per_city[name])} appearances"
        )
    wanted = {osm for apps in per_city.values() for osm, _ in apps}
    new_tags = tags_by_id(new_pbf, wanted)
    old_tags = tags_by_id(old_pbf, wanted)
    for name, apps in per_city.items():
        kinds: Counter[str] = Counter()
        for osm, _cat in apps:
            t, ot = new_tags.get(osm, {}), old_tags.get(osm)
            start = year_of(t.get("start_date") or t.get("opening_date"))
            if ot is not None and (
                ot.get("shop") in ("vacant", "no") or any(k.startswith("disused:") for k in ot)
            ):
                kinds["opened (was vacant)"] += 1
            elif start is not None and start >= old_year:
                kinds["opened (dated)"] += 1
            elif start is not None:
                kinds["catch_up (dated earlier)"] += 1
            elif ot is not None:
                kinds["retagged"] += 1
            else:
                kinds["unknown (new object, no date)"] += 1
        total = sum(kinds.values()) or 1
        print(f"\n{name} ({total} appearances)")
        for k, n in kinds.most_common():
            print(f"  {k:32} {n:5}  {n / total:6.1%}")
        dated = sum(1 for osm, _ in apps if new_tags.get(osm, {}).get("start_date"))
        print(f"  with any start_date: {dated / total:.1%}")


if __name__ == "__main__":
    main()
