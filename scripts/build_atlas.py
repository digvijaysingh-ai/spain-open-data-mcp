"""Build the Spain Opportunity Atlas: every municipality (and the districts of large cities,
for local categories) scored for each atlas category with the same engine the tools use.

    uv run python scripts/build_atlas.py --out build/atlas [--provinces 47 28] [--categories gym bar]

Writes:
- atlas.csv: one row per area x category with every input, its method and status, so any
  number on the map can be traced and recomputed.
- site/<category>.json: compact per-category files for the map page.
- registration.json: method version, date, row count and the CSV's SHA-256. Committed to
  validation/registrations/ when the atlas is published: the frozen prediction the
  quarterly check against INE's business demography (CODEM) scores.
- hero_spain.json: municipalities over 20,000 residents with descriptive figures only
  (calibrated restaurant supply per 1,000 residents and market per business) for
  djcrafted.com; the unvalidated score is left out.

A row whose inputs could not all be fetched is kept with status 'missing:<input>' and no
score; the build fails if more than 1% of rows are missing an input.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import logging
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from spain_open_data_mcp.categories import ATLAS_CATEGORIES, BY_KEY, MARKET  # noqa: E402
from spain_open_data_mcp.core.errors import DataError  # noqa: E402
from spain_open_data_mcp.core.http import HttpClient  # noqa: E402
from spain_open_data_mcp.engine import model  # noqa: E402
from spain_open_data_mcp.engine.inputs import MarketContext  # noqa: E402
from spain_open_data_mcp.places.areas import population_of  # noqa: E402
from spain_open_data_mcp.places.codes import PROVINCES  # noqa: E402
from spain_open_data_mcp.places.resolver import municipalities_in, place_from_code  # noqa: E402
from spain_open_data_mcp.sources.ine.datasets import IneData  # noqa: E402
from spain_open_data_mcp.sources.ine.tempus import TempusClient  # noqa: E402
from spain_open_data_mcp.sources.osm.snapshot import SnapshotStore  # noqa: E402

log = logging.getLogger("build_atlas")
DISTRICT_CITY_MIN = 100_000  # districts are scored for local categories in cities this large
HERO_MIN = 20_000
MAX_MISSING_SHARE = 0.01
COLUMNS = [
    "method", "category", "code", "name", "level", "province", "region", "population", "households",
    "monthly_income", "spend_per_household", "income_ratio", "family_share", "market_low", "market_base",
    "market_high", "visitor_market_base", "mapped_places", "supply_low", "supply_base", "supply_high",
    "supply_method", "score_low", "score_base", "score_high", "completeness", "too_small", "status",
]  # fmt: skip


def centroids(geometry_db: Path | None) -> dict[str, tuple[float, float]]:
    """Municipality and district centre as the mean of their tracts' bounding-box centres."""
    if geometry_db is None or not geometry_db.exists():
        return {}
    con = sqlite3.connect(f"file:{geometry_db}?mode=ro", uri=True)
    acc: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    for code, minx, miny, maxx, maxy in con.execute("SELECT code, minx, miny, maxx, maxy FROM tract"):
        for key in (code[:5], code[:7]):
            a = acc[key]
            a[0] += (minx + maxx) / 2
            a[1] += (miny + maxy) / 2
            a[2] += 1
    con.close()
    return {k: (round(v[0] / v[2], 4), round(v[1] / v[2], 4)) for k, v in acc.items()}


def r(v: float | None, digits: int = 0) -> float | None:
    return None if v is None else round(v, digits)


async def build(args: argparse.Namespace) -> None:
    ine = IneData(TempusClient(HttpClient()))
    snap = await SnapshotStore().get()
    provinces = args.provinces or sorted(PROVINCES)
    categories = args.categories or list(ATLAS_CATEGORIES)
    rows: list[dict] = []
    started = time.monotonic()
    for key in categories:
        profile = MARKET[key]
        if profile.visitor_only:
            log.info("%s: skipped (visitor-only, no market model yet)", key)
            continue
        ctx = await MarketContext.create(ine, snap, key)
        for prov in provinces:
            region = f"CA{PROVINCES[prov][1]}"
            for code in municipalities_in(prov):
                pop = population_of(code)[0]
                areas = [(code, place_from_code(code).name, "municipality", pop)]
                if profile.scope == "local" and pop >= DISTRICT_CITY_MIN:
                    demo = await ctx._units("demographics", prov)
                    for d in sorted(c for c in demo if len(c) == 7 and c.startswith(code)):
                        found = demo[d].latest("population")
                        areas.append((d, demo[d].name, "district", int(found[1]) if found else 0))
                for a_code, name, level, a_pop in areas:
                    row = {"method": model.METHOD_VERSION, "category": key, "code": a_code, "name": name, "level": level,
                           "province": prov, "region": region, "population": a_pop}  # fmt: skip
                    if a_pop < profile.min_population:
                        rows.append(row | {"too_small": True, "status": "ok"})
                        continue
                    try:
                        res = await ctx.evaluate(a_code, name, level, region, a_pop)
                    except (DataError, KeyError, ZeroDivisionError) as e:
                        rows.append(row | {"too_small": False, "status": f"missing:{type(e).__name__}"})
                        continue
                    m, s, sc = res.market, res.supply, res.score
                    rows.append(
                        row
                        | {
                            "households": r(res.households.base if res.households else None),
                            "monthly_income": res.monthly_income,
                            "spend_per_household": r(res.spend_per_household.base),
                            "income_ratio": r(res.income_ratio.base, 3),
                            "family_share": r(res.family_share.base, 3),
                            "market_low": r(m.low if m else None),
                            "market_base": r(m.base if m else None),
                            "market_high": r(m.high if m else None),
                            "visitor_market_base": r(
                                res.visitor_foreign.base if res.visitor_foreign else None
                            ),
                            "mapped_places": res.mapped_places,
                            "supply_low": r(s.low, 1),
                            "supply_base": r(s.base, 1),
                            "supply_high": r(s.high, 1),
                            "supply_method": res.supply_method,
                            "score_low": r(sc.low if sc else None, 3),
                            "score_base": r(sc.base if sc else None, 3),
                            "score_high": r(sc.high if sc else None, 3),
                            "completeness": r(res.completeness, 3),
                            "too_small": res.too_small,
                            "status": "ok" if m is not None else "missing:households",
                        }
                    )
            log.info(
                "%s: province %s done (%d rows so far, %.0f s)",
                key,
                prov,
                len(rows),
                time.monotonic() - started,
            )
    write_outputs(args.out, rows, centroids(args.geometry))


