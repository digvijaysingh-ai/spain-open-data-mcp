"""demographics: population, age structure, households, and sub-area breakdowns."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from ..core.envelope import Check, Envelope, Source, confidence_from, dedupe_sources
from ..places.resolver import Place
from ..sources.ine.adrh import UnitValues, by_unit
from ..sources.ine.datasets import (
    ADRH_NAME,
    CENSUS_NAME,
    PADRON_NAME,
    IneData,
    latest_year,
    source_for,
)
from ..sources.ine.tempus import Series
from .common import fmt_int, fmt_pct, pct_diff, resolve_place_arg, to_result

SubAreaLevel = Literal["none", "district", "tract"]
THIN_POPULATION = 5_000
MAX_SUB_AREAS = 60


class AgeBand(BaseModel):
    band: str = Field(description="e.g. '0-4', '100+'")
    min_age: int
    max_age: int | None
    population: int | None
    share_pct: float | None


class YearValue(BaseModel):
    year: int
    value: float


class HouseholdProfile(BaseModel):
    """From the income atlas (ADRH); available down to census tract."""

    year: int | None = None
    population: int | None = None
    mean_age: float | None = None
    pct_under_18: float | None = None
    pct_65_plus: float | None = None
    avg_household_size: float | None = None
    pct_single_person_households: float | None = None
    pct_spanish_nationality: float | None = None
    estimated_households: int | None = Field(
        default=None, description="population / average household size. An estimate."
    )


class AreaDemographics(BaseModel):
    place: Place
    population: int | None = Field(
        default=None, description="Census population on 1 January of `population_year`."
    )
    population_year: int | None = None
    official_population: int | None = Field(
        default=None, description="Official Padrón figure for municipalities (the legal population)."
    )
    male: int | None = None
    female: int | None = None
    age_bands: list[AgeBand] = Field(default_factory=list)
    pct_age_0_4: float | None = None
    pct_age_0_14: float | None = None
    pct_age_15_64: float | None = None
    pct_age_65_plus: float | None = None
    population_trend: list[YearValue] = Field(default_factory=list)
    growth_pct: float | None = Field(
        default=None, description="Population change over `population_trend`, in %."
    )
    households: HouseholdProfile | None = None


class SubArea(BaseModel):
    code: str
    name: str
    level: str
    profile: HouseholdProfile


class DemographicsData(BaseModel):
    area: AreaDemographics
    sub_area_level: SubAreaLevel = "none"
    sub_areas: list[SubArea] = Field(default_factory=list)
    sub_areas_total: int = 0


_BAND_RE = re.compile(r"De (\d+) a (\d+) años|(\d+) (?:y|o) más años")


def _parse_band(name: str) -> tuple[str, int, int | None] | None:
    m = _BAND_RE.search(name)
    if not m:
        return None
    if m.group(1):
        lo, hi = int(m.group(1)), int(m.group(2))
        return f"{lo}-{hi}", lo, hi
    lo = int(m.group(3))
    return f"{lo}+", lo, None


def _age_name(s: Series) -> str:
    for var, (_c, name) in s.dims.items():
        if "edad" in var.lower():
            return name
    return ""


def census_summary(series: list[Series], area: AreaDemographics) -> None:
    """Fill totals, sex split, age bands and trend from census series (summed if several units)."""
    year = latest_year(series)
    if year is None:
        return
    area.population_year = year
    totals: dict[int, float] = {}
    bands: dict[tuple[str, int, int | None], float] = {}
    sex_tot: dict[str, float] = {}
    for s in series:
        sex = s.dim_name("Sexo") or "Total"
        age = _age_name(s)
        for p in s.points:
            if p.value is None:
                continue
            if age == "Todas las edades":
                if sex == "Total":
                    totals[p.year] = totals.get(p.year, 0) + p.value
                elif p.year == year:
                    sex_tot[sex] = sex_tot.get(sex, 0) + p.value
            elif sex == "Total" and p.year == year:
                band = _parse_band(age)
                if band:
                    bands[band] = bands.get(band, 0) + p.value
    total = totals.get(year)
    area.population = int(total) if total is not None else None
    area.male = int(sex_tot["Hombres"]) if "Hombres" in sex_tot else None
    area.female = int(sex_tot["Mujeres"]) if "Mujeres" in sex_tot else None
    area.population_trend = [YearValue(year=y, value=v) for y, v in sorted(totals.items())]
    if len(area.population_trend) >= 2:
        first, last = area.population_trend[0].value, area.population_trend[-1].value
        area.growth_pct = round(pct_diff(last, first), 2)
    if total:
        ordered = sorted(bands.items(), key=lambda kv: kv[0][1])
        area.age_bands = [
            AgeBand(band=b, min_age=lo, max_age=hi, population=int(v), share_pct=round(v / total * 100, 2))
            for (b, lo, hi), v in ordered
        ]

        def share(lo: int, hi: int | None) -> float:
            return round(
                sum(v for (_b, a, _h), v in bands.items() if a >= lo and (hi is None or a <= hi))
                / total
                * 100,
                2,
            )

        area.pct_age_0_4 = share(0, 4)
        area.pct_age_0_14 = share(0, 14)
        area.pct_age_15_64 = share(15, 64)
        area.pct_age_65_plus = share(65, None)


def household_profile(u: UnitValues) -> HouseholdProfile:
    year = max((y for ys in u.values.values() for y in ys), default=None)
    if year is None:
        return HouseholdProfile()

    def v(key: str) -> float | None:
        return u.value(key, year)

    pop, size = v("population"), v("avg_household_size")
    return HouseholdProfile(
        year=year,
        population=int(pop) if pop is not None else None,
        mean_age=v("mean_age"),
        pct_under_18=v("pct_under_18"),
        pct_65_plus=v("pct_65_plus"),
        avg_household_size=size,
        pct_single_person_households=v("pct_single_person_households"),
        pct_spanish_nationality=v("pct_spanish_nationality"),
        estimated_households=round(pop / size) if pop and size else None,
    )


async def demographics(
    ine: IneData, place: str, sub_areas: SubAreaLevel = "none"
) -> Envelope[DemographicsData]:
    p, checks = resolve_place_arg(place)
    sources: list[Source] = []
    area = AreaDemographics(place=p)

    if p.level in ("municipality", "province", "ccaa"):
        census = await ine.census_age_sex(p, last_n=5)
        census_summary(census.series, area)
        sources.append(source_for(census, CENSUS_NAME, f"{area.population_year}-01-01"))

    if p.level == "municipality":
        padron = await ine.padron(p.code)
        official = next(
            (
                s.latest()
                for s in padron.series
                if s.dim_code("Municipios") == p.code and s.dim_name("Sexo") == "Total"
            ),
            None,
        )
        if official and official.value is not None:
            area.official_population = int(official.value)
            sources.append(source_for(padron, PADRON_NAME, official.date))
            if area.population:
                diff = pct_diff(area.population, area.official_population)
                checks.append(
                    Check(
                        kind="cross_check",
                        severity="info" if abs(diff) < 2 else "warning",
                        message=f"Census population {fmt_int(area.population)} vs official Padrón "
                        f"{fmt_int(area.official_population)} ({diff:+.1f}%). Both are INE figures for "
                        "1 January; the Padrón is the legal count, the census is register-based and "
                        "corrected for over-registration.",
                    )
                )

    # Household profile from the income atlas, for the area itself and its sub-areas.
    if p.level in ("municipality", "district", "tract") or sub_areas != "none":
        whole = sub_areas != "none" or p.level in ("district", "tract")
        adrh = await ine.adrh("demographics", p, whole_province=whole)
        units = by_unit(adrh.series)
        if p.code in units:
            area.households = household_profile(units[p.code])
            if p.level in ("district", "tract") and area.households.population:
                area.population = area.households.population
                area.population_year = area.households.year
        year = latest_year(adrh.series)
        sources.append(source_for(adrh, ADRH_NAME + ": indicadores demográficos", str(year)))
        result_subs: list[SubArea] = []
        total_subs = 0
        if sub_areas != "none":
            if p.level != "municipality":
                checks.append(
                    Check(
                        kind="scope",
                        severity="warning",
                        message="Sub-areas are listed for municipalities only; pass a municipality.",
                    )
                )
            else:
                subs = [u for c, u in units.items() if u.level == sub_areas and c.startswith(p.code)]
                total_subs = len(subs)
                result_subs = [
                    SubArea(code=u.code, name=u.name, level=u.level, profile=household_profile(u))
                    for u in sorted(subs, key=lambda u: u.code)
                ][:MAX_SUB_AREAS]
                withheld = sum(1 for u in subs if any(u.suppressed.values()))
                if withheld:
                    checks.append(
                        Check(
                            kind="suppressed",
                            severity="info",
                            message=f"INE withheld some values for {withheld} of {total_subs} {sub_areas}s "
                            "(statistical secrecy for small units).",
                        )
                    )
                if total_subs > MAX_SUB_AREAS:
                    checks.append(
                        Check(
                            kind="scope",
                            severity="info",
                            message=f"Showing the first {MAX_SUB_AREAS} of {total_subs} {sub_areas}s by code. "
                            "Use districts for a city-wide overview.",
                        )
                    )
                if sub_areas == "tract":
                    checks.append(
                        Check(
                            kind="scope",
                            severity="info",
                            message=f"Tract codes follow INE's {year} boundaries; INE redraws some tracts "
                            "every year.",
                        )
                    )
        if area.households and area.households.year and area.population_year:
            gap = area.population_year - area.households.year
            if gap >= 2:
                checks.append(
                    Check(
                        kind="stale",
                        severity="info",
                        message=f"Household profile is from {area.households.year} (latest income atlas "
                        f"release); population is {area.population_year}.",
                    )
                )
        data = DemographicsData(
            area=area, sub_area_level=sub_areas, sub_areas=result_subs, sub_areas_total=total_subs
        )
    else:
        data = DemographicsData(area=area)

    if area.population is not None and area.population < THIN_POPULATION:
        checks.append(
            Check(
                kind="thin_sample",
                severity="warning",
                message=f"Small population ({fmt_int(area.population)}); shares and rates move a lot "
                "year to year.",
            )
        )
    if area.households and area.households.estimated_households:
        checks.append(
            Check(
                kind="estimate",
                severity="info",
                message="estimated_households = population / average household size (income atlas).",
            )
        )
    return Envelope(
        data=data, sources=dedupe_sources(sources), checks=checks, confidence=confidence_from(checks)
    )


def render(env: Envelope[DemographicsData]) -> str:
    a = env.data.area
    p = a.place
    lines = [f"Demographics: {p.name} ({p.level}, INE code {p.code})"]
    if p.province and p.level != "province":
        lines[0] += f", {p.province}"
    if a.population is not None:
        lines.append(f"Population ({a.population_year}): {fmt_int(a.population)}")
    if a.official_population is not None:
        lines.append(f"Official Padrón population: {fmt_int(a.official_population)}")
    if a.male is not None and a.female is not None:
        lines.append(f"Men {fmt_int(a.male)}, women {fmt_int(a.female)}")
    if a.growth_pct is not None and a.population_trend:
        lines.append(
            f"Change {a.population_trend[0].year}-{a.population_trend[-1].year}: {a.growth_pct:+.1f}%"
        )
    if a.age_bands:
        lines.append(
            f"Age: 0-4 {fmt_pct(a.pct_age_0_4)}, 0-14 {fmt_pct(a.pct_age_0_14)}, "
            f"15-64 {fmt_pct(a.pct_age_15_64)}, 65+ {fmt_pct(a.pct_age_65_plus)}"
        )
        lines.append("Age bands: " + ", ".join(f"{b.band}: {fmt_int(b.population)}" for b in a.age_bands))
    h = a.households
    if h and h.year:
        lines.append(
            f"Households ({h.year}): average size {h.avg_household_size}, single-person "
            f"{fmt_pct(h.pct_single_person_households)}, about {fmt_int(h.estimated_households)} households; "
            f"mean age {h.mean_age}, under 18 {fmt_pct(h.pct_under_18)}, 65+ {fmt_pct(h.pct_65_plus)}"
        )
    if env.data.sub_areas:
        lines.append("")
        lines.append(
            f"{env.data.sub_area_level.capitalize()}s ({len(env.data.sub_areas)} of {env.data.sub_areas_total}):"
        )
        lines.append("code | name | population | mean age | under 18 | 65+ | household size")
        for s in env.data.sub_areas:
            q = s.profile
            lines.append(
                f"{s.code} | {s.name} | {fmt_int(q.population)} | {q.mean_age or 'n/a'} | "
                f"{fmt_pct(q.pct_under_18)} | {fmt_pct(q.pct_65_plus)} | {q.avg_household_size or 'n/a'}"
            )
    return "\n".join(lines)


async def demographics_tool(ine: IneData, place: str, sub_areas: SubAreaLevel = "none"):
    env = await demographics(ine, place, sub_areas)
    return to_result(env, render(env))
