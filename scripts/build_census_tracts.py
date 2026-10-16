"""Build census_tracts.sqlite from INE's bulk file of the annual census (table 65034).

    curl -o build/downloads/65034.csv https://www.ine.es/jaxiT3/files/t/es/csv_bdsc/65034.csv
    uv run python scripts/build_census_tracts.py --csv build/downloads/65034.csv --out build/census

The bulk file (about 1.6 GB, 15 million rows) has population by tract, sex and five-year age
band for five years. Tract rows are streamed and kept as: total population for every year,
and sex and age bands for the latest year. Published on the census-v<schema> release.

Data: INE, CC BY 4.0. "Elaboración propia con datos extraídos del sitio web del INE: www.ine.es"
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from spain_open_data_mcp.sources.ine.census_tracts import (  # noqa: E402
    BANDS,
    DB_NAME,
    MANIFEST_NAME,
    SCHEMA_SQL,
    SCHEMA_VERSION,
)

SOURCE_URL = "https://www.ine.es/jaxiT3/Tabla.htm?t=65034"
_BAND = re.compile(r"De (\d+) a (\d+) años|(\d+) y más años")
SEX = {"Total": "total", "Hombres": "male", "Mujeres": "female"}


def band(age: str) -> str | None:
    m = _BAND.fullmatch(age.strip())
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}" if m.group(1) else f"{m.group(3)}+"


def number(text: str) -> float | None:
    t = text.strip().replace(".", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None  # '..' or empty: withheld or missing


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "census")
    args = ap.parse_args()

    tracts: dict[str, dict] = {}
    latest = 0
    with args.csv.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f, delimiter=";")
        header = next(reader)
        col = {name.strip().rstrip(":"): i for i, name in enumerate(header)}
        i_tract, i_sex, i_age, i_year, i_val = (
            col[k] for k in ("Secciones", "Sexo", "Edad", "Periodo", "Total")
        )
        for row in reader:
            tract = row[i_tract]
            if not tract:
                continue
            code = tract.split(" ", 1)[0]
            year = int(row[i_year])
            value = number(row[i_val])
            if value is None:
                continue
            latest = max(latest, year)
            t = tracts.setdefault(code, {"total": {}, "latest": {}})
            sex, age = SEX.get(row[i_sex]), row[i_age]
            if age == "Todas las edades":
                if sex == "total":
                    t["total"][str(year)] = value
                elif sex:
                    t["latest"][(year, sex)] = value
            elif sex == "total" and (b := band(age)):
                t["latest"][(year, b)] = value
    rows = []
    for code, t in tracts.items():
        if str(latest) not in t["total"]:
            continue  # tract not in the latest year's boundaries
        data = {
            "total": {y: int(v) for y, v in sorted(t["total"].items())},
            "male": int(t["latest"].get((latest, "male"), 0)),
            "female": int(t["latest"].get((latest, "female"), 0)),
            "bands": {b: int(t["latest"].get((latest, b), 0)) for b in BANDS},
        }
        rows.append((code, json.dumps(data, separators=(",", ":"))))
    if len(rows) < 30_000:
        raise SystemExit(f"Only {len(rows)} tracts for {latest}; the file layout may have changed")
    args.out.mkdir(parents=True, exist_ok=True)
    db_path = args.out / DB_NAME
    db_path.unlink(missing_ok=True)
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA_SQL)
    db.executemany("INSERT INTO tract VALUES (?, ?)", sorted(rows))
    meta = {
        "schema_version": str(SCHEMA_VERSION),
        "year": str(latest),
        "source_url": SOURCE_URL,
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "tracts": str(len(rows)),
    }
    db.executemany("INSERT INTO meta VALUES (?, ?)", sorted(meta.items()))
    db.commit()
    db.execute("VACUUM")
    db.close()
    digest = hashlib.sha256(db_path.read_bytes()).hexdigest()
    manifest = {**meta, "file": DB_NAME, "bytes": db_path.stat().st_size, "sha256": digest}
    (args.out / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {db_path} ({db_path.stat().st_size / 1e6:.1f} MB, {len(rows)} tracts, {latest})")


if __name__ == "__main__":
    main()
