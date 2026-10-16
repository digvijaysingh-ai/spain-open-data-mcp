"""market_size and compare_areas: the opportunity engine as tools.

Both evaluate areas with engine.inputs.MarketContext, so a number in a ranking is the same
number market_size gives for that area.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..categories import BY_KEY, MARKET, find_category
from ..core.envelope import Check, Confidence, Envelope, Source, confidence_from, dedupe_sources
from ..core.errors import DataError
from ..engine import model
from ..engine.estimate import Estimate
from ..engine.inputs import PARTIAL_INCOME_REGIONS, AreaResult, MarketContext, Overrides, region_of
from ..places.areas import SUB_UNIT_CODE_LENGTH, code_prefixes, population_of
from ..places.resolver import Place, municipalities_in, place_from_code
from ..sources.ine.adrh import UnitValues
from ..sources.ine.datasets import IneData, register_source
from ..sources.osm import snapshot as osm
from .businesses import osm_sources
from .common import fmt_eur, fmt_int, resolve_place_arg, to_result

CompareLevel = Literal["auto", "municipality", "district", "tract"]
MAX_ROWS = 40
SPEND_OVERRIDE_MAX_FACTOR = 5.0


class Range(BaseModel):
    low: float
    base: float
    high: float

    @classmethod
    def of(cls, e: Estimate | None, digits: int = 0) -> Range | None:
        return (
            None
            if e is None
            else cls(low=round(e.low, digits), base=round(e.base, digits), high=round(e.high, digits))
        )


class Assumption(BaseModel):
    name: str
    value: str
    source: str
    user_set: bool = False


class AreaOpportunity(BaseModel):
    code: str
    name: str
    level: str
    population: int | None
    households: Range | None
    market_eur: Range | None = Field(description="Yearly household spending pool for the category, euros.")
    mapped_places: int
    supply: Range = Field(description="Estimated competing businesses.")
    supply_method: str
    market_per_business: Range | None = Field(
        description="Euros per year per business (one pseudo-business added)."
    )
    score: Range | None = Field(
        description="Market per business relative to the region; 1.0 = same as the region. None if too small."
    )
    mapping_completeness: float | None = Field(
        description="Mapped pharmacies and bars per resident vs the region; under 0.5 the map looks thin."
    )
    too_small: bool
    thin_map: bool = Field(default=False, description="Mapping looks too thin to trust the supply estimate.")
    visitor_eur: Range | None = Field(
        default=None,
        description="Foreign visitors' share of market_eur (Spanish visitors raise only the high end).",
    )
    visitor_hotel_nights: dict[str, float] = Field(
        default_factory=dict, description="Hotel nights in the last 12 months by origin (domestic, foreign)."
    )
    visitor_method: str | None = None


class MarketData(BaseModel):
    method_version: str
    category: str
    label: str
    region: str
    area: AreaOpportunity
    spend_per_household: Range
    income_ratio: Range
    family_share: Range
    obtainable_eur: Range | None = Field(default=None, description="market_eur × capture_share, if given.")
    assumptions: list[Assumption]


class CompareData(BaseModel):
    method_version: str
    category: str
    label: str
    within: Place
    level: str
    ranked_by: str = "score.low"
    areas: list[AreaOpportunity]
    areas_total: int
    too_small: int


def resolve_market_category(text: str) -> str:
    cat = find_category(text)
    if cat is None or cat.key not in MARKET:
        raise DataError(
            problem=f"'{text}' has no market model.",
            cause="Market sizing covers categories whose household spending INE measures.",
            fix=f"Use one of: {', '.join(k for k in MARKET if not MARKET[k].visitor_only)}.",
        )
    return cat.key


def to_area(r: AreaResult) -> AreaOpportunity:
    return AreaOpportunity(
        code=r.code,
        name=r.name,
        level=r.level,
        population=r.population,
        households=Range.of(r.households),
        market_eur=Range.of(r.market),
        mapped_places=r.mapped_places,
        supply=Range.of(r.supply, 1) or Range(low=0, base=0, high=0),
        supply_method=r.supply_method,
        market_per_business=Range.of(r.density),
        score=Range.of(r.score, 2),
        mapping_completeness=round(r.completeness, 2) if r.completeness is not None else None,
        too_small=r.too_small,
        thin_map=r.completeness is not None and r.completeness < model.COMPLETENESS_LOW,
        visitor_eur=Range.of(r.visitor_foreign),
        visitor_hotel_nights={k: round(v) for k, v in r.visitor_nights.items()},
        visitor_method=r.visitor_method,
    )


def _population(p: Place, demo: dict[str, UnitValues] | None = None) -> int | None:
    if p.level in ("district", "tract"):
        u = (demo or {}).get(p.code)
        found = u.latest("population") if u else None
        return int(found[1]) if found else None
    return population_of(p.code)[0]


def engine_checks(
    ctx: MarketContext, results: list[AreaResult], region: str, level: str, ranking: bool = False
) -> list[Check]:
    """Checks every market answer carries."""
    checks = [
        Check(
            kind="estimate",
            severity="info",
            message=f"Method {model.METHOD_VERSION}: market = households × EPF spend per household × income-band "
            "ratio × category share of a shared pool; supply = DIRCE companies allocated by mapped places; score = "
            "market per business vs the region. Formulas: docs/designs/opportunity-engine.md.",
        ),
        Check(
            kind="estimate",
            severity="info",
            message="The income-band ratio comes from INE's total spend by household income band (no category "
            "breakdown), so it likely understates how fast spending on eating out or personal care rises with income.",
        ),
    ]
    if ctx.profile.visitor_food and level in ("municipality", "province", "ccaa"):
        estimated = [r.name for r in results if r.visitor_method == "province share by tourist-flat beds"]
        checks.append(
            Check(
                kind="estimate",
                severity="info",
                message="Includes foreign visitors' food and drink spend: hotel nights (INE, last 12 months) × their "
                "daily spend in the region (EGATUR) × the food and drink share × 80% eaten out (an assumption, "
                "range 60-100%). Spanish visitors raise only the high end, since residents' budgets already "
                "include some of it. Nights in tourist flats and second homes are not measured, so visitor "
                "demand is a lower bound."
                + (
                    f" Not an INE tourist point, so its hotel nights are the province's shared out by tourist-flat beds: "
                    f"{', '.join(estimated[:5])}."
                    if estimated
                    else ""
                ),
            )
        )
    elif ctx.profile.visitor_food:
        checks.append(
            Check(
                kind="scope",
                severity="warning",
                message="Visitor demand is computed for municipalities and up; this figure is residents' spending only.",
            )
        )
    elif ctx.profile.tourism:
        checks.append(
            Check(
                kind="scope",
                severity="warning",
                message="Residents' spending only: visitors are not counted for this category, which understates the "
                "market in tourist areas.",
            )
        )
    if ctx.profile.scope == "destination" and level in ("district", "tract"):
        checks.append(
            Check(
                kind="scope",
                severity="warning",
                message=f"{ctx.category.label} is a destination category: residents travel to spend on it, so a "
                f"{level} figure is what its residents spend, not what is spent there. Compare municipalities instead.",
            )
        )
    if any(r.supply_method == "mapped_only" for r in results):
        checks.append(
            Check(
                kind="estimate",
                severity="warning",
                message=f"INE's company directory has no yardstick for {ctx.category.label.lower()} (chains count once), "
                "so supply is the mapped count, which is below the true count. Scores are upper bounds.",
            )
        )
    unmapped = [r.name for r in results if r.supply_method == "dirce_population_floor"]
    if unmapped:
        checks.append(
            Check(
                kind="estimate",
                severity="warning",
                message=f"Few or no {ctx.category.label.lower()} mapped in {len(unmapped)} area(s) "
                f"({', '.join(unmapped[:5])}): their supply is set to half their population's share of the "
                "region's DIRCE companies, a floor rather than a count, which caps how high they can score.",
            )
        )
    thin = [r for r in results if r.completeness is not None and r.completeness < model.COMPLETENESS_LOW]
    if thin:
        names = ", ".join(r.name for r in thin[:5])
        checks.append(
            Check(
                kind="thin_sample",
                # One thin area in a ranking is set aside (ranked last); for a single area it sinks the answer.
                severity="warning" if ranking else "critical",
                message=f"The map looks thin in {len(thin)} area(s) ({names}): under half the region's mapped pharmacies "
                "and bars per resident. Their supply is likely understated and their score overstated"
                + ("; they are ranked after the others." if ranking else "."),
            )
        )
    if region in PARTIAL_INCOME_REGIONS:
        checks.append(
            Check(
                kind="scope",
                severity="warning",
                message=f"{PARTIAL_INCOME_REGIONS[region]} has its own tax system; INE's income atlas is partial there.",
            )
        )
    notes = sorted({n for r in results for n in r.notes})
    for n in notes[:4]:
        checks.append(Check(kind="estimate", severity="info", message=n[0].upper() + n[1:] + "."))
    return checks


async def market_size(
    ine: IneData,
    store: osm.SnapshotStore,
    place: str,
    category: str,
    spend_per_household: float | None = None,
    capture_share: float | None = None,
    competitors: float | None = None,
) -> Envelope[MarketData]:
    p, checks = resolve_place_arg(place)
    key = resolve_market_category(category)
    snap = await store.get()
    ctx = await MarketContext.create(ine, snap, key)
    region = region_of(p)
    default_spend, _ = ctx.epf.spend_per_household(ctx.profile.ecoicop, region)
    if (
        spend_per_household is not None
        and not 0 < spend_per_household <= default_spend.base * SPEND_OVERRIDE_MAX_FACTOR
    ):
        raise DataError(
            problem=f"spend_per_household {spend_per_household} is out of range.",
            cause=f"INE's figure is about {fmt_eur(default_spend.base)} per household per year.",
            fix=f"Pass a value above 0 and at most {fmt_eur(default_spend.base * SPEND_OVERRIDE_MAX_FACTOR)}.",
        )
    if capture_share is not None and not 0 < capture_share <= 1:
        raise DataError(
            problem=f"capture_share {capture_share} is out of range.",
            cause="It is the share of the market one business could win.",
            fix="Pass a fraction between 0 and 1, e.g. 0.05 for 5%.",
        )
    if competitors is not None and not 0 <= competitors <= 100_000:
        raise DataError(
            problem=f"competitors {competitors} is out of range.",
            cause="It replaces the estimated number of competing businesses.",
            fix="Pass a count between 0 and 100,000.",
        )
    demo = await ctx._units("demographics", p.code[:2]) if p.level in ("district", "tract") else None
    pop = _population(p, demo)
    ov = Overrides(spend_per_household, capture_share, competitors)
    r = await ctx.evaluate(p.code, p.name, p.level, region, pop, ov)
    checks += engine_checks(ctx, [r], region, p.level)
    if r.too_small:
        checks.append(
            Check(
                kind="thin_sample",
                severity="warning",
                message=f"Under {fmt_int(ctx.profile.min_population)} residents: too small to score "
                f"{ctx.category.label.lower()}; the market figure is shown, the score is not.",
            )
        )
    assumptions = [
        Assumption(
            name="Spend per household per year",
            value=fmt_eur(r.spend_per_household.base),
            source=f"INE EPF {ctx.epf.latest_year}, COICOP {', '.join(ctx.profile.ecoicop)}, {region}, 3-year smoothed"
            if spend_per_household is None
            else "user",
            user_set=spend_per_household is not None,
        ),
        Assumption(
            name="Income-band ratio",
            value=f"{r.income_ratio.base:.2f}",
            source=f"INE EPF spend by household income band, at {fmt_eur(r.monthly_income)} net per month (ADRH)",
        ),
        Assumption(
            name="Category share of the spending pool",
            value=f"{r.family_share.base:.0%}",
            source="national share of mapped places among categories sharing the same spending codes",
        ),
        Assumption(
            name="Competing businesses",
            value=f"{r.supply.base:,.0f}",
            source={
                "dirce_calibrated": "DIRCE companies in the region × the area's share of mapped places",
                "dirce_population_floor": "half of DIRCE companies in the region × the area's share of residents (floor)",
                "mapped_only": "mapped places (OpenStreetMap)",
                "user": "user",
            }[r.supply_method],
            user_set=competitors is not None,
        ),
    ]
    if r.visitor_foreign is not None:
        nights = r.visitor_nights
        assumptions.append(
            Assumption(
                name="Visitor hotel nights, last 12 months",
                value=f"{nights.get('foreign', 0):,.0f} foreign, {nights.get('domestic', 0):,.0f} Spanish",
                source=f"INE hotel occupancy survey ({r.visitor_method})",
            )
        )
        assumptions.append(
            Assumption(
                name="Share of visitors' food and drink spend eaten out",
                value="80% (60-100%)",
                source="assumption; EGATUR's food and drink item also covers groceries",
            )
        )
    obtainable = None
    if capture_share is not None and r.market is not None:
        obtainable = r.market * capture_share
        assumptions.append(
            Assumption(name="Capture share", value=f"{capture_share:.1%}", source="user", user_set=True)
        )
    if any(a.user_set for a in assumptions):
        checks.append(
            Check(
                kind="estimate",
                severity="warning",
                message="Includes user assumptions (marked in the table); the result is a scenario, not an INE figure.",
            )
        )
    sources: list[Source] = [*ctx.sources, *osm_sources(snap), register_source()]
    data = MarketData(
        method_version=model.METHOD_VERSION,
        category=key,
        label=ctx.category.label,
        region=region,
        area=to_area(r),
        spend_per_household=Range.of(r.spend_per_household) or Range(low=0, base=0, high=0),
        income_ratio=Range.of(r.income_ratio, 2) or Range(low=1, base=1, high=1),
        family_share=Range.of(r.family_share, 3) or Range(low=1, base=1, high=1),
        obtainable_eur=Range.of(obtainable),
        assumptions=assumptions,
    )
    floor: Confidence = "medium"
    return Envelope(
        data=data, sources=dedupe_sources(sources), checks=checks, confidence=confidence_from(checks, floor)
    )


async def compare_areas(
    ine: IneData,
    store: osm.SnapshotStore,
    within: str,
    category: str,
    level: CompareLevel = "auto",
    limit: int = 20,
) -> Envelope[CompareData]:
    p, checks = resolve_place_arg(within)
    key = resolve_market_category(category)
    snap = await store.get()
    ctx = await MarketContext.create(ine, snap, key)
    if level == "auto":
        level = "district" if p.level == "municipality" else "municipality"
    if p.level == "municipality" and level in ("district", "tract"):
        demo = await ctx._units("demographics", p.code[:2])
        length = SUB_UNIT_CODE_LENGTH[level]  # type: ignore[index]
        codes = sorted({c for c in demo if len(c) == length and c.startswith(p.code)})
        areas = [(c, demo[c].name, demo[c].latest("population")) for c in codes]
        units = [(c, n, int(pop[1]) if pop else None) for c, n, pop in areas]
    elif p.level in ("province", "ccaa") and level == "municipality":
        munis = [c for prefix in code_prefixes(p.code) for c in municipalities_in(prefix)]
        units = [(c, place_from_code(c).name, population_of(c)[0]) for c in munis]
    else:
        raise DataError(
            problem=f"Cannot compare {level}s within a {p.level}.",
            cause="Comparisons are districts or tracts within a municipality, or municipalities within a province or region.",
            fix="Pass a municipality with level 'district' or 'tract', or a province or region with level 'municipality'.",
        )
    results: list[AreaResult] = []
    too_small = 0
    for code, name, pop in units:
        if (pop or 0) < ctx.profile.min_population:
            too_small += 1
            continue
        region = place_from_code(code[:2]).ccaa_code or ""
        results.append(await ctx.evaluate(code, name, level, region, pop))
    scored = [r for r in results if r.score is not None]

    def thin(r: AreaResult) -> bool:
        return r.completeness is not None and r.completeness < model.COMPLETENESS_LOW

    scored.sort(key=lambda r: (thin(r), -(r.score.low if r.score else 0), -(r.score.base if r.score else 0)))
    region = region_of(p)
    checks += engine_checks(ctx, scored, region, level, ranking=True)
    checks.append(
        Check(
            kind="scope",
            severity="info",
            message="Ranked by the low end of each score, so an area ranks high only if even the cautious estimate "
            f"is good. {too_small} area(s) below {fmt_int(ctx.profile.min_population)} residents were not scored.",
        )
    )
    if len(scored) > limit:
        checks.append(
            Check(
                kind="scope",
                severity="info",
                message=f"Showing the top {limit} of {len(scored)} scored areas.",
            )
        )
    sources: list[Source] = [*ctx.sources, *osm_sources(snap), register_source()]
    data = CompareData(
        method_version=model.METHOD_VERSION,
        category=key,
        label=BY_KEY[key].label,
        within=p,
        level=level,
        areas=[to_area(r) for r in scored[: min(limit, MAX_ROWS)]],
        areas_total=len(results) + too_small,
        too_small=too_small,
    )
    return Envelope(
        data=data,
        sources=dedupe_sources(sources),
        checks=checks,
        confidence=confidence_from(checks, "medium"),
    )


def _r(e: Range | None, money: bool = False) -> str:
    if e is None:
        return "n/a"
    f = fmt_eur if money else (lambda v: f"{v:,.0f}")
    return f"{f(e.base)} ({f(e.low)} to {f(e.high)})"


def render_market(env: Envelope[MarketData]) -> str:
    d = env.data
    a = d.area
    lines = [
        f"Market for {d.label.lower()} in {a.name} ({a.level}, INE code {a.code}), method {d.method_version}"
    ]
    lines.append(f"Yearly spending pool: {_r(a.market_eur, money=True)}")
    if a.visitor_eur:
        lines.append(f"  of which foreign visitors: {_r(a.visitor_eur, money=True)}")
    if d.obtainable_eur:
        lines.append(f"Obtainable at the given capture share: {_r(d.obtainable_eur, money=True)}")
    lines.append(
        f"Households: {_r(a.households)}; spend per household {_r(d.spend_per_household, money=True)}"
    )
    lines.append(f"Competing businesses: {_r(a.supply)} ({a.mapped_places} mapped; method {a.supply_method})")
    lines.append(f"Market per business: {_r(a.market_per_business, money=True)}")
    if a.score:
        lines.append(
            f"Opportunity score vs {d.region}: {a.score.base:.2f} ({a.score.low:.2f} to {a.score.high:.2f}); "
            "1.0 = as much market per business as the region"
        )
    if a.mapping_completeness is not None:
        lines.append(f"Map completeness vs region: {a.mapping_completeness:.2f}")
    lines.append("")
    lines.append("Assumptions:")
    for s in d.assumptions:
        lines.append(f"- {s.name}: {s.value} ({'user assumption' if s.user_set else s.source})")
    return "\n".join(lines)


def render_compare(env: Envelope[CompareData]) -> str:
    d = env.data
    lines = [
        f"Opportunity for {d.label.lower()} by {d.level} in {d.within.name} ({d.within.level}, INE code {d.within.code}), "
        f"method {d.method_version}",
        f"{len(d.areas)} shown of {d.areas_total}; {d.too_small} too small to score. Ranked by the low end of the score.",
        "",
        "code | name | residents | market €/yr | businesses (mapped) | € per business | score (low-high)",
    ]
    for a in d.areas:
        mk = fmt_eur(a.market_eur.base) if a.market_eur else "n/a"
        pb = fmt_eur(a.market_per_business.base) if a.market_per_business else "n/a"
        sc = f"{a.score.base:.2f} ({a.score.low:.2f}-{a.score.high:.2f})" if a.score else "n/a"
        sc += " [thin map]" if a.thin_map else ""
        lines.append(
            f"{a.code} | {a.name} | {fmt_int(a.population)} | {mk} | {a.supply.base:,.0f} ({a.mapped_places}) | {pb} | {sc}"
        )
    return "\n".join(lines)


async def market_size_tool(ine: IneData, store: osm.SnapshotStore, *args, **kwargs):
    env = await market_size(ine, store, *args, **kwargs)
    return to_result(env, render_market(env))


async def compare_areas_tool(ine: IneData, store: osm.SnapshotStore, *args, **kwargs):
    env = await compare_areas(ine, store, *args, **kwargs)
    return to_result(env, render_compare(env))
