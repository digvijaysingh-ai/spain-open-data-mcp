"""crime: recorded offences per 1,000 residents for a place, from the Interior ministry.

The ministry publishes every municipality over 20,000 residents, every province and region,
and Spain (data/crime.json, built by scripts/build_crime.py). Smaller places, districts and
tracts are answered with the smallest published area that contains them, and the result says
so. Rates use the official population (Padrón).
"""

from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from importlib import resources
from typing import Any

from pydantic import BaseModel, Field

from ..core.envelope import Check, Envelope, Source, confidence_from
from ..core.errors import DataError
from ..places.areas import SPAIN, benchmark_ladder, population_of
from ..places.codes import CCAA, PROVINCES
from ..places.resolver import Place, natural_name
from .common import fmt_int, fmt_pct, resolve_place_arg, to_result

LABELS = {
    "total": "All recorded offences",
    "conventional": "Conventional crime (all except cybercrime)",
    "theft": "Theft without force (pickpocketing, shoplifting)",
    "burglary": "Burglary (homes, businesses and other premises)",
    "home_burglary": "Home burglary",
    "robbery_violence": "Robbery with violence or intimidation",
    "vehicle_theft": "Vehicle theft",
    "assault": "Serious assault and brawls",
    "sexual": "Sexual offences",
    "homicide": "Homicide and murder (completed)",
    "attempted_homicide": "Attempted homicide and murder",
    "drug_trafficking": "Drug trafficking",
    "cybercrime": "Cybercrime",
    "online_fraud": "Online fraud",
}
THIN = 20  # counts below this swing a lot from year to year
MIN_MUNICIPALITY = 20_000


class Rate(BaseModel):
    type: str
    label: str
    count: float | None
    previous_count: float | None
    change_pct: float | None = Field(description="Change against the same period a year earlier.")
    per_1000_residents: float | None


class CrimeArea(BaseModel):
    code: str
    name: str
    level: str
    population: int
    population_date: str
    rates: list[Rate]


class LatestPeriod(BaseModel):
    period: str
    previous_period: str
    rates: list[Rate] = Field(description="Year to date; per-resident rates are not annualised.")


class CrimeData(BaseModel):
    place: Place
    area: CrimeArea = Field(
        description="The area the figures describe: the place, or the smallest published area containing it."
    )
    period: str
    previous_period: str
    benchmarks: list[CrimeArea]
    latest: LatestPeriod | None


