"""businesses: how many businesses of a kind an area has, per resident and per km².

Counts come from the monthly OpenStreetMap snapshot (sources/osm). OpenStreetMap is
mapped by volunteers, so every count is a lower bound; where INE's company directory
(DIRCE) counts the same activity, the result says how complete the map looks in that
region.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from ..core.envelope import Check, Confidence, Envelope, Source, confidence_from, dedupe_sources
from ..core.errors import DataError
from ..core.text import quoted
from ..places.codes import PROVINCES
from ..places.resolver import Place, place_from_code, population_in
from ..sources.ine.adrh import UnitValues, by_unit
from ..sources.ine.datasets import ADRH_NAME, DIRCE_NAME, IneData, latest_year, register_source, source_for
from ..sources.osm import snapshot as osm
from ..sources.osm.categories import BY_KEY, BY_TAG, CATEGORIES, SECTORS, Category, find_category
from .common import fmt_int, fmt_pct, pct_diff, resolve_place_arg, to_result

SubAreaLevel = Literal["none", "district", "tract"]
RankBy = Literal["count", "per_10k_residents", "per_km2"]
MAX_SUB_AREAS = 60
STALE_DAYS = 45
THIN_COUNT = 10
# OSM places per DIRCE company below which the map is likely missing many businesses.
COVERAGE_LOW = 0.4
COVERAGE_PARTIAL = 0.7


class AreaCount(BaseModel):
    code: str
    name: str
    level: str
    count: int
    population: int | None = None
    population_year: int | None = None
    per_10k_residents: float | None = None
    area_km2: float | None = None
    per_km2: float | None = None


class CategoryCount(BaseModel):
    category: str
    label: str
    count: int
    per_10k_residents: float | None = None


class BrandCount(BaseModel):
    brand: str
    count: int
    share_pct: float


class Coverage(BaseModel):
    """OSM places vs DIRCE companies for the same activity in the place's region."""

    region_code: str
    region_name: str
    osm_categories: list[str]
    osm_count: int
    cnae: str
    cnae_label: str
    dirce_companies: int
    dirce_date: str
    ratio: float = Field(description="OSM places per DIRCE company. Near or above 1 suggests good coverage.")


class BusinessesData(BaseModel):
    query: str = Field(description="What was counted: a category, a sector, OSM tags, or all businesses.")
    categories: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    osm_date: str
    area: AreaCount
    benchmarks: list[AreaCount] = Field(default_factory=list)
    by_category: list[CategoryCount] = Field(default_factory=list)
    top_brands: list[BrandCount] = Field(default_factory=list)
    branded_share_pct: float | None = Field(default=None, description="Share of places tagged with a brand.")
    coverage: Coverage | None = None
    sub_area_level: SubAreaLevel = "none"
    sub_area_rank_by: RankBy = "count"
    sub_areas: list[AreaCount] = Field(default_factory=list)
    sub_areas_total: int = 0


@dataclass(frozen=True)
class _What:
    label: str
    selection: osm.Selection
    categories: list[Category]
    tags: list[str]
    listed: list[Category]  # categories to break the count down by


