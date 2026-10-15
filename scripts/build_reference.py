"""Build the bundled reference files in src/spain_open_data_mcp/data/.

    uv run python scripts/build_reference.py

Writes:
- municipalities.csv: every INE municipality with code, name, province, official
  population (Padrón, table 29005) and the INE value id used to filter census tables.
- ine_tables.json: which INE table holds each income atlas (ADRH) indicator set for
  each province. INE renumbers these tables at times, so this is rebuilt rather than
  hand-maintained. Re-run after each ADRH release (October each year).

Data: INE, CC BY 4.0. "Elaboración propia con datos extraídos del sitio web del INE: www.ine.es"
"""

from __future__ import annotations

import csv
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from spain_open_data_mcp.config import settings

BASE = "https://servicios.ine.es/wstempus/js/ES"
OUT = Path(__file__).resolve().parents[1] / "src" / "spain_open_data_mcp" / "data"

PADRON_MUNICIPAL = 29005
CENSUS_AGE_SEX = 65034
ADRH_OPERATION = 353
ADRH_KINDS = {
    "Indicadores de renta media y mediana": "income",
    "Indicadores demográficos": "demographics",
    "Índice de Gini y Distribución de la renta P80/P20": "inequality",
    "Distribución por fuente de ingresos": "income_sources",
}

client = httpx.Client(headers={"User-Agent": settings.user_agent}, timeout=300, follow_redirects=True)


def get(path: str, **params: Any) -> Any:
    status = 0
    for attempt in range(6):
        r = client.get(f"{BASE}/{path}", params=params)
        status = r.status_code
        if status == 200:
            time.sleep(0.2)
            return r.json()
        time.sleep(2 * 2**attempt)  # also covers INE's HTTP 202 'request in process'
    raise SystemExit(f"INE {path} failed: HTTP {status}")


def build_municipalities() -> int:
    print("Padrón municipal figures (29005)...")
    rows: dict[str, dict[str, Any]] = {}
    year = 0
    for s in get(f"DATOS_TABLA/{PADRON_MUNICIPAL}", nult=1, tip="AM"):
        meta = {m["T3_Variable"]: m for m in s["MetaData"]}
        if meta["Sexo"]["Nombre"] != "Total" or not s["Data"]:
            continue
        muni = meta["Municipios"]
        point = s["Data"][0]
        year = max(year, point["Anyo"])
        rows[muni["Codigo"]] = {
            "code": muni["Codigo"],
            "name": muni["Nombre"],
            "province_code": muni["Codigo"][:2],
            "population": int(point["Valor"]) if point["Valor"] is not None else "",
            "population_date": point["Fecha"][:10],
            "census_value_id": "",
        }
    print("Census value ids (65034)...")
    groups = get(f"GRUPOS_TABLA/{CENSUS_AGE_SEX}")
    territory = next(g for g in groups if "municipios" in g["Nombre"].lower())
    for v in get(f"VALORES_GRUPOSTABLA/{CENSUS_AGE_SEX}/{territory['Id']}"):
        if v["FK_Variable"] == 19 and v["Codigo"] in rows:
            rows[v["Codigo"]]["census_value_id"] = v["Id"]
    with (OUT / "municipalities.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(next(iter(rows.values())).keys()))
        w.writeheader()
        for code in sorted(rows):
            w.writerow(rows[code])
    print(f"  {len(rows)} municipalities, Padrón {year}")
    return year


def build_tables() -> dict[str, Any]:
    print("ADRH tables by province...")
    adrh: dict[str, dict[str, int]] = {}
    national: dict[str, int] = {}  # every municipality, district and tract in one table
    aggregates: dict[str, int] = {}  # Spain, CCAA and provinces
    for t in get(f"TABLAS_OPERACION/{ADRH_OPERATION}"):
        kind = ADRH_KINDS.get(t["Nombre"].strip())
        if not kind:
            continue
        groups = get(f"GRUPOS_TABLA/{t['Id']}")
        territory = next(g for g in groups if "territoriales" in g["Nombre"].lower())
        values = get(f"VALORES_GRUPOSTABLA/{t['Id']}/{territory['Id']}")
        provinces = sorted({v["Codigo"][:2] for v in values if v["FK_Variable"] == 19 and v["Codigo"]})
        if len(provinces) == 1:
            adrh.setdefault(provinces[0], {})[kind] = t["Id"]
        elif len(provinces) > 1:
            national[kind] = t["Id"]
        elif any(v["FK_Variable"] == 349 for v in values):  # 'Total Nacional'
            aggregates[kind] = t["Id"]
        print(f"  {t['Id']} {kind}: {len(provinces)} province(s)")
    census_groups = {g["Nombre"]: g["Id"] for g in get(f"GRUPOS_TABLA/{CENSUS_AGE_SEX}")}
    # Tempus value ids are not the INE codes (province '08' has a different id), so map them.
    territory_id = next(i for n, i in census_groups.items() if "municipios" in n.lower())
    province_ids = {
        v["Codigo"]: v["Id"]
        for v in get(f"VALORES_GRUPOSTABLA/{CENSUS_AGE_SEX}/{territory_id}")
        if v["FK_Variable"] == 115
    }
    return {
        "padron_municipal": PADRON_MUNICIPAL,
        "census_age_sex": CENSUS_AGE_SEX,
        "census_age_sex_groups": census_groups,
        "census_province_value_ids": dict(sorted(province_ids.items())),
        "adrh_operation": ADRH_OPERATION,
        "adrh_by_province": dict(sorted(adrh.items())),
        "adrh_national": national,
        "adrh_aggregates": aggregates,
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    padron_year = build_municipalities()
    tables = build_tables()
    tables["generated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    tables["padron_year"] = padron_year
    (OUT / "ine_tables.json").write_text(json.dumps(tables, indent=2, ensure_ascii=False) + "\n")
    print("Done.")


if __name__ == "__main__":
    main()
