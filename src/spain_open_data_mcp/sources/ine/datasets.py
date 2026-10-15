"""The INE tables this server uses, and how to fetch each for a place.

Table ids live in data/ine_tables.json (built by scripts/build_reference.py), so a
renumbering by INE is fixed by re-running the script, not by editing code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from importlib import resources
from typing import Any, Literal

from ...core.envelope import Source
from ...core.errors import DataError
from ...core.http import DAY
from ...places.codes import PROVINCES
from ...places.resolver import Place, census_value_id
from .tempus import ATTRIBUTION, LICENCE, TABLE_PAGE, Series, TableResult, TempusClient

# Tempus variable ids (stable across tables)
VAR_PROVINCE = 115
VAR_MUNICIPALITY = 19
VAR_SEX = 18

AdrhKind = Literal["income", "demographics", "inequality", "income_sources"]

ADRH_NAME = "Atlas de Distribución de Renta de los Hogares (ADRH)"
CENSUS_NAME = "Censo anual de población: población por sexo y edad"
PADRON_NAME = "Cifras oficiales de población de los municipios (Padrón)"

# Statistics change once a year; refetch weekly at most.
TTL = 7 * DAY


@lru_cache(maxsize=1)
def tables() -> dict[str, Any]:
    path = resources.files("spain_open_data_mcp.data").joinpath("ine_tables.json")
    return json.loads(path.read_text(encoding="utf-8"))


def source_for(result: TableResult, dataset: str, data_period: str) -> Source:
    return Source(
        publisher="INE (Instituto Nacional de Estadística)",
        dataset=dataset,
        dataset_id=str(result.table_id),
        url=result.url,
        licence=LICENCE,
        attribution=ATTRIBUTION,
        data_period=data_period,
        retrieved_at=result.retrieved_at,
    )


def register_source() -> Source:
    """The bundled municipality list (names, codes, populations), built from INE table 29005."""
    t = tables()
    return Source(
        publisher="INE (Instituto Nacional de Estadística)",
        dataset=PADRON_NAME + " (bundled code list)",
        dataset_id=str(t["padron_municipal"]),
        url=TABLE_PAGE.format(table_id=t["padron_municipal"]),
        licence=LICENCE,
        attribution=ATTRIBUTION,
        data_period=f"{t['padron_year']}-01-01",
        retrieved_at=datetime.fromisoformat(t["generated_at"]),
    )


def latest_year(series: list[Series]) -> int | None:
    years = [p.year for s in series for p in s.points if p.status == "ok"]
    return max(years) if years else None


@dataclass
class IneData:
    tempus: TempusClient

    async def census_age_sex(self, place: Place, last_n: int = 5) -> TableResult:
        """Population by sex and five-year age band, 1 January, for a municipality,
        province or CCAA (as the sum of its provinces)."""
        t = tables()
        if place.level == "municipality":
            vid = census_value_id(place.code)
            if vid is None:
                raise DataError(
                    problem=f"No census breakdown for {place.name}.",
                    cause="The municipality is missing from INE's census table.",
                    fix="Try the province instead.",
                )
            filters = [(VAR_MUNICIPALITY, vid)]
            expected = {place.code}
        elif place.level in ("province", "ccaa"):
            if place.level == "province":
                expected = {place.code}
            else:
                expected = {p for p, (_n, ccaa, _a) in PROVINCES.items() if f"CA{ccaa}" == place.code}
            ids = t["census_province_value_ids"]
            filters = [(VAR_PROVINCE, ids[p]) for p in sorted(expected)]
        else:
            raise ValueError(f"census_age_sex does not support level {place.level}")
        result = await self.tempus.table(t["census_age_sex"], last_n=last_n, filters=filters, ttl_s=TTL)
        check_units(result, expected)
        return result

    async def padron(self, municipality_code: str) -> TableResult:
        vid = census_value_id(municipality_code)
        filters = [(VAR_MUNICIPALITY, vid)] if vid else None
        return await self.tempus.table(tables()["padron_municipal"], last_n=1, filters=filters, ttl_s=TTL)

    async def adrh(
        self,
        kind: AdrhKind,
        place: Place,
        *,
        last_n: int = 1,
        whole_province: bool = False,
    ) -> TableResult:
        """Income atlas table for a place.

        whole_province=True returns every municipality, district and tract in the
        province (cached, a few MB), which is how sub-areas are listed.
        """
        t = tables()
        if place.level in ("ccaa", "province") and not whole_province:
            return await self.tempus.table(t["adrh_aggregates"][kind], last_n=last_n, ttl_s=TTL)
        prov = place.province_code
        if prov is None or prov not in t["adrh_by_province"]:
            raise DataError(
                problem=f"No income atlas table for {place.name}.",
                cause="This province is missing from the bundled table map.",
                fix="Rebuild reference data with scripts/build_reference.py.",
            )
        table_id = t["adrh_by_province"][prov][kind]
        if place.level == "municipality" and not whole_province:
            vid = census_value_id(place.code)
            if vid:
                return await self.tempus.table(
                    table_id, last_n=last_n, filters=[(VAR_MUNICIPALITY, vid)], ttl_s=TTL
                )
        return await self.tempus.table(table_id, last_n=last_n, ttl_s=TTL)

    async def adrh_benchmarks(self, kind: AdrhKind, last_n: int = 1) -> TableResult:
        """Spain, CCAA and province values for comparison."""
        return await self.tempus.table(tables()["adrh_aggregates"][kind], last_n=last_n, ttl_s=TTL)


def check_units(result: TableResult, expected: set[str]) -> None:
    """Fail loudly if INE returned data for other places than asked (wrong value id)."""
    got = {u[0] for s in result.series if (u := unit_code(s))}
    if got != expected:
        raise DataError(
            problem="INE returned data for different places than requested.",
            cause=f"Asked for {sorted(expected)}, got {sorted(got)[:5]} from table {result.table_id}.",
            fix="The bundled value-id map is out of date; rebuild it with scripts/build_reference.py.",
        )


def unit_code(series: Series) -> tuple[str, str] | None:
    """(INE code, name) of the territorial unit a series describes, at any level."""
    for var in ("Secciones", "Distritos", "Municipios", "Provincias", "Comunidades y Ciudades Autónomas"):
        if var in series.dims:
            code, name = series.dims[var]
            if var == "Comunidades y Ciudades Autónomas" and code:
                code = f"CA{code}"
            return code, name
    for var, (_code, name) in series.dims.items():
        if var.lower().startswith("total nacional") or name == "Total Nacional":
            return "ES", "España"
    return None