def resolve_what(category: str | None) -> _What:
    """Turn the `category` argument into a selection of places."""
    text = (category or "").strip()
    if text.lower() in ("", "all", "all businesses", "any", "todos", "todas"):
        listed = [c for c in CATEGORIES if c.in_total]
        return _What(
            label="all businesses",
            selection=osm.Selection.all_businesses(),
            categories=[],
            tags=[],
            listed=listed,
        )
    sector = text.lower().replace(" ", "_").replace("and_", "")
    if sector in SECTORS:
        cats = [c for c in CATEGORIES if c.sector == sector]
        return _What(
            label=f"{sector.replace('_', ' ')} sector",
            selection=osm.Selection.categories([c.key for c in cats]),
            categories=cats,
            tags=[],
            listed=cats,
        )
    if "=" in text:
        tags = [t.strip() for t in text.split(",") if t.strip()]
        bad = [t for t in tags if t.count("=") != 1 or not all(t.split("="))]
        if bad:
            raise DataError(
                problem=f"'{', '.join(bad)}' is not an OpenStreetMap tag.",
                cause="Tags are written key=value.",
                fix="Use e.g. 'shop=bakery' or 'amenity=restaurant,amenity=fast_food'.",
            )
        cats = list({BY_TAG[t].key: BY_TAG[t] for t in tags if t in BY_TAG}.values())
        return _What(
            label=", ".join(tags), selection=osm.Selection.tags(tags), categories=cats, tags=tags, listed=[]
        )
    cat = find_category(text)
    if cat is None:
        raise DataError(
            problem=f"Unknown business category '{text}'.",
            cause="It is not one of this server's categories or a word it knows for one.",
            fix=f"Use a category ({', '.join(BY_KEY)}), a sector ({', '.join(SECTORS)}), "
            "or an OpenStreetMap tag such as 'shop=bakery'.",
        )
    return _What(
        label=cat.label.lower(),
        selection=osm.Selection.categories([cat.key]),
        categories=[cat],
        tags=list(cat.tags),
        listed=[],
    )


def area_of(p: Place) -> osm.Area:
    if p.level == "ccaa":
        return osm.Area(tuple(code for code, (_n, ccaa, _a) in PROVINCES.items() if f"CA{ccaa}" == p.code))
    return osm.Area((p.code,))


def _rate(count: int, population: int | None) -> float | None:
    return round(count / population * 10_000, 2) if population else None


def _density(count: int, km2: float | None) -> float | None:
    return round(count / km2, 2) if km2 else None


def _counted(
    snap: osm.Snapshot,
    sel: osm.Selection,
    code: str,
    name: str,
    level: str,
    area: osm.Area,
    population: int | None,
    year: int | None,
) -> AreaCount:
    n = snap.count(area, sel)
    km2 = snap.area_km2(area)
    return AreaCount(
        code=code,
        name=name,
        level=level,
        count=n,
        population=population,
        population_year=year,
        per_10k_residents=_rate(n, population),
        area_km2=round(km2, 2) if km2 else None,
        per_km2=_density(n, km2),
    )


def _padron_year(date: str) -> int | None:
    return int(date[:4]) if date[:4].isdigit() else None


def osm_sources(snap: osm.Snapshot) -> list[Source]:
    built = datetime.fromisoformat(snap.meta["built_at"])
    return [
        Source(
            publisher="OpenStreetMap contributors (extract by Geofabrik)",
            dataset="OpenStreetMap shops and services, monthly snapshot",
            dataset_id=f"osm-snapshot {snap.osm_date:%Y-%m-%d}",
            url=osm.RELEASE_PAGE,
            licence=osm.LICENCE,
            attribution=osm.ATTRIBUTION,
            data_period=f"{snap.osm_date:%Y-%m-%d}",
            retrieved_at=built,
        ),
        Source(
            publisher="INE (Instituto Nacional de Estadística)",
            dataset=f"Cartografía digitalizada de secciones censales {snap.tracts_year}",
            dataset_id=f"seccionado_{snap.tracts_year}",
            url=snap.meta["tracts_url"],
            licence="CC BY 4.0",
            attribution="Fuente: INE. Elaboración propia con datos extraídos del sitio web del INE: www.ine.es",
            data_period=f"{snap.tracts_year}-01-01",
            retrieved_at=built,
        ),
    ]


async def _coverage(
    ine: IneData, snap: osm.Snapshot, p: Place, what: _What
) -> tuple[Coverage | None, Source | None]:
    cnaes = {c.cnae for c in what.categories}
    if len(cnaes) != 1 or None in cnaes or not what.categories:
        return None, None
    cnae = next(iter(cnaes))
    assert cnae is not None
    family = [c for c in CATEGORIES if c.cnae == cnae]
    region = p.code if p.level == "ccaa" else p.ccaa_code
    if not region:
        return None, None
    region_place = place_from_code(region)
    result, by_cnae = await ine.dirce_companies(region)
    companies = by_cnae.get(cnae)
    if not companies:
        return None, None
    osm_n = snap.count(area_of(region_place), osm.Selection.categories([c.key for c in family]))
    year = latest_year(result.series)
    date = f"{year}-01-01" if year else "latest"
    cov = Coverage(
        region_code=region,
        region_name=region_place.name,
        osm_categories=[c.key for c in family],
        osm_count=osm_n,
        cnae=cnae,
        cnae_label=family[0].cnae_label or "",
        dirce_companies=companies,
        dirce_date=date,
        ratio=round(osm_n / companies, 2),
    )
    return cov, source_for(result, DIRCE_NAME, date)