def write_outputs(out: Path, rows: list[dict], centres: dict[str, tuple[float, float]]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    missing = [row for row in rows if str(row.get("status", "")).startswith("missing")]
    share = len(missing) / len(rows) if rows else 0.0
    if share > MAX_MISSING_SHARE:
        raise SystemExit(
            f"{len(missing)} of {len(rows)} rows ({share:.1%}) are missing an input; not publishing"
        )
    csv_path = out / "atlas.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    built = datetime.now(UTC).isoformat(timespec="seconds")
    site = out / "site"
    site.mkdir(exist_ok=True)
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        c = centres.get(row["code"])
        if c is None or row["level"] != "municipality" or row.get("too_small"):
            continue  # too-small towns are not drawn: the page says so in its legend
        thin = row.get("completeness") is not None and row["completeness"] < model.COMPLETENESS_LOW
        by_cat[row["category"]].append(
            {
                "c": row["code"],
                "n": row["name"],
                "x": c[0],
                "y": c[1],
                "p": row["population"],
                "m": row.get("market_base"),
                "b": row.get("supply_base"),
                "s": row.get("score_base"),
                "lo": row.get("score_low"),
                "hi": row.get("score_high"),
                "f": "small"
                if row.get("too_small")
                else ("thin" if thin else ("missing" if str(row["status"]) != "ok" else "")),
            }
        )
    index = []
    for key, items in sorted(by_cat.items()):
        (site / f"{key}.json").write_text(
            json.dumps(items, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )
        index.append({"key": key, "label": BY_KEY[key].label, "areas": len(items)})
    meta = {"method": model.METHOD_VERSION, "built_at": built, "atlas_sha256": digest, "categories": index}
    (site / "index.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    registration = {
        "method": model.METHOD_VERSION,
        "registered_at": built,
        "rows": len(rows),
        "atlas_sha256": digest,
        "missing_rows": len(missing),
        "outcome": "INE business demography (CODEM): openings by province and CNAE division, and municipal "
        "opening rates, in the quarters after registration",
    }
    (out / "registration.json").write_text(json.dumps(registration, indent=2) + "\n", encoding="utf-8")
    hero = []
    for row in rows:
        if row["category"] != "restaurant" or row["level"] != "municipality" or row["population"] < HERO_MIN:
            continue
        c = centres.get(row["code"])
        if c is None or row.get("supply_base") is None or row.get("market_base") is None:
            continue
        per_1000 = row["supply_base"] / row["population"] * 1000
        per_business = row["market_base"] / (row["supply_base"] + model.PSEUDO_BUSINESSES)
        hero.append(
            {
                "id": row["code"],
                "name": row["name"],
                "lat": c[1],
                "lon": c[0],
                "population": row["population"],
                "metric": round(per_1000, 2),
                "insight": f"{row['name']}: about {row['supply_base']:,.0f} restaurants, {per_1000:.1f} per 1,000 "
                f"residents; €{per_business / 1000:,.0f}k of residents' spending per restaurant",
                "source": "INE (EPF, DIRCE, Padrón, ADRH), © OpenStreetMap contributors",
                "data_date": built[:10],
            }
        )
    hero_doc = {
        "metric": "Estimated restaurants per 1,000 residents (DIRCE-calibrated OpenStreetMap count)",
        "method": model.METHOD_VERSION,
        "licences": "INE data CC BY 4.0 (Fuente: INE); OpenStreetMap data © OpenStreetMap contributors, ODbL 1.0",
        "municipalities": sorted(hero, key=lambda h: -h["population"]),
    }
    (out / "hero_spain.json").write_text(
        json.dumps(hero_doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    log.info(
        "atlas: %d rows (%d missing), %d categories, hero %d municipalities (%.0f KB)",
        len(rows), len(missing), len(index), len(hero), (out / "hero_spain.json").stat().st_size / 1000,
    )  # fmt: skip


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "atlas")
    ap.add_argument("--geometry", type=Path, default=ROOT / "build" / "geometry" / "tracts_geometry.sqlite")
    ap.add_argument("--provinces", nargs="*")
    ap.add_argument("--categories", nargs="*")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(build(args))


if __name__ == "__main__":
    main()
