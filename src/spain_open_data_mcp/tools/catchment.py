"""catchment: who lives within a radius of an address, what they earn, which businesses are
there, and (with a category) the market and opportunity score for that circle.

    address ──▶ geocode (checked) ──▶ point ──▶ tracts touching the circle, with the share of
    each tract's area inside it ──▶ residents, households and income from the income atlas,
    weighted by those shares ──▶ businesses mapped inside the circle ──▶ (category) market,
    supply and score, computed per region of each tract and summed

Residents are assumed to be spread evenly within a tract. Visitors are not counted here.
"""

from __future__ import annotations

import asyncio
import math
from collections import Counter, defaultdict

from pydantic import BaseModel, Field

from ..categories import BY_KEY, MARKET
from ..core.envelope import Check, Envelope, Source, confidence_from, dedupe_sources
from ..core.errors import DataError, PlaceNotFound, UpstreamError
from ..engine import model
from ..engine.estimate import Estimate
from ..engine.inputs import COMPLETENESS_CATEGORIES, MarketContext
from ..places.areas import population_of
from ..places.codes import PROVINCES
from ..places.resolver import place_from_code
from ..sources.geo import geocode, walk
from ..sources.geo.geometry import MAX_RADIUS_M, MIN_RADIUS_M, GeometryStore
from ..sources.geo.walk import WalkGraphs
from ..sources.ine.adrh import UnitValues, by_unit
from ..sources.ine.datasets import ADRH_NAME, IneData, latest_year, source_for
from ..sources.osm import snapshot as osm
from .businesses import osm_sources
from .common import fmt_eur, fmt_int, fmt_pct, to_result
from .market import AreaOpportunity, Range, engine_checks, resolve_market_category

THIN_RADIUS_M = 300
MIN_WALK_MIN, MAX_WALK_MIN = 3, 30
WIDE_RADIUS_M = 5_000


class CategoryCount(BaseModel):
    category: str
    label: str
    count: int


class CatchmentData(BaseModel):
    point: tuple[float, float] = Field(description="(lat, lon), rounded to about 100 m.")
    found: str = Field(description="What the address resolved to, without floor or door.")
    geocoder: str
    municipality: str | None
    radius_m: int = Field(description="Circle radius, or the reach used to find tracts for a walking area.")
    walk_minutes: int | None = None
    area_method: str = Field(description="'circle', 'walking network', or 'walking estimate (circle)'.")
    area_km2: float
    tracts: int
    residents: int
    households: int | None
    avg_household_size: float | None
    net_income_per_household: float | None = Field(description="Weighted by households, euros per year.")
    pct_65_plus: float | None
    pct_under_18: float | None
    income_year: int | None
    businesses: int = Field(description="Mapped businesses inside the circle.")
    by_category: list[CategoryCount]
    unplaced_share: float = Field(
        description="Share of the circle's tracts with no income-atlas figures (redrawn)."
    )
    market: AreaOpportunity | None = None
    method_version: str | None = None


async def _units(ine: IneData, prov: str, kind: str, sources: list[Source]) -> dict[str, UnitValues]:
    from ..places.resolver import Place

    p = Place(code=prov, level="province", name=prov, province_code=prov)
    res = await ine.adrh(kind, p, whole_province=True)  # type: ignore[arg-type]
    sources.append(source_for(res, f"{ADRH_NAME}: {kind}", str(latest_year(res.series))))
    return by_unit(res.series)


def _latest(u: UnitValues | None, key: str) -> float | None:
    found = u.latest(key) if u else None
    return found[1] if found else None


