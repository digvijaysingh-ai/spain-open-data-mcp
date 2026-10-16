"""Record trimmed INE responses for offline tests.

    uv run python tests/fixtures/record_ine.py

Each file in tests/fixtures/ine/ is a real Tempus response, cut down to Valladolid
(municipality 47186, its 12 districts and a handful of tracts) and to the province,
region and Spain benchmark rows, so tests stay small and fast.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
from spain_open_data_mcp.config import settings  # noqa: E402
from spain_open_data_mcp.sources.ine.datasets import tables  # noqa: E402
from tests.fixtures.ine_mock import fixture_name  # noqa: E402

OUT = Path(__file__).parent / "ine"
BASE = "https://servicios.ine.es/wstempus/js/ES"
KEEP_TRACTS = {"4718601001", "4718601003", "4718604001", "4718610001", "4718612001"}
KEEP_CNAE = (" 56 ", " 561 ", " 563 ", " 931 ", " 960 ", " 862 ", " 472 ")

client = httpx.Client(headers={"User-Agent": settings.user_agent}, timeout=300)


def fetch(table_id: int, params: list[tuple[str, Any]]) -> Any:
    status = 0
    for attempt in range(6):
        r = client.get(f"{BASE}/DATOS_TABLA/{table_id}", params=params)
        status = r.status_code
        if status == 200:
            return r.json()
        time.sleep(2 * 2**attempt)  # HTTP 202 means INE is still building the table
    raise SystemExit(f"table {table_id}: HTTP {status}")


def keep(series: dict[str, Any]) -> bool:
    for m in series.get("MetaData", []):
        var, code = m.get("T3_Variable", ""), str(m.get("Codigo") or "")
        if var == "Secciones":
            return code in KEEP_TRACTS
        if var in ("Distritos", "Municipios"):
            return code.startswith("47186")
        if var == "Provincias":
            return code == "47"
        if var == "Comunidades y Ciudades Autónomas":
            return code == "07"
        if var == "Total Nacional":
            return True
        if var == "Islas":
            return False
    return True


def keep_cnae(series: dict[str, Any]) -> bool:
    return any(c in series["Nombre"] for c in KEEP_CNAE)


def record(
    table_id: int, params: list[tuple[str, Any]], keep: Callable[[dict[str, Any]], bool] = keep
) -> None:
    raw = fetch(table_id, params)
    trimmed = [s for s in raw if keep(s)]
    name = fixture_name(table_id, params)
    (OUT / name).write_text(json.dumps(trimmed, ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
    print(f"{name}: {len(raw)} -> {len(trimmed)} series")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    t = tables()
    vid = 495  # Valladolid's Tempus value id (see data/municipalities.csv)
    prov = t["adrh_by_province"]["47"]
    agg = t["adrh_aggregates"]
    record(t["census_age_sex"], [("nult", 5), ("tip", "AM"), ("tv", f"19:{vid}")])
    record(t["padron_municipal"], [("nult", 1), ("tip", "AM"), ("tv", f"19:{vid}")])
    for kind in ("income", "inequality", "income_sources", "demographics"):
        n = 5 if kind == "income" else 1
        record(prov[kind], [("nult", n), ("tip", "AM"), ("tv", f"19:{vid}")])
        record(prov[kind], [("nult", n), ("tip", "AM")])
        record(prov[kind], [("nult", 1), ("tip", "AM")])
        record(agg[kind], [("nult", n), ("tip", "AM")])
        record(agg[kind], [("nult", 1), ("tip", "AM")])
    dirce = [("tv", f"{var}:{val}") for var, val in t["dirce_total_filters"]]
    record(
        t["dirce_companies"],
        [("nult", 1), ("tip", "AM"), ("tv", f"70:{t['dirce_region_value_ids']['CA07']}"), *dirce],
        keep_cnae,
    )


if __name__ == "__main__":
    main()