def _coverage_check(cov: Coverage) -> Check:
    names = ", ".join(BY_KEY[k].label.lower() for k in cov.osm_categories)
    base = (
        f"In {cov.region_name}, OpenStreetMap has {fmt_int(cov.osm_count)} places ({names}) against "
        f"{fmt_int(cov.dirce_companies)} companies in INE's company directory for CNAE {cov.cnae} "
        f"({cov.cnae_label}, {cov.dirce_date}): {cov.ratio:.2f} places per company."
    )
    if cov.ratio < COVERAGE_LOW:
        return Check(
            kind="cross_check",
            severity="warning",
            message=base + " The map is likely missing most of these businesses here; treat counts as a "
            "small lower bound and use them only to compare areas.",
        )
    if cov.ratio < COVERAGE_PARTIAL:
        return Check(
            kind="cross_check",
            severity="warning",
            message=base + " The map likely misses a sizeable share; treat counts as lower bounds.",
        )
    return Check(
        kind="cross_check",
        severity="info",
        message=base + " Coverage looks reasonable. Companies and premises differ: one company can run "
        "several premises, and DIRCE places a company where it is registered.",
    )


async def businesses(
    ine: IneData,
    store: osm.SnapshotStore,
    place: str,
    category: str | None = None,
    sub_areas: SubAreaLevel = "none",
    rank_by: RankBy = "count",
) -> Envelope[BusinessesData]:
    p, checks = resolve_place_arg(place)
    what = resolve_what(category)
    snap = await store.get()
    sel = what.selection
    sources: list[Source] = osm_sources(snap)
    area = area_of(p)

    # Population: the bundled Padrón for municipalities and above; the income atlas
    # (INE's only source by district and tract) below, and for sub-areas.
    adrh_units: dict[str, UnitValues] = {}
    adrh_year: int | None = None
    if p.level in ("district", "tract") or (sub_areas != "none" and p.level == "municipality"):
        adrh = await ine.adrh("demographics", p, whole_province=True)
        adrh_units = by_unit(adrh.series)
        adrh_year = latest_year(adrh.series)
        sources.append(source_for(adrh, ADRH_NAME + ": indicadores demográficos", str(adrh_year)))

    def adrh_population(code: str) -> int | None:
        u = adrh_units.get(code)
        v = u.value("population", adrh_year) if u and adrh_year else None
        return int(v) if v else None

    if p.level in ("district", "tract"):
        pop, pop_year = adrh_population(p.code), adrh_year
    else:
        pop, date = population_in(p.code)
        pop_year = _padron_year(date)
        sources.append(register_source())
    main = _counted(snap, sel, p.code, p.name, p.level, area, pop, pop_year)

    # Benchmarks: the municipality, province, region and Spain around the place.
    bench: list[AreaCount] = []
    levels: list[tuple[str, str]] = []
    if p.level in ("district", "tract") and p.municipality_code:
        levels.append((p.municipality_code, "municipality"))
    if p.level in ("municipality", "district", "tract") and p.province_code:
        levels.append((p.province_code, "province"))
    if p.level != "ccaa" and p.ccaa_code:
        single = sum(1 for _c, (_n, ccaa, _a) in PROVINCES.items() if f"CA{ccaa}" == p.ccaa_code) == 1
        if not single:
            levels.append((p.ccaa_code, "ccaa"))
    for code, level in levels:
        bp = place_from_code(code)
        bpop, bdate = population_in(code)
        bench.append(_counted(snap, sel, code, bp.name, level, area_of(bp), bpop, _padron_year(bdate)))
    es_pop, es_date = population_in("")
    bench.append(_counted(snap, sel, "ES", "España", "country", osm.Area(()), es_pop, _padron_year(es_date)))
    sources.append(register_source())

    # Breakdown by category, for 'all businesses' and sectors.
    by_category: list[CategoryCount] = []
    if what.listed:
        counts = snap.count_by_category(area)
        for c in what.listed:
            n = counts.get(c.key, 0)
            if n:
                by_category.append(
                    CategoryCount(category=c.key, label=c.label, count=n, per_10k_residents=_rate(n, pop))
                )
        if not what.categories and counts.get(None):
            n = counts[None]
            by_category.append(
                CategoryCount(
                    category="other",
                    label="Other shops, offices and crafts",
                    count=n,
                    per_10k_residents=_rate(n, pop),
                )
            )
        by_category.sort(key=lambda c: -c.count)

    brands, branded = snap.brands(area, sel, limit=8)
    top_brands = [
        BrandCount(brand=b, count=n, share_pct=round(n / main.count * 100, 1))
        for b, n in brands
        if main.count
    ]

    # Sub-areas.
    subs: list[AreaCount] = []
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
            length = 7 if sub_areas == "district" else 10
            counts = snap.count_by_unit(area, length, sel)
            areas = snap.area_km2_by_unit(area, length)
            for code, km2 in areas.items():
                n = counts.get(code, 0)
                u = adrh_units.get(code)
                spop = adrh_population(code)
                subs.append(
                    AreaCount(
                        code=code,
                        name=u.name if u else place_from_code(code).name,
                        level=sub_areas,
                        count=n,
                        population=spop,
                        population_year=adrh_year if spop else None,
                        per_10k_residents=_rate(n, spop),
                        area_km2=round(km2, 3),
                        per_km2=_density(n, km2),
                    )
                )
            total_subs = len(subs)
            subs.sort(key=lambda s: (getattr(s, rank_by) is None, -(getattr(s, rank_by) or 0), s.code))
            if total_subs > MAX_SUB_AREAS:
                half = MAX_SUB_AREAS // 2
                ranked = [s for s in subs if getattr(s, rank_by) is not None]
                subs = ranked[:half] + ranked[-half:]
                checks.append(
                    Check(
                        kind="scope",
                        severity="info",
                        message=f"{total_subs} {sub_areas}s; showing the top {half} and bottom {half} by {rank_by}.",
                    )
                )
            if sub_areas == "tract" or adrh_year:
                missing = sum(1 for s in subs if s.population is None)
                checks.append(
                    Check(
                        kind="scope",
                        severity="info",
                        message=f"Places are assigned to INE's {snap.tracts_year} {sub_areas}s; residents come from the "
                        f"{adrh_year} income atlas, which uses that year's boundaries."
                        + (
                            f" {missing} {sub_areas}s shown have no resident figure (redrawn since)."
                            if missing
                            else ""
                        ),
                    )
                )

    # Cross-check against INE's company directory.
    coverage: Coverage | None = None
    try:
        coverage, dirce_source = await _coverage(ine, snap, p, what)
    except DataError as e:
        checks.append(
            Check(
                kind="cross_check",
                severity="info",
                message=f"The company-directory cross-check was skipped: {e.problem}",
            )
        )
    else:
        if coverage and dirce_source:
            sources.append(dirce_source)
            checks.append(_coverage_check(coverage))

    # Checks that apply to every OSM count.
    checks.append(
        Check(
            kind="scope",
            severity="info",
            message=f"Counts are places mapped in OpenStreetMap on {snap.osm_date:%Y-%m-%d}. Volunteers map them, so "
            "counts are lower bounds and completeness varies between towns; compare areas of similar size and "
            "type. Each place is counted once, by its main tag.",
        )
    )
    age = (datetime.now(UTC) - snap.osm_date).days
    if age > STALE_DAYS:
        checks.append(
            Check(
                kind="stale",
                severity="warning",
                message=f"The OpenStreetMap snapshot is {age} days old; openings and closures since are missing.",
            )
        )
    if snap.stale_reason:
        checks.append(
            Check(kind="stale", severity="info", message=f"Using the local snapshot: {snap.stale_reason}.")
        )
    if main.count < THIN_COUNT:
        checks.append(
            Check(
                kind="thin_sample",
                severity="info",
                message=f"Only {main.count} places: one opening or closure moves the rates a lot.",
            )
        )
    if p.level in ("district", "tract") and adrh_year:
        checks.append(
            Check(
                kind="stale",
                severity="info",
                message=f"Residents for this {p.level} are from the {adrh_year} income atlas.",
            )
        )

    floor: Confidence = "medium"  # volunteer-mapped data is never more than medium
    if coverage and coverage.ratio < COVERAGE_LOW:
        floor = "low"
    data = BusinessesData(
        query=what.label,
        categories=[c.key for c in what.categories],
        tags=what.tags,
        osm_date=f"{snap.osm_date:%Y-%m-%d}",
        area=main,
        benchmarks=bench,
        by_category=by_category,
        top_brands=top_brands,
        branded_share_pct=round(branded / main.count * 100, 1) if main.count else None,
        coverage=coverage,
        sub_area_level=sub_areas,
        sub_area_rank_by=rank_by,
        sub_areas=subs,
        sub_areas_total=total_subs,
    )
    return Envelope(
        data=data, sources=dedupe_sources(sources), checks=checks, confidence=confidence_from(checks, floor)
    )


