"""income: household income from INE's income atlas (ADRH), down to census tract."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..core.envelope import Check, Envelope, Source, confidence_from, dedupe_sources
from ..places.codes import PROVINCES
from ..places.resolver import Place
from ..sources.ine.adrh import UnitValues, by_unit
from ..sources.ine.datasets import ADRH_NAME, IneData, latest_year, source_for
from .common import fmt_eur, fmt_pct, pct_diff, resolve_place_arg, summary_stats, to_result

SubAreaLevel = Literal["none", "district", "tract"]
RankBy = Literal["net_income_per_person", "net_income_per_household", "median_income_per_consumption_unit"]
MAX_SUB_AREAS = 60

INCOME_KEYS = (
    "net_income_per_person",
    "net_income_per_household",
    "gross_income_per_person",
    "gross_income_per_household",
    "mean_income_per_consumption_unit",
    "median_income_per_consumption_unit",
)


class IncomeFigures(BaseModel):
    """Euros per year, in money of the data year (not inflation-adjusted)."""

    year: int | None = None
    net_income_per_person: float | None = None
    net_income_per_household: float | None = None
    gross_income_per_person: float | None = None
    gross_income_per_household: float | None = None
    mean_income_per_consumption_unit: float | None = None
    median_income_per_consumption_unit: float | None = None
    gini: float | None = Field(default=None, description="Gini index, 0-100.")
    p80_p20: float | None = Field(
        default=None, description="Income of the 80th percentile / 20th percentile."
    )
    pct_income_wages: float | None = None
    pct_income_pensions: float | None = None
    pct_income_unemployment_benefits: float | None = None
    pct_income_other_benefits: float | None = None
    pct_income_other: float | None = None
    suppressed: list[str] = Field(default_factory=list, description="Indicators INE withheld for this unit.")


class AreaIncome(BaseModel):
    code: str
    name: str
    level: str
    figures: IncomeFigures


class TrendPoint(BaseModel):
    year: int
    net_income_per_person: float | None
    net_income_per_household: float | None


class IncomeData(BaseModel):
    place: Place
    figures: IncomeFigures
    trend: list[TrendPoint] = Field(default_factory=list)
    growth_pct_per_person: float | None = Field(
        default=None, description="Change in net income per person over the trend."
    )
    benchmarks: list[AreaIncome] = Field(default_factory=list, description="Province, CCAA and Spain.")
    sub_area_level: SubAreaLevel = "none"
    sub_areas: list[AreaIncome] = Field(default_factory=list)
    sub_areas_total: int = 0
    sub_area_summary: dict[str, float] | None = Field(
        default=None, description="min / p25 / median / p75 / max of the ranking metric across all sub-areas."
    )


def figures_for(units: list[UnitValues], year: int | None = None) -> IncomeFigures:
    """Merge one unit's values from several ADRH tables (income, inequality, sources)."""
    years = [y for u in units for ys in u.values.values() for y in ys]
    if year is None:
        year = max(years, default=None)
    f = IncomeFigures(year=year)
    if year is None:
        return f
    for u in units:
        for key in list(u.values) + list(u.suppressed):
            if not hasattr(f, key):
                continue
            v = u.value(key, year)
            if v is not None:
                setattr(f, key, v)
            elif u.is_suppressed(key, year) and key not in f.suppressed:
                f.suppressed.append(key)
    return f