async def catchment(
    ine: IneData,
    store: osm.SnapshotStore,
    geo_store: GeometryStore,
    near: str,
    radius_m: int = 800,
    category: str | None = None,
    walk_minutes: int | None = None,
    walk_store: WalkGraphs | None = None,
) -> Envelope[CatchmentData]:
    if walk_minutes is not None and not MIN_WALK_MIN <= walk_minutes <= MAX_WALK_MIN:
        raise DataError(
            problem=f"walk_minutes {walk_minutes} is out of range.",
            cause=f"Walking catchments are {MIN_WALK_MIN} to {MAX_WALK_MIN} minutes.",
            fix="Use e.g. 5, 10 or 15.",
        )
    if not MIN_RADIUS_M <= radius_m <= MAX_RADIUS_M:
        raise DataError(
            problem=f"radius_m {radius_m} is out of range.",
            cause=f"Catchments are {MIN_RADIUS_M} to {MAX_RADIUS_M:,} metres.",
            fix="Use e.g. 500 for a street corner, 1,500 for a neighbourhood, 5,000 for a town.",
        )
    key = resolve_market_category(category) if category else None
    loc = await geocode.locate(near)
    geo = await geo_store.get()
    here = await asyncio.to_thread(geo.tract_at, loc.lon, loc.lat)
    if here is None:
        raise PlaceNotFound(
            problem=f"{loc.label} is not inside a Spanish census tract.",
            cause="The point is at sea or outside Spain.",
            fix="Check the coordinates or address.",
        )
    if loc.municipality_code and here[:5] != loc.municipality_code:
        raise PlaceNotFound(
            problem=f"The geocoder placed '{loc.label}' outside {place_from_code(loc.municipality_code).name}.",
            cause=f"The point falls in {place_from_code(here[:5]).name}.",
            fix="Check the address, or pass coordinates as 'lat, lon'.",
        )
    sources: list[Source] = []
    walk_note: Check | None = None
    shape = None  # walking area polygon, if any
    method, area_km2 = "circle", math.pi * radius_m**2 / 1e6
    if walk_minutes is not None:
        reach = walk_minutes * walk.WALK_M_PER_MIN
        try:
            graphs_store = walk_store or walk.WalkGraphs()
            provinces = await graphs_store.provinces_near(loc.lon, loc.lat, reach)
            if not provinces:
                raise UpstreamError(problem="no walking graph", cause="none published here", fix="")
            graphs = [await graphs_store.graph(p) for p in provinces]
            area = await asyncio.to_thread(walk.walk_area, graphs, loc.lon, loc.lat, walk_minutes)
            shape, method, area_km2, radius_m = area.shape, "walking network", area.area_km2, int(reach)
            walk_note = Check(
                kind="scope",
                severity="info",
                message=f"Walking area: {area.street_km:.1f} km of streets reachable in {walk_minutes} minutes at "
                f"{walk.WALK_M_PER_MIN * 60 / 1000:.1f} km/h, buffered by {walk.BUFFER_M:.0f} m (OpenStreetMap streets).",
            )
        except UpstreamError:
            radius_m = int(walk.fallback_radius_m(walk_minutes))
            method, area_km2 = "walking estimate (circle)", math.pi * radius_m**2 / 1e6
            walk_note = Check(
                kind="estimate",
                severity="warning",
                message=f"No walking graph available here yet: the area is a circle of {radius_m} m, about "
                f"{walk_minutes} minutes' walk allowing for streets not being straight.",
            )
    if shape is not None:
        shares = await asyncio.to_thread(geo.area_shares, shape, loc.lon, loc.lat, radius_m)
    else:
        shares = await asyncio.to_thread(geo.circle_shares, loc.lon, loc.lat, radius_m)
    checks: list[Check] = [
        Check(
            kind="scope",
            severity="info",
            message=f"Located with {loc.source}: {loc.label}. "
            f"Coordinates rounded to about 100 m; floor and door details are never sent or kept.",
        )
    ]

    demo: dict[str, UnitValues] = {}
    income: dict[str, UnitValues] = {}
    for prov in sorted({s.code[:2] for s in shares}):
        demo |= await _units(ine, prov, "demographics", sources)
        income |= await _units(ine, prov, "income", sources)

    residents = households = hh_income = 0.0
    weighted: dict[str, float] = defaultdict(float)
    by_region: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    unplaced = 0.0
    for t in shares:
        u = demo.get(t.code)
        pop, size = _latest(u, "population"), _latest(u, "avg_household_size")
        if not pop or not size:
            unplaced += t.share
            continue
        people, hh = pop * t.share, pop * t.share / size
        inc = _latest(income.get(t.code), "net_income_per_household")
        residents += people
        households += hh
        if inc:
            hh_income += inc * hh
            weighted["income_hh"] += hh
        for k in ("pct_65_plus", "pct_under_18"):
            v = _latest(u, k)
            if v is not None:
                weighted[k] += v * people
                weighted[k + "_n"] += people
        region = f"CA{PROVINCES[t.code[:2]][1]}"
        r = by_region[region]
        r["people"] += people
        r["households"] += hh
        if inc:
            r["income_x_hh"] += inc * hh
            r["income_hh"] += hh

    snap = await store.get()
    if shape is not None:
        places = await asyncio.to_thread(
            snap.within_shape, shape, loc.lon, loc.lat, radius_m, osm.Selection.all_businesses()
        )
    else:
        places = await asyncio.to_thread(
            snap.within, loc.lon, loc.lat, radius_m, osm.Selection.all_businesses()
        )
    counts = Counter(c for c, _b in places if c)
    by_category = [
        CategoryCount(category=k, label=BY_KEY[k].label, count=n)
        for k, n in counts.most_common(10)
        if k in BY_KEY
    ]
    sources += osm_sources(snap)

    def pct(k: str) -> float | None:
        return round(weighted[k] / weighted[k + "_n"], 1) if weighted.get(k + "_n") else None

    data = CatchmentData(
        point=(loc.lat, loc.lon),
        found=loc.label,
        geocoder=loc.source,
        municipality=place_from_code(here[:5]).name,
        radius_m=radius_m,
        walk_minutes=walk_minutes,
        area_method=method,
        area_km2=round(area_km2, 3),
        tracts=len(shares),
        residents=round(residents),
        households=round(households) if households else None,
        avg_household_size=round(residents / households, 2) if households else None,
        net_income_per_household=round(hh_income / weighted["income_hh"])
        if weighted.get("income_hh")
        else None,
        pct_65_plus=pct("pct_65_plus"),
        pct_under_18=pct("pct_under_18"),
        income_year=max((y for u in demo.values() for y in u.values.get("population", {})), default=None),
        businesses=len(places),
        by_category=by_category,
        unplaced_share=round(unplaced / len(shares), 3) if shares else 0.0,
    )

    if key:
        ctx = await MarketContext.create(ine, snap, key)
        main = max(by_region, key=lambda r: by_region[r]["people"]) if by_region else ""
        market_total: Estimate | None = None
        for region, r in by_region.items():
            if not r["households"]:
                continue
            spend, _notes = ctx.epf.spend_per_household(ctx.profile.ecoicop, region)
            monthly = r["income_x_hh"] / r["income_hh"] / 12 if r["income_hh"] else None
            ratio = ctx.epf.income_ratio(region, monthly)[0] if monthly else Estimate.exact(1.0)
            part = model.market(Estimate.around(r["households"], 0.03), spend, ratio, ctx.family_share())
            market_total = part if market_total is None else market_total + part
        mapped = sum(1 for c, _b in places if c == key)
        region_pop = population_of(main)[0] if main else 0
        cnae = ctx.category.cnae
        if cnae:
            fam = [c.key for c in BY_KEY.values() if c.cnae == cnae]
            inputs = model.SupplyInputs(
                osm_area=mapped,
                osm_region_family=ctx.osm_count(main, fam),
                osm_region_category=ctx.osm_count(main, [key]),
                companies_region=(await ctx.dirce(main)).get(cnae),
                population_share=residents / region_pop if region_pop else 0.0,
            )
        else:
            inputs = model.SupplyInputs(mapped, 0, 0, None, 0.0)
        supply, method = model.supply(inputs)
        density = model.density(market_total, supply) if market_total else None
        ref = await ctx.reference_density(main) if main else None
        score = model.score(density, ref) if density and ref and ref.base > 0 else None
        well_mapped = sum(1 for c, _b in places if c in COMPLETENESS_CATEGORIES)
        region_well = ctx.osm_count(main, list(COMPLETENESS_CATEGORIES)) if main else 0
        completeness = model.completeness(
            well_mapped / residents * 10_000 if residents else None,
            region_well / region_pop * 10_000 if region_pop else None,
        )
        too_small = residents < MARKET[key].min_population
        data.market = AreaOpportunity(
            code="catchment",
            name=(
                f"{walk_minutes} min walk from {loc.label}"
                if walk_minutes
                else f"{radius_m} m around {loc.label}"
            ),
            level="catchment",
            population=round(residents),
            households=Range.of(Estimate.around(households, 0.03)) if households else None,
            market_eur=Range.of(market_total),
            mapped_places=mapped,
            supply=Range.of(supply, 1) or Range(low=0, base=0, high=0),
            supply_method=method,
            market_per_business=Range.of(density),
            score=None if too_small else Range.of(score, 2),
            mapping_completeness=round(completeness, 2) if completeness is not None else None,
            too_small=too_small,
            thin_map=completeness is not None and completeness < model.COMPLETENESS_LOW,
        )
        data.method_version = model.METHOD_VERSION
        sources += ctx.sources
        fake = _as_result(data.market, method, completeness)
        checks += engine_checks(ctx, [fake], main, "tract")
        checks.append(
            Check(
                kind="scope",
                severity="info",
                message="The score compares this circle with its whole region. Visitors are not counted in "
                "catchments; use market_size on the municipality for that.",
            )
        )

    if walk_note:
        checks.append(walk_note)
    if radius_m < THIN_RADIUS_M:
        checks.append(
            Check(
                kind="thin_sample",
                severity="warning",
                message=f"Under {THIN_RADIUS_M} m the circle covers parts of a few tracts; residents assumed evenly "
                "spread within each tract make the figures rough.",
            )
        )
    if radius_m > WIDE_RADIUS_M:
        checks.append(
            Check(
                kind="scope",
                severity="info",
                message="A wide circle: compare with the municipality's own figures.",
            )
        )
    if data.unplaced_share > 0:
        checks.append(
            Check(
                kind="missing",
                severity="info" if data.unplaced_share < 0.05 else "warning",
                message=f"{fmt_pct(data.unplaced_share * 100)} of the tracts in the circle have no income-atlas "
                f"figures (INE redrew them since {geo.tracts_year}); their residents are not counted.",
            )
        )
    checks.append(
        Check(
            kind="estimate",
            severity="info",
            message=f"Residents and income are tract figures from the income atlas ({data.income_year}) weighted by "
            f"the share of each tract's area inside the circle (tract boundaries {geo.tracts_year}), assuming "
            "residents are spread evenly within each tract.",
        )
    )
    return Envelope(
        data=data,
        sources=dedupe_sources(sources),
        checks=checks,
        confidence=confidence_from(checks, "medium"),
    )


