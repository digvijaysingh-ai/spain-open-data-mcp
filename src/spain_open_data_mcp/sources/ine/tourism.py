"""Visitor demand from INE tourism statistics.

- Hotel overnight stays (EOH), last 12 months, split into Spanish residents and foreign
  visitors: by "tourist point" (about 130 municipalities) and by province.
- Tourist flat beds by municipality (experimental statistic, VTE), used to share out a
  province's nights to municipalities that are not tourist points.
- Daily spend per visitor: foreign visitors from EGATUR by destination region, Spanish
  residents from the residents' travel survey (ETR) by destination region.
- Food and drink's share of foreign visitors' spend (EGATUR "manutención" / total).

Only hotel nights are measured; nights in tourist flats, campsites and second homes are
not, so visitor demand is a lower bound outside hotel-heavy towns.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .tempus import Series

DOMESTIC, FOREIGN = "domestic", "foreign"


def _dim(s: Series, *prefixes: str) -> tuple[str, str] | None:
    for var, (code, name) in s.dims.items():
        if any(var.lower().startswith(p.lower()) for p in prefixes):
            return code, name
    return None


def _origin(s: Series) -> str | None:
    for _code, name in s.dims.values():
        if "xtranjero" in name:
            return FOREIGN
        if "España" in name and name.startswith("Residentes"):
            return DOMESTIC
    return None


def _is(s: Series, word: str) -> bool:
    return any(name.lower().startswith(word) for _c, name in s.dims.values())


def _region(s: Series) -> str | None:
    for var, (code, name) in s.dims.items():
        if var.startswith("Total Nacional") or name in ("Total Nacional", "Total"):
            return "ES"
        if var.startswith("Comunidades") and code:
            return f"CA{code}"
    return None


def _sum_last_year(s: Series) -> float:
    return sum(p.value for p in s.points if p.value is not None)


@dataclass
class Tourism:
    point_nights: dict[str, dict[str, float]] = field(
        default_factory=dict
    )  # municipality -> origin -> nights
    province_nights: dict[str, dict[str, float]] = field(default_factory=dict)
    flat_beds: dict[str, float] = field(default_factory=dict)  # municipality -> tourist flat beds
    foreign_daily: dict[str, float] = field(default_factory=dict)  # region -> € per person per day
    domestic_daily: dict[str, float] = field(default_factory=dict)
    foreign_food_share: float | None = None

    def nights(self, municipality: str) -> tuple[dict[str, float], str]:
        """Hotel nights in the last 12 months by origin, and how they were obtained."""
        if municipality in self.point_nights:
            return self.point_nights[municipality], "tourist point"
        prov = municipality[:2]
        total = self.province_nights.get(prov)
        if not total:
            return {}, "no data"
        points = [m for m in self.point_nights if m[:2] == prov]
        rest = {
            o: max(total.get(o, 0.0) - sum(self.point_nights[m].get(o, 0.0) for m in points), 0.0)
            for o in total
        }
        beds_elsewhere = sum(b for m, b in self.flat_beds.items() if m[:2] == prov and m not in points)
        share = self.flat_beds.get(municipality, 0.0) / beds_elsewhere if beds_elsewhere else 0.0
        return {o: v * share for o, v in rest.items()}, "province share by tourist-flat beds"


def parse_points(series: list[Series], municipalities: dict[str, str]) -> dict[str, dict[str, float]]:
    """Municipality -> origin -> hotel nights. EOH codes most points internally ('H1' is
    Málaga); `municipalities` maps them to INE codes (built by scripts/build_reference.py)."""
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for s in series:
        point, origin = _dim(s, "PUNTOS TUR"), _origin(s)
        muni = municipalities.get(point[0]) if point and point[0] else None
        if muni and origin and _is(s, "pernoct"):
            out[muni][origin] = _sum_last_year(s)  # one series per point and origin
    return dict(out)


def parse_province_nights(series: list[Series], _province: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for s in series:
        origin = _origin(s)
        if origin and _is(s, "pernoct"):
            out[origin] = _sum_last_year(s)
    return out


def parse_flat_beds(series: list[Series]) -> dict[str, float]:
    out: dict[str, float] = {}
    for s in series:
        muni = _dim(s, "Municipios")
        latest = s.latest()
        if muni and muni[0] and _is(s, "plazas") and not _is(s, "plazas por") and latest and latest.value:
            out[muni[0]] = latest.value
    return out


def parse_daily_spend(series: list[Series]) -> dict[str, float]:
    """Region -> average of the latest values of 'gasto medio diario por persona'."""
    out: dict[str, float] = {}
    for s in series:
        region = _region(s)
        if not region or not _is(s, "gasto medio diario"):
            continue
        if any(
            name.startswith(("Tasa", "Variación", "Distribución", "Acumulado"))
            for _c, name in s.dims.values()
        ):
            continue
        vals = [p.value for p in s.points if p.value]
        if vals:
            out[region] = sum(vals) / len(vals)
    return out


def parse_food_share(series: list[Series]) -> float | None:
    totals: dict[str, float] = {}
    for s in series:
        if any(name.startswith(("Tasa", "Acumulado")) for _c, name in s.dims.values()):
            continue
        for _code, name in s.dims.values():
            if name in ("Gasto total", "Gasto en manutención"):
                totals[name] = _sum_last_year(s)
    if totals.get("Gasto total"):
        return totals.get("Gasto en manutención", 0.0) / totals["Gasto total"]
    return None
