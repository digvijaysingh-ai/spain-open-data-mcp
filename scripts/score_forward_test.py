"""Score a registered atlas against INE's business openings (the pre-registered forward test).

    uv run python scripts/score_forward_test.py --registration build/atlas/registration.json \\
        --atlas build/atlas/atlas.csv [--dry-run]

For each atlas category, provinces are ranked by their population-weighted opportunity score;
the outcome is new businesses ('altas', INE business demography table 82131) in the category's
CNAE division per existing business, summed over the quarters after registration. The lift
(top fifth vs bottom fifth of provinces) is compared with a population-only ranking.

Writes validation/results/<method>-<as of>.json and .md. Until a quarter after registration
is published, it records 'awaiting data'. --dry-run uses all published quarters regardless of
the registration date, to test the pipeline; its numbers are not evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from spain_open_data_mcp.core.http import DAY, HttpClient  # noqa: E402
from spain_open_data_mcp.engine.validation import (  # noqa: E402
    CATEGORY_DIVISION,
    quarter_after,
    quintile_lift,
    weighted_scores,
)
from spain_open_data_mcp.places.areas import population_of  # noqa: E402
from spain_open_data_mcp.places.codes import PROVINCES  # noqa: E402
from spain_open_data_mcp.sources.ine.datasets import VAR_PROVINCE, tables  # noqa: E402
from spain_open_data_mcp.sources.ine.tempus import TempusClient  # noqa: E402

CODEM_PROVINCE_DIVISIONS = 82131
OUT = ROOT / "validation" / "results"


async def openings() -> dict[tuple[str, str], dict[tuple[int, int], dict[str, float]]]:
    """(province, division) -> quarter -> {'altas', 'stock'} from INE table 82131."""
    tempus = TempusClient(HttpClient())
    ids = tables()["census_province_value_ids"]
    out: dict[tuple[str, str], dict[tuple[int, int], dict[str, float]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    for prov in sorted(PROVINCES):
        res = await tempus.table(
            CODEM_PROVINCE_DIVISIONS, last_n=12, filters=[(VAR_PROVINCE, ids[prov])], ttl_s=7 * DAY
        )
        for s in res.series:
            div = s.dims.get("DIVISIONES", ("", ""))[0]
            concept = next((n for v, (_c, n) in s.dims.items() if v.startswith("Conceptos")), "")
            if not div:
                continue
            for p in s.points:
                if p.value is None or not p.period.startswith("T"):
                    continue
                q = out[(prov, div)][(p.year, int(p.period[1:]))]
                if concept.startswith("Altas"):
                    q["altas"] = p.value
                elif concept in ("Permanencias", "Bajas"):
                    q["stock"] = q.get("stock", 0.0) + p.value  # units at the start of the quarter
    return out


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--registration", type=Path, required=True)
    ap.add_argument("--atlas", type=Path, required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    reg = json.loads(args.registration.read_text(encoding="utf-8"))
    if hashlib.sha256(args.atlas.read_bytes()).hexdigest() != reg["atlas_sha256"]:
        raise SystemExit(
            "atlas.csv does not match the registration's checksum: not the registered prediction"
        )
    rows = list(csv.DictReader(args.atlas.open(encoding="utf-8")))
    flows = await openings()
    registered = reg["registered_at"][:10]
    quarters = sorted({q for per in flows.values() for q in per})
    used = quarters if args.dry_run else [q for q in quarters if quarter_after(q, registered)]
    as_of = datetime.now(UTC).date().isoformat()
    result: dict = {
        "method": reg["method"],
        "registered_at": registered,
        "as_of": as_of,
        "dry_run": args.dry_run,
    }
    if not used:
        result["status"] = "awaiting data"
        result["note"] = (
            f"No quarter after {registered} is published yet; INE publishes CODEM about 4 months after each quarter."
        )
    else:
        result["status"] = "dry run (not evidence)" if args.dry_run else "scored"
        result["quarters"] = [f"{y}T{q}" for y, q in used]
        pop = {p: float(population_of(p)[0]) for p in PROVINCES}
        per_category = {}
        for cat in sorted({r["category"] for r in rows}):
            div = CATEGORY_DIVISION.get(cat)
            if not div:
                continue
            scores = weighted_scores([r for r in rows if r["category"] == cat], 2)
            outcome = {}
            for prov in PROVINCES:
                per = flows.get((prov, div), {})
                altas = sum(per[q].get("altas", 0.0) for q in used if q in per)
                stock = sum(per[q].get("stock", 0.0) for q in used if q in per)
                if stock > 0:
                    outcome[prov] = altas / stock
            model, base = quintile_lift(scores, outcome), quintile_lift(pop, outcome)
            per_category[cat] = {
                "division": div,
                "lift": model.__dict__ if model else None,
                "population_baseline_lift": base.__dict__ if base else None,
            }
        result["categories"] = per_category
    OUT.mkdir(parents=True, exist_ok=True)
    stem = f"{reg['method']}-{as_of}" + ("-dry-run" if args.dry_run else "")
    (OUT / f"{stem}.json").write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    lines = [
        f"# Forward test: {reg['method']}, registered {registered}, as of {as_of}",
        "",
        f"Status: {result['status']}.",
    ]
    if result.get("note"):
        lines.append(result["note"])
    for cat, c in result.get("categories", {}).items():
        m, b = c["lift"], c["population_baseline_lift"]
        if m and b:
            lines.append(
                f"- {cat} (CNAE {c['division']}): lift {m['lift']:.2f} ({m['ci_low']:.2f}-{m['ci_high']:.2f}) "
                f"vs population baseline {b['lift']:.2f}"
            )
    (OUT / f"{stem}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    asyncio.run(main())