def _as_result(a: AreaOpportunity, method: str, completeness: float | None):
    """A minimal AreaResult for engine_checks."""
    from ..engine.inputs import AreaResult

    zero = Estimate.exact(0.0)
    return AreaResult(
        code=a.code, name=a.name, level=a.level, region="", population=a.population, households=None,
        monthly_income=None, spend_per_household=zero, income_ratio=Estimate.exact(1.0),
        family_share=Estimate.exact(1.0), market=None, mapped_places=a.mapped_places, supply=zero,
        supply_method=method, density=None, score=None, completeness=completeness, too_small=a.too_small,
    )  # fmt: skip


def render(env: Envelope[CatchmentData]) -> str:
    d = env.data
    lines = [
        (
            f"Catchment: {d.walk_minutes} minutes' walk from {d.found} ({d.municipality}; {d.area_method}, "
            f"{d.area_km2:.2f} km²); {d.tracts} census tracts"
            if d.walk_minutes
            else f"Catchment: {d.radius_m:,} m around {d.found} ({d.municipality}); {d.tracts} census tracts"
        ),
        f"Residents: {fmt_int(d.residents)}; households {fmt_int(d.households)} (average size {d.avg_household_size})",
        f"Net income per household: {fmt_eur(d.net_income_per_household)} ({d.income_year}); "
        f"aged 65+ {fmt_pct(d.pct_65_plus)}, under 18 {fmt_pct(d.pct_under_18)}",
        f"Mapped businesses inside: {fmt_int(d.businesses)}. Top: "
        + ", ".join(f"{c.label.lower()} {c.count}" for c in d.by_category[:6]),
    ]
    m = d.market
    if m:
        lines.append("")
        lines.append(f"Market ({m.name}, method {d.method_version}):")
        if m.market_eur:
            lines.append(
                f"- Yearly spending pool: {fmt_eur(m.market_eur.base)} ({fmt_eur(m.market_eur.low)} to {fmt_eur(m.market_eur.high)})"
            )
        lines.append(
            f"- Competing businesses: {m.supply.base:,.0f} ({m.mapped_places} mapped; {m.supply_method})"
        )
        if m.score:
            lines.append(
                f"- Opportunity score vs region: {m.score.base:.2f} ({m.score.low:.2f} to {m.score.high:.2f})"
            )
        elif m.too_small:
            lines.append("- Too few residents in the circle to score this category.")
    return "\n".join(lines)


async def catchment_tool(
    ine: IneData, store: osm.SnapshotStore, geo_store: GeometryStore, walk_store: WalkGraphs, *args, **kwargs
):
    env = await catchment(ine, store, geo_store, *args, walk_store=walk_store, **kwargs)
    return to_result(env, render(env))
