"""The INE tables this server uses, and how to fetch each for a place.

Table ids live in data/ine_tables.json (built by scripts/build_reference.py), so a
renumbering by INE is fixed by re-running the script, not by editing code.
"""

from __future__ import annotations

import json
import re
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
from .epf import Epf, parse_epf
from .tempus import ATTRIBUTION, LICENCE, TABLE_PAGE, Series, TableResult, TempusClient
from .tourism import (
    Tourism,
    parse_daily_spend,
    parse_flat_beds,
    parse_food_share,
    parse_points,
    parse_province_nights,
)

# Tempus variable ids (stable across tables)
VAR_PROVINCE = 115
VAR_MUNICIPALITY = 19
VAR_SEX = 18
VAR_CCAA = 70
VAR_NATIONAL = 349

AdrhKind = Literal["income", "demographics", "inequality", "income_sources"]

ADRH_NAME = "Atlas de Distribución de Renta de los Hogares (ADRH)"
CENSUS_NAME = "Censo anual de población: población por sexo y edad"
PADRON_NAME = "Cifras oficiales de población de los municipios (Padrón)"
EPF_NAME = "Encuesta de Presupuestos Familiares (EPF): gasto de los hogares"
TOURISM_NAME = "Estadísticas de turismo (EOH, viviendas turísticas, EGATUR, ETR)"
DIRCE_NAME = "Directorio Central de Empresas (DIRCE): empresas por comunidad autónoma y actividad principal"

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

    async def dirce_companies(self, region: str) -> tuple[TableResult, dict[str, int]]:
        """Companies on 1 January by CNAE code ('56', '561', ...) for a CCAA ('CA07') or 'ES'.

        DIRCE counts companies (including the self-employed) by the region of their
        registered office, not premises: a chain counts once.
        """
        t = tables()
        var = VAR_NATIONAL if region == "ES" else VAR_CCAA
        filters = [(var, t["dirce_region_value_ids"][region])]
        filters += [(v, i) for v, i in t["dirce_total_filters"]]
        result = await self.tempus.table(t["dirce_companies"], last_n=1, filters=filters, ttl_s=TTL)
        check_units(result, {region})
        return result, dirce_by_cnae(result.series)

    async def epf(self) -> tuple[Epf, list[TableResult]]:
        """Household spending by COICOP code, by income band, and coefficients of variation.

        Three survey years, all regions; a few MB, refreshed monthly at most.
        """
        t = tables()["epf"]
        spend = await self.tempus.table(t["spend_by_code"], last_n=3, ttl_s=30 * DAY)
        income = await self.tempus.table(t["spend_by_income"], last_n=1, ttl_s=30 * DAY)
        cv = await self.tempus.table(t["cv_by_subgroup"], last_n=1, ttl_s=30 * DAY)
        return parse_epf(spend.series, income.series, cv.series), [spend, income, cv]

    async def cpi_national(self) -> TableResult:
        """National CPI, general and groups, 2025 base (from January 2025)."""
        return await self.tempus.table(tables()["cpi"]["national"], last_n=24, ttl_s=DAY)

    async def cpi_groups_since_2002(self) -> TableResult:
        """National CPI by ECOICOP group on the 2025 base, monthly since January 2002."""
        return await self.tempus.table(tables()["cpi"]["national_groups_since_2002"], last_n=400, ttl_s=DAY)

    async def cpi_province(self, province: str) -> TableResult:
        t = tables()["cpi"]
        vid = t["province_value_ids"][province]
        result = await self.tempus.table(t["provincial"], last_n=2, filters=[(VAR_PROVINCE, vid)], ttl_s=DAY)
        check_units(result, {province})
        return result

    async def tourism(self) -> tuple[Tourism, list[TableResult]]:
        """Hotel nights by tourist point, tourist-flat beds and visitor spend (no provinces yet;
        add them with tourism_province)."""
        t = tables()["tourism"]
        month = 30 * DAY
        points = await self.tempus.table(t["hotel_nights_points"], last_n=12, ttl_s=7 * DAY)
        flats = await self.tempus.table(t["tourist_flats_municipalities"], last_n=1, ttl_s=month)
        foreign = await self.tempus.table(t["foreign_spend_by_region"], last_n=12, ttl_s=month)
        items = await self.tempus.table(t["foreign_spend_by_item"], last_n=12, ttl_s=month)
        domestic = await self.tempus.table(t["domestic_trips_by_destination"], last_n=1, ttl_s=month)
        tour = Tourism(
            point_nights=parse_points(points.series, t["point_municipalities"]),
            flat_beds=parse_flat_beds(flats.series),
            foreign_daily=parse_daily_spend(foreign.series),
            domestic_daily=parse_daily_spend(domestic.series),
            foreign_food_share=parse_food_share(items.series),
        )
        return tour, [points, flats, foreign, items, domestic]

    async def tourism_province(self, tour: Tourism, province: str) -> TableResult | None:
        """Add a province's hotel nights (last 12 months) to `tour`, once."""
        if province in tour.province_nights:
            return None
        t = tables()["tourism"]
        vid = tables()["census_province_value_ids"][province]
        res = await self.tempus.table(
            t["hotel_nights_provinces"], last_n=12, filters=[(VAR_PROVINCE, vid)], ttl_s=7 * DAY
        )
        tour.province_nights[province] = parse_province_nights(res.series, province)
        return res

    async def adrh_benchmarks(self, kind: AdrhKind, last_n: int = 1) -> TableResult:
        """Spain, CCAA and province values for comparison."""
        return await self.tempus.table(tables()["adrh_aggregates"][kind], last_n=last_n, ttl_s=TTL)


_CNAE_IN_NAME = re.compile(r"\.\s(\d{2,4})\s")


def dirce_by_cnae(series: list[Series]) -> dict[str, int]:
    """CNAE code -> latest company count. The code is only in the series name ('... 561 Restaurantes')."""
    out: dict[str, int] = {}
    for s in series:
        m = _CNAE_IN_NAME.search(s.name)
        p = s.latest()
        if m and p and p.value is not None:
            out[m.group(1)] = int(p.value)
    return out


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
