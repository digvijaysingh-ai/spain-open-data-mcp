"""rents: how housing rents have changed in a place, from INE's rent price index (IPVA).

IPVA is an experimental INE statistic built from landlords' tax returns (AEAT) and the
cadastre: an index of rents on existing and new contracts, 2015 = 100, yearly. It is published
for Spain, regions, provinces, municipalities over 10,000 residents and districts of provincial
capitals. It does not cover the Basque Country or Navarra (their own tax agencies), and it
gives changes, not rents in euros: the state rent reference system (SERPAVI) that has euro
levels is not available for automated download.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..core.envelope import Check, Envelope, confidence_from, dedupe_sources
from ..core.errors import DataError
from ..places.area_names import with_names
from ..places.areas import SPAIN, benchmark_ladder
from ..places.resolver import Place, natural_name
from ..sources.ine.datasets import IPVA_NAME, IneData, source_for
from ..sources.ine.tempus import Series, TableResult
from .common import fmt_pct, resolve_place_arg, to_result

NO_IPVA_CCAA = {"CA15", "CA16"}  # Navarra, País Vasco: foral tax systems


class RentArea(BaseModel):
    code: str
    name: str
    level: str
    latest_year: int
    index: float = Field(description="Rent index, 2015 = 100.")
    annual_change_pct: float | None
    change_5y_pct: float | None = Field(description="Change over the last five years.")
    change_since_2015_pct: float
    by_year: dict[int, float] = Field(description="Index by year.")


class RentsData(BaseModel):
    place: Place
    area: RentArea = Field(description="The place, or the smallest area containing it that INE publishes.")
    benchmarks: list[RentArea]


def _code(s: Series) -> tuple[str, str] | None:
    """(our code, name) for a series' area: '00' is Spain, regions get the 'CA' prefix."""
    for var, (code, name) in s.dims.items():
        if var == "Total Nacional":
            return SPAIN, "España"
        if var.startswith("Comunidades"):
            return f"CA{code}", natural_name(name)
        if var in ("Provincias", "Municipios", "Distritos"):
            return code, with_names(code, natural_name(name))
    return None


def _is_total(s: Series) -> bool:
    return all(
        name == "Total"
        for var, (_c, name) in s.dims.items()
        if var.startswith(("Tamaño", "Tipo de edif", "Antigüedad"))
    )


def parse_ipva(result: TableResult, level: str) -> dict[str, RentArea]:
    index: dict[str, dict[int, float]] = {}
    change: dict[str, dict[int, float]] = {}
    names: dict[str, str] = {}
    for s in result.series:
        key = _code(s)
        if key is None or not _is_total(s):
            continue
        code, name = key
        kind = next((n for var, (_c, n) in s.dims.items() if var == "Tipo de dato"), "")
        target = index if kind == "Índice" else change if kind.startswith("Variación anual") else None
        if target is None:
            continue
        names[code] = name
        target.setdefault(code, {}).update({p.year: p.value for p in s.points if p.value is not None})
    out = {}
    for code, years in index.items():
        if not years:
            continue
        last = max(years)
        five = years.get(last - 5)
        out[code] = RentArea(
            code=code, name=names[code], level="country" if code == SPAIN else level, latest_year=last,
            index=years[last], annual_change_pct=change.get(code, {}).get(last),
            change_5y_pct=(years[last] / five - 1) * 100 if five else None,
            change_since_2015_pct=years[last] - 100, by_year=dict(sorted(years.items())),
        )  # fmt: skip
    return out


def _level_of(code: str) -> str:
    if code == SPAIN or code.startswith("CA"):
        return "ccaa"
    return {2: "province", 5: "municipality", 7: "district"}[len(code)]


async def rents(ine: IneData, place: str) -> Envelope[RentsData]:
    p, checks = resolve_place_arg(place)
    if p.ccaa_code in NO_IPVA_CCAA or p.code in NO_IPVA_CCAA:
        raise DataError(
            problem=f"INE's rent index does not cover {p.name}.",
            cause="The Basque Country and Navarra collect their own taxes, and the index is built from "
            "the state tax agency's records.",
            fix="Use the Basque (Eustat) or Navarra (Nastat) housing statistics; find_datasets('alquiler') can locate them.",
        )
    own = p.code[:7] if p.level == "tract" else p.code
    candidates = [own, *benchmark_ladder(p)]
    tables: dict[str, TableResult] = {}
    parsed: dict[str, dict[str, RentArea]] = {}

    async def get(level: str) -> dict[str, RentArea]:
        if level not in parsed:
            tables[level] = await ine.rent_index(level)
            parsed[level] = parse_ipva(tables[level], level)
        return parsed[level]

    found: list[RentArea] = []
    for code in candidates:
        area = (await get(_level_of(code))).get(code)
        if area:
            found.append(area)
    if not found:
        raise DataError(
            problem=f"No rent index for {p.name}.",
            cause="INE publishes it for municipalities over 10,000 residents, districts of provincial "
            "capitals, provinces and regions.",
            fix="Try the province.",
        )
    area, benchmarks = found[0], found[1:]
    if area.code != own:
        why = {
            "tract": "INE does not publish rents by census tract",
            "district": "INE publishes a rent index only for districts of provincial capitals with enough lets",
            "municipality": "INE publishes municipalities over 10,000 residents only",
        }.get(p.level, "INE does not publish this area")
        checks.append(
            Check(
                kind="scope",
                severity="warning",
                message=f"{why}; figures are for {area.name} ({area.level}).",
            )
        )
    checks += [
        Check(
            kind="estimate",
            severity="info",
            message="IPVA is an experimental INE statistic from landlords' tax returns: it measures how rents "
            "on declared contracts changed (2015 = 100), not rents in euros, and misses undeclared lets.",
        ),
        Check(
            kind="stale",
            severity="info",
            message=f"Latest year {area.latest_year}; INE publishes each year's index about ten months later.",
        ),
    ]
    sources = dedupe_sources([source_for(t, IPVA_NAME, str(area.latest_year)) for t in tables.values()])
    data = RentsData(place=p, area=area, benchmarks=benchmarks)
    return Envelope(data=data, sources=sources, checks=checks, confidence=confidence_from(checks, "medium"))


def render(env: Envelope[RentsData]) -> str:
    d = env.data
    a = d.area
    lines = [
        f"Rents: {a.name} ({a.level}), INE rent index {a.latest_year}: {a.index:.1f} (2015 = 100)",
        f"Change: last year {fmt_pct(a.annual_change_pct)}, last five years {fmt_pct(a.change_5y_pct)}, "
        f"since 2015 {fmt_pct(a.change_since_2015_pct)}",
        "By year: " + ", ".join(f"{y} {v:.1f}" for y, v in a.by_year.items()),
    ]
    for b in d.benchmarks:
        lines.append(
            f"Benchmark {b.name} ({b.level}): last year {fmt_pct(b.annual_change_pct)}, "
            f"five years {fmt_pct(b.change_5y_pct)}, since 2015 {fmt_pct(b.change_since_2015_pct)}"
        )
    return "\n".join(lines)


async def rents_tool(ine: IneData, place: str):
    env = await rents(ine, place)
    return to_result(env, render(env))