def _fmt_rate(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{v:,.1f}" if v == 0 or abs(v) >= 1 else f"{v:.2f}"


def render(env: Envelope[BusinessesData]) -> str:
    d = env.data
    a = d.area
    lines = [f"Businesses: {d.query} in {a.name} ({a.level}, INE code {a.code}), OpenStreetMap {d.osm_date}"]
    pop = f" (population {fmt_int(a.population)}, {a.population_year})" if a.population else ""
    lines.append(
        f"Count: {fmt_int(a.count)}. Per 10,000 residents: {_fmt_rate(a.per_10k_residents)}{pop}. "
        f"Per km²: {_fmt_rate(a.per_km2)} ({_fmt_rate(a.area_km2)} km²)"
    )
    for b in d.benchmarks:
        diff = (
            f"; {a.name} is {pct_diff(a.per_10k_residents, b.per_10k_residents):+.1f}%"
            if a.per_10k_residents and b.per_10k_residents
            else ""
        )
        lines.append(
            f"Benchmark {b.name} ({b.level}): {fmt_int(b.count)}, {_fmt_rate(b.per_10k_residents)} per 10,000 residents{diff}"
        )
    if d.top_brands:
        lines.append(
            f"Brands: {fmt_pct(d.branded_share_pct)} of places carry a brand. Top: "
            + ", ".join(f"{quoted(b.brand)} {b.count}" for b in d.top_brands)
        )
    if d.by_category:
        lines.append("")
        lines.append("By category (count, per 10,000 residents):")
        for c in d.by_category:
            lines.append(f"- {c.label} ({c.category}): {fmt_int(c.count)}, {_fmt_rate(c.per_10k_residents)}")
    if d.sub_areas:
        lines.append("")
        lines.append(
            f"{d.sub_area_level.capitalize()}s ({len(d.sub_areas)} of {d.sub_areas_total}, by {d.sub_area_rank_by}):"
        )
        lines.append("code | name | count | residents | per 10,000 residents | per km²")
        for s in d.sub_areas:
            lines.append(
                f"{s.code} | {s.name} | {fmt_int(s.count)} | {fmt_int(s.population)} | "
                f"{_fmt_rate(s.per_10k_residents)} | {_fmt_rate(s.per_km2)}"
            )
    return "\n".join(lines)


async def businesses_tool(
    ine: IneData,
    store: osm.SnapshotStore,
    place: str,
    category: str | None = None,
    sub_areas: SubAreaLevel = "none",
    rank_by: RankBy = "count",
):
    env = await businesses(ine, store, place, category, sub_areas, rank_by)
    return to_result(env, render(env))
