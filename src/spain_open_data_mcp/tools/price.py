"""price_band: a price range for a place, from local purchasing power and inflation.

Public data has no competitor prices, so this does not observe prices. It answers "if this
sells for P elsewhere (or did on a date), what range fits here today?":

    P_today  = P × CPI(category group, latest month) / CPI(category group, month of P)
    index    = median income per consumption unit here / the same in the reference place
    range    = between P_today (no local adjustment) and P_today × index (full adjustment);
               base = P_today × index^0.5 (half of the income gap passed through to price)

Always labelled as an estimate.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from ..categories import MARKET, find_category
from ..core.envelope import Check, Envelope, Source, confidence_from, dedupe_sources
from ..core.errors import DataError
from ..places.resolver import Place
from ..sources.ine.adrh import UnitValues, by_unit
from ..sources.ine.datasets import ADRH_NAME, IneData, source_for
from ..sources.ine.tempus import Series, TableResult
from .common import fmt_eur, fmt_pct, resolve_place_arg, to_result
from .market import Range

CPI_NAME = "Índice de Precios de Consumo (IPC), base 2025"
PASS_THROUGH = 0.5
CPI_GROUPS = {
    "01": "Alimentos y bebidas no alcohólicas",
    "03": "Vestido y calzado",
    "05": "Muebles y artículos del hogar",
    "06": "Sanidad",
    "09": "Actividades recreativas, deporte y cultura",
    "10": "Enseñanza",
    "11": "Restaurantes y servicios de alojamiento",
    "13": "Cuidado personal y bienes y servicios diversos",
}
_MONTH = re.compile(r"^(\d{4})-(\d{2})$")


class PriceBand(BaseModel):
    place: Place
    reference_place: Place
    reference_price: float
    price_month: str | None = Field(description="Month the reference price applied (YYYY-MM), if given.")
    cpi_group: str | None
    inflation_factor: float = Field(
        description="CPI change from price_month to the latest month (1.0 if none)."
    )
    cpi_latest_month: str | None
    reference_price_today: float
    purchasing_power_index: float = Field(description="Median income per consumption unit here / reference.")
    income_year: int | None
    band: Range
    local_annual_inflation_pct: float | None = Field(description="Province, general CPI, latest 12 months.")


def _cpi_index(series: list[Series], group: str) -> dict[tuple[int, int], float]:
    """(year, month) -> index for one ECOICOP group ('00' = general) from a CPI table."""
    out: dict[tuple[int, int], float] = {}
    for s in series:
        g = next((code for var, (code, _n) in s.dims.items() if var.startswith("Grupos ECOICOP")), None)
        kind = next((name for var, (_c, name) in s.dims.items() if var.startswith("Índices y Tasas")), None)
        if g == group and kind == "Índice":
            for p in s.points:
                if p.value is not None and p.period.startswith("M"):
                    out[(p.year, int(p.period[1:]))] = p.value
    return out


def _annual_change(series: list[Series]) -> float | None:
    for s in series:
        g = next((code for var, (code, _n) in s.dims.items() if var.startswith("Grupos ECOICOP")), None)
        kind = next((name for var, (_c, name) in s.dims.items() if var.startswith("Índices y Tasas")), None)
        if g == "00" and kind == "Variación anual":
            latest = s.latest()
            return latest.value if latest else None
    return None


async def _median_income(ine: IneData, p: Place) -> tuple[float, int, TableResult]:
    res = await ine.adrh("income", p)
    units: dict[str, UnitValues] = by_unit(res.series)
    u = units.get(p.code)
    found = u.latest("median_income_per_consumption_unit") if u else None
    if not found:
        raise DataError(
            problem=f"No median income for {p.name}.",
            cause="INE's income atlas withholds it here, or the place is not in the atlas.",
            fix="Use the municipality or province instead.",
        )
    year, value = found
    return value, year, res


async def price_band(
    ine: IneData,
    place: str,
    reference_price: float,
    category: str | None = None,
    price_month: str | None = None,
    reference_place: str = "ES",
) -> Envelope[PriceBand]:
    if not 0 < reference_price <= 1_000_000:
        raise DataError(
            problem=f"reference_price {reference_price} is out of range.",
            cause="It is the price of the product or service elsewhere, in euros.",
            fix="Pass a positive price, e.g. 39.9 for a monthly gym membership.",
        )
    p, checks = resolve_place_arg(place)
    sources: list[Source] = []
    if reference_place.upper() in ("ES", "SPAIN", "ESPAÑA", "ESPANA"):
        ref = Place(code="ES", level="ccaa", name="España")
    else:
        ref, more = resolve_place_arg(reference_place)
        checks += more

    group = None
    if category:
        cat = find_category(category)
        profile = MARKET.get(cat.key) if cat else None
        if profile is None:
            raise DataError(
                problem=f"No price group for '{category}'.",
                cause="Price uprating uses the CPI group of a market category.",
                fix=f"Use one of: {', '.join(MARKET)}, or leave category empty for the general CPI.",
            )
        group = profile.ecoicop[0][:2]

    factor, latest_month = 1.0, None
    if price_month:
        m = _MONTH.match(price_month)
        if not m:
            raise DataError(
                problem=f"'{price_month}' is not a month.",
                cause="Months are YYYY-MM.",
                fix="Pass e.g. '2024-03'.",
            )
        when = (int(m.group(1)), int(m.group(2)))
        cpi_res = await ine.cpi_groups_since_2002() if group else await ine.cpi_national()
        idx = _cpi_index(cpi_res.series, group or "00")
        if not idx:
            raise DataError(
                problem="No CPI series found.",
                cause="INE returned no index for this group.",
                fix="Try without category.",
            )
        latest = max(idx)
        if when not in idx:
            first = min(idx)
            raise DataError(
                problem=f"No CPI for {price_month}.",
                cause=f"The series runs from {first[0]}-{first[1]:02d} to {latest[0]}-{latest[1]:02d}"
                + ("" if group else "; the general index starts with the 2025 base"),
                fix="Pass a month in that range"
                + ("" if group else ", or a category to use its long series")
                + ".",
            )
        factor = idx[latest] / idx[when]
        latest_month = f"{latest[0]}-{latest[1]:02d}"
        sources.append(source_for(cpi_res, CPI_NAME, latest_month))

    here, here_year, here_res = await _median_income(ine, p)
    there, _y, there_res = await _median_income(ine, ref)
    sources += [
        source_for(here_res, f"{ADRH_NAME}: renta", str(here_year)),
        source_for(there_res, f"{ADRH_NAME}: renta", str(here_year)),
    ]
    index = here / there
    today = reference_price * factor
    adjusted, half = today * index, today * index**PASS_THROUGH
    low, high = sorted((today, adjusted))
    band = Range(low=round(low, 2), base=round(half, 2), high=round(high, 2))

    local_inflation = None
    if p.province_code:
        prov = await ine.cpi_province(p.province_code)
        local_inflation = _annual_change(prov.series)
        if local_inflation is not None:
            sources.append(source_for(prov, CPI_NAME + ": provincias", "latest"))

    checks.append(
        Check(
            kind="estimate",
            severity="info",
            message="A price range from purchasing power, not observed prices: public data has no competitor prices. "
            f"The base passes half of the income gap ({fmt_pct((index - 1) * 100)}) through to price.",
        )
    )
    if latest_month and here_year < int(latest_month[:4]) - 1:
        checks.append(
            Check(
                kind="stale",
                severity="info",
                message=f"Incomes are from {here_year} (latest income atlas); prices are uprated to {latest_month}.",
            )
        )
    data = PriceBand(
        place=p,
        reference_place=ref,
        reference_price=reference_price,
        price_month=price_month,
        cpi_group=f"{group} {CPI_GROUPS.get(group, '')}".strip()
        if group
        else ("00 Índice general" if price_month else None),
        inflation_factor=round(factor, 4),
        cpi_latest_month=latest_month,
        reference_price_today=round(today, 2),
        purchasing_power_index=round(index, 3),
        income_year=here_year,
        band=band,
        local_annual_inflation_pct=local_inflation,
    )
    return Envelope(
        data=data,
        sources=dedupe_sources(sources),
        checks=checks,
        confidence=confidence_from(checks, "medium"),
    )


def render(env: Envelope[PriceBand]) -> str:
    d = env.data
    lines = [
        f"Price band in {d.place.name} ({d.place.level}, INE code {d.place.code}) vs {d.reference_place.name}"
    ]
    if d.price_month:
        lines.append(
            f"Reference price {fmt_eur(d.reference_price)} in {d.price_month} is {d.reference_price_today:,.2f} € in "
            f"{d.cpi_latest_month} (CPI {d.cpi_group}: ×{d.inflation_factor:.3f})"
        )
    lines.append(
        f"Purchasing power here vs {d.reference_place.name}: {d.purchasing_power_index:.2f} "
        f"(median income per consumption unit, {d.income_year})"
    )
    lines.append(f"Suggested range: {d.band.low:,.2f} € to {d.band.high:,.2f} €; base {d.band.base:,.2f} €")
    if d.local_annual_inflation_pct is not None:
        lines.append(
            f"Local inflation (province, general CPI, 12 months): {d.local_annual_inflation_pct:.1f}%"
        )
    return "\n".join(lines)


async def price_band_tool(ine: IneData, *args, **kwargs):
    env = await price_band(ine, *args, **kwargs)
    return to_result(env, render(env))