async def income(
    ine: IneData,
    place: str,
    sub_areas: SubAreaLevel = "none",
    rank_by: RankBy = "net_income_per_person",
    years: int = 5,
) -> Envelope[IncomeData]:
    p, checks = resolve_place_arg(place)
    years = max(1, min(years, 9))
    sources: list[Source] = []
    whole = sub_areas != "none" or p.level in ("district", "tract")

    tables = {}
    for kind in ("income", "inequality", "income_sources"):
        n = years if kind == "income" else 1
        if p.level in ("province", "ccaa"):
            res = await ine.adrh_benchmarks(kind, last_n=n)
        else:
            res = await ine.adrh(kind, p, last_n=1 if whole and kind != "income" else n, whole_province=whole)
        tables[kind] = by_unit(res.series)
        label = {
            "income": "renta media y mediana",
            "inequality": "Gini y P80/P20",
            "income_sources": "fuente de ingresos",
        }
        sources.append(source_for(res, f"{ADRH_NAME}: {label[kind]}", str(latest_year(res.series))))

    def unit(code: str) -> list[UnitValues]:
        return [t[code] for t in tables.values() if code in t]

    own = unit(p.code)
    if not own:
        raise_missing(p)
    figs = figures_for(own)
    inc = tables["income"].get(p.code)
    trend: list[TrendPoint] = []
    if inc:
        for y in sorted({y for ys in inc.values.values() for y in ys}):
            trend.append(
                TrendPoint(
                    year=y,
                    net_income_per_person=inc.value("net_income_per_person", y),
                    net_income_per_household=inc.value("net_income_per_household", y),
                )
            )
    data = IncomeData(place=p, figures=figs, trend=trend)
    pts = [t for t in trend if t.net_income_per_person is not None]
    if len(pts) >= 2:
        data.growth_pct_per_person = round(
            pct_diff(pts[-1].net_income_per_person or 0, pts[0].net_income_per_person or 1), 2
        )

    # Benchmarks: province, CCAA, Spain
    bench = await ine.adrh_benchmarks("income", last_n=1)
    bunits = by_unit(bench.series)
    single_province_ccaa = p.ccaa_code and len(provinces_in_ccaa(p.ccaa_code)) == 1
    for code in (p.province_code, None if single_province_ccaa else p.ccaa_code, "ES"):
        if code and code != p.code and code in bunits:
            u = bunits[code]
            data.benchmarks.append(
                AreaIncome(code=u.code, name=u.name, level=u.level, figures=figures_for([u]))
            )
    if data.benchmarks:
        sources.append(
            source_for(bench, f"{ADRH_NAME}: Spain, CCAA and provinces", str(latest_year(bench.series)))
        )

    # Sub-areas
    if sub_areas != "none":
        if p.level != "municipality":
            checks.append(
                Check(
                    kind="scope", severity="warning", message="Sub-areas are listed for municipalities only."
                )
            )
        else:
            codes = [c for c, u in tables["income"].items() if u.level == sub_areas and c.startswith(p.code)]
            rows = [
                AreaIncome(
                    code=c,
                    name=tables["income"][c].name,
                    level=sub_areas,
                    figures=figures_for(unit(c), figs.year),
                )
                for c in codes
            ]
            ranked = [r for r in rows if getattr(r.figures, rank_by) is not None]
            withheld = [r for r in rows if getattr(r.figures, rank_by) is None]
            ranked.sort(key=lambda r: -(getattr(r.figures, rank_by) or 0))
            data.sub_area_level = sub_areas
            data.sub_areas_total = len(rows)
            data.sub_area_summary = summary_stats(getattr(r.figures, rank_by) for r in ranked)
            if len(ranked) > MAX_SUB_AREAS:
                half = MAX_SUB_AREAS // 2
                data.sub_areas = ranked[:half] + ranked[-half:]
                checks.append(
                    Check(
                        kind="scope",
                        severity="info",
                        message=f"Showing the top and bottom {half} of {len(ranked)} {sub_areas}s by {rank_by}; "
                        "the summary covers all of them.",
                    )
                )
            else:
                data.sub_areas = ranked
            capped = top_coded([getattr(r.figures, rank_by) for r in ranked])
            if capped:
                value, n = capped
                checks.append(
                    Check(
                        kind="suppressed",
                        severity="warning",
                        message=f"{n} {sub_areas}s share the same top value ({fmt_eur(value)}) for {rank_by}: "
                        "INE caps the highest incomes for confidentiality, so their true values are higher "
                        "and they tie at the top of the ranking.",
                    )
                )
            if withheld:
                checks.append(
                    Check(
                        kind="suppressed",
                        severity="info" if len(withheld) <= 0.05 * len(rows) else "warning",
                        message=f"INE withheld {rank_by} for {len(withheld)} of {len(rows)} {sub_areas}s "
                        "(statistical secrecy for small units); they are left out of the ranking.",
                    )
                )
            if sub_areas == "tract":
                checks.append(
                    Check(
                        kind="scope",
                        severity="info",
                        message=f"Tracts follow INE's {figs.year} boundaries; some are redrawn every year.",
                    )
                )

    if figs.suppressed:
        checks.append(
            Check(
                kind="suppressed",
                severity="warning",
                message=f"INE withheld {', '.join(figs.suppressed)} for {p.name} (statistical secrecy).",
            )
        )
    if figs.year:
        checks.append(
            Check(
                kind="stale",
                severity="info",
                message=f"Income is for {figs.year}, the latest year INE has published (the atlas runs about "
                "two years behind). Euros of that year, not adjusted for inflation since.",
            )
        )
    return Envelope(
        data=data, sources=dedupe_sources(sources), checks=checks, confidence=confidence_from(checks)
    )