@lru_cache(maxsize=1)
def crime_data() -> dict[str, Any]:
    path = resources.files("spain_open_data_mcp.data").joinpath("crime.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _level(code: str) -> str:
    if code == SPAIN:
        return "country"
    return "ccaa" if code.startswith("CA") else {5: "municipality", 2: "province"}[len(code)]


def _name(code: str, areas: dict[str, Any]) -> str:
    if code == SPAIN:
        return "España"
    if code.startswith("CA"):
        return natural_name(CCAA[code[2:]][0])
    if len(code) == 2:
        return natural_name(PROVINCES[code][0])
    return areas[code]["name"]


def published_code(code: str, areas: dict[str, Any]) -> str | None:
    """The code itself if published; a single-province region's province is published as the region."""
    if code in areas:
        return code
    if len(code) == 2 and code in PROVINCES and f"CA{PROVINCES[code][1]}" in areas:
        return f"CA{PROVINCES[code][1]}"
    return None


def _rates(values: dict[str, list[float | None]], population: int) -> list[Rate]:
    out = []
    for slug, label in LABELS.items():
        prev, cur = values.get(slug, [None, None])
        change = (cur - prev) / prev * 100 if cur is not None and prev else None
        per = cur / population * 1000 if cur is not None and population else None
        out.append(
            Rate(
                type=slug,
                label=label,
                count=cur,
                previous_count=prev,
                change_pct=change,
                per_1000_residents=per,
            )
        )
    return out


def _area(code: str, period: dict[str, Any]) -> CrimeArea:
    areas = period["areas"]
    pop, pop_date = population_of(code)
    return CrimeArea(
        code=code, name=_name(code, areas), level=_level(code), population=pop, population_date=pop_date,
        rates=_rates(areas[code]["values"], pop),
    )  # fmt: skip


def crime(place: str) -> Envelope[CrimeData]:
    p, checks = resolve_place_arg(place)
    data = crime_data()
    year, latest = data["periods"]["year"], data["periods"]["latest"]
    areas = year["areas"]
    own = p.municipality_code if p.level in ("district", "tract") else p.code
    candidates = [own, *benchmark_ladder(p)] if own else benchmark_ladder(p)
    chosen = next((c for c in (published_code(c, areas) for c in candidates) if c), None)
    if chosen is None:
        raise DataError(
            problem=f"No crime figures for {p.name}.",
            cause="The Interior ministry's balance has no row for it or any area containing it.",
            fix="Try the province or region.",
        )
    if p.level in ("district", "tract"):
        checks.append(
            Check(
                kind="scope",
                severity="warning",
                message=f"Crime is published by municipality, not by {p.level}; figures are for "
                f"{_name(chosen, areas)} as a whole.",
            )
        )
    elif published_code(p.code, areas) is None:
        checks.append(
            Check(
                kind="scope",
                severity="warning",
                message=f"The ministry publishes municipalities over {MIN_MUNICIPALITY:,} residents only; "
                f"{p.name} is not among them, so figures are for {_name(chosen, areas)}.",
            )
        )
    elif chosen != p.code:
        checks.append(
            Check(
                kind="scope",
                severity="info",
                message=f"{p.name} is published as part of {_name(chosen, areas)}.",
            )
        )
    area = _area(chosen, year)
    bench_codes = [c for c in (published_code(c, areas) for c in benchmark_ladder(p)) if c and c != chosen]
    benchmarks = [_area(c, year) for c in dict.fromkeys(bench_codes)]
    latest_block = None
    if chosen in latest["areas"] and latest["label"] != year["label"]:
        latest_block = LatestPeriod(
            period=latest["label"], previous_period=latest["previous_label"],
            rates=_rates(latest["areas"][chosen]["values"], area.population),
        )  # fmt: skip
    thin = [r.label for r in area.rates if r.count is not None and r.count < THIN and r.type != "homicide"]
    if area.level == "municipality" and thin:
        checks.append(
            Check(
                kind="thin_sample",
                severity="info",
                message=f"Fewer than {THIN} offences a year for: {', '.join(thin)}. Such counts swing a lot "
                "from year to year; compare rates over several years before drawing conclusions.",
            )
        )
    checks.append(
        Check(
            kind="scope",
            severity="info",
            message="These are offences known to the police (recorded crime), not all crime: reporting "
            "rates differ by type of offence. Rates per 1,000 residents overstate the risk for residents "
            "where many visitors or commuters come in (city centres, tourist towns).",
        )
    )
    retrieved = datetime.fromisoformat(data["built_at"])
    sources = [
        Source(
            publisher=data["publisher"],
            dataset=f"{data['dataset']}, {period['label']}",
            dataset_id=f"quarter {period['quarter']}",
            url=period["url"],
            licence=data["licence"],
            attribution="Fuente: Ministerio del Interior, Balance de Criminalidad.",
            data_period=period["label"],
            retrieved_at=retrieved,
        )
        for period in ([year, latest] if latest_block else [year])
    ]
    pop_check = Check(
        kind="scope",
        severity="info",
        message=f"Rates use the official population (Padrón) of {area.population_date}.",
    )
    checks.append(pop_check)
    env_data = CrimeData(
        place=p, area=area, period=year["label"], previous_period=year["previous_label"],
        benchmarks=benchmarks, latest=latest_block,
    )  # fmt: skip
    return Envelope(data=env_data, sources=sources, checks=checks, confidence=confidence_from(checks))


KEY_TYPES = ("total", "conventional", "theft", "burglary", "home_burglary", "robbery_violence", "vehicle_theft", "assault", "sexual", "drug_trafficking", "cybercrime")  # fmt: skip


def render(env: Envelope[CrimeData]) -> str:
    d = env.data
    a = d.area
    lines = [f"Recorded crime: {a.name} ({a.level}, population {fmt_int(a.population)}), {d.period}"]
    rates = {r.type: r for r in a.rates}
    for t in KEY_TYPES:
        r = rates[t]
        if r.count is None:
            continue
        per = f"{r.per_1000_residents:.1f} per 1,000" if r.per_1000_residents is not None else "n/a"
        change = f", {r.change_pct:+.1f}% vs {d.previous_period}" if r.change_pct is not None else ""
        lines.append(f"- {r.label}: {fmt_int(r.count)} ({per}{change})")
    homicide = rates["homicide"].count
    if homicide is not None:
        lines.append(f"- {LABELS['homicide']}: {fmt_int(homicide)}")
    for b in d.benchmarks:
        total = next(r for r in b.rates if r.type == "total")
        conv = next(r for r in b.rates if r.type == "conventional")
        lines.append(
            f"Benchmark {b.name} ({b.level}): all offences {total.per_1000_residents or 0:.1f} per 1,000, "
            f"conventional {conv.per_1000_residents or 0:.1f} per 1,000"
        )
    if d.latest:
        total = next(r for r in d.latest.rates if r.type == "total")
        lines.append(
            f"Latest: {d.latest.period}, all offences {fmt_int(total.count)} "
            f"({fmt_pct(total.change_pct) if total.change_pct is not None else 'n/a'} vs {d.latest.previous_period})"
        )
    return "\n".join(lines)


async def crime_tool(place: str):
    env = crime(place)
    return to_result(env, render(env))