def top_coded(values: list[float | None], min_ties: int = 3) -> tuple[float, int] | None:
    """INE top-codes the highest incomes: many units share exactly the maximum value."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    top = max(vals)
    n = sum(1 for v in vals if v == top)
    return (top, n) if n >= min_ties else None


def provinces_in_ccaa(ccaa_code: str) -> list[str]:
    return [p for p, (_n, ccaa, _a) in PROVINCES.items() if f"CA{ccaa}" == ccaa_code]


def raise_missing(p: Place) -> None:
    from ..core.errors import DataError

    raise DataError(
        problem=f"No income data for {p.name} ({p.code}).",
        cause="The code is not in INE's income atlas for the latest year (tract codes change yearly).",
        fix="Use the municipality code, or list current tracts with sub_areas='tract'.",
    )


def render(env: Envelope[IncomeData]) -> str:
    d = env.data
    f = d.figures
    lines = [f"Household income: {d.place.name} ({d.place.level}, INE code {d.place.code}), {f.year}"]
    lines.append(
        f"Net income per person {fmt_eur(f.net_income_per_person)}, per household {fmt_eur(f.net_income_per_household)}; "
        f"gross per person {fmt_eur(f.gross_income_per_person)}, per household {fmt_eur(f.gross_income_per_household)}"
    )
    lines.append(
        f"Per consumption unit: mean {fmt_eur(f.mean_income_per_consumption_unit)}, "
        f"median {fmt_eur(f.median_income_per_consumption_unit)}"
    )
    if f.gini is not None:
        lines.append(f"Inequality: Gini {f.gini}, P80/P20 {f.p80_p20}")
    if f.pct_income_wages is not None:
        lines.append(
            f"Income sources: wages {fmt_pct(f.pct_income_wages)}, pensions {fmt_pct(f.pct_income_pensions)}, "
            f"unemployment {fmt_pct(f.pct_income_unemployment_benefits)}, other benefits "
            f"{fmt_pct(f.pct_income_other_benefits)}, other {fmt_pct(f.pct_income_other)}"
        )
    if d.trend and d.growth_pct_per_person is not None:
        lines.append(
            "Net income per person by year: "
            + ", ".join(f"{t.year} {fmt_eur(t.net_income_per_person)}" for t in d.trend)
            + f" ({d.growth_pct_per_person:+.1f}%)"
        )
    for b in d.benchmarks:
        bp = b.figures.net_income_per_person
        rel = (
            f"; {d.place.name} is {pct_diff(f.net_income_per_person, bp):+.1f}%"
            if bp and f.net_income_per_person
            else ""
        )
        lines.append(f"Benchmark {b.name} ({b.level}): net per person {fmt_eur(bp)}{rel}")
    if d.sub_areas:
        s = d.sub_area_summary or {}
        lines.append("")
        lines.append(
            f"{d.sub_area_level.capitalize()}s: {d.sub_areas_total} total. Net income per person across them: "
            f"min {fmt_eur(s.get('min'))}, median {fmt_eur(s.get('median'))}, max {fmt_eur(s.get('max'))}"
        )
        lines.append("code | name | net/person | net/household | median per cons. unit")
        for r in d.sub_areas:
            g = r.figures
            lines.append(
                f"{r.code} | {r.name} | {fmt_eur(g.net_income_per_person)} | {fmt_eur(g.net_income_per_household)} | "
                f"{fmt_eur(g.median_income_per_consumption_unit)}"
            )
    return "\n".join(lines)


async def income_tool(
    ine: IneData,
    place: str,
    sub_areas: SubAreaLevel = "none",
    rank_by: RankBy = "net_income_per_person",
    years: int = 5,
):
    env = await income(ine, place, sub_areas, rank_by, years)
    return to_result(env, render(env))
