"""INE household budget survey (Encuesta de Presupuestos Familiares, EPF).

Three tables, all by autonomous community (region) and for Spain:
- spend_by_code: euros per household per year for each 4-digit COICOP 2018 code.
- spend_by_income: total spend per household by net monthly household income band. It has
  no category breakdown, so the same income ratio applies to every category.
- cv_by_subgroup: coefficient of variation (%) of spend per 3-digit subgroup.

Regional 4-digit cells are noisy, and INE publishes 0 where a sample is too small, so:
- a 0 is treated as missing, never as no spending;
- a region's spend for a category is the national 4-digit amount, averaged over the last
  three survey years, times the region's ratio to Spain at the 3-digit subgroup level, also
  averaged over three years.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field

from ...engine.estimate import Estimate
from .tempus import Series

SPAIN = "ES"
YEARS_AVERAGED = 3
DEFAULT_CV_PCT = 10.0  # when INE publishes no CV for a region and subgroup
# Net monthly household income bands in table 74002, with the midpoints used to interpolate.
BANDS: tuple[tuple[str, float], ...] = (
    ("Hasta 999 euros", 500.0),
    ("De 1.000 a 1.999 euros", 1_500.0),
    ("De 2.000 a 2.999 euros", 2_500.0),
    ("3.000 euros o más", 4_000.0),
)
_CODE = re.compile(r"^(\d\d\.\d\.\d)")
_SUBGROUP = re.compile(r"^(\d\d\.\d)")


def _region(s: Series) -> str | None:
    for var, (code, _name) in s.dims.items():
        if var == "Total Nacional":
            return SPAIN
        if var.startswith("Comunidades") and code:
            return f"CA{code}"
    return None


def _measure(s: Series) -> str:
    for var, (_code, name) in s.dims.items():
        if var.startswith("Gastos totales") or var.startswith("Gastos medios"):
            return name
    return ""


def _code(s: Series, pattern: re.Pattern[str], var_prefix: str) -> str | None:
    for var, (code, _name) in s.dims.items():
        if var.startswith(var_prefix) and code and (m := pattern.match(code)):
            return m.group(1)
    return None


def _matches(code: str, wanted: tuple[str, ...]) -> bool:
    """'01.1.1' matches '01.1.1' and the prefix '01.'."""
    return any(code.startswith(w) for w in wanted)


@dataclass
class Epf:
    # (region, 4-digit code) -> {year: euros per household}; zeros are dropped
    spend: dict[tuple[str, str], dict[int, float]] = field(default_factory=dict)
    # (region, band label) -> {year: euros per household}
    by_income: dict[tuple[str, str], dict[int, float]] = field(default_factory=dict)
    # (region, 3-digit subgroup) -> latest CV in %
    cv: dict[tuple[str, str], float] = field(default_factory=dict)

    @property
    def latest_year(self) -> int | None:
        years = [y for v in self.spend.values() for y in v]
        return max(years) if years else None

    def _total(self, region: str, codes: list[str], years: list[int]) -> list[float]:
        """Sum over codes, per year, keeping only years where every code has a value."""
        out = []
        for y in years:
            vals = [self.spend.get((region, c), {}).get(y) for c in codes]
            if all(v is not None for v in vals):
                out.append(sum(v for v in vals if v is not None))
        return out

    def _ratio(self, region: str, codes: list[str], year: int) -> float | None:
        """Region / Spain for one year, over the codes both publish that year."""
        pairs = [
            (r, n)
            for c in codes
            if (r := self.spend.get((region, c), {}).get(year))
            and (n := self.spend.get((SPAIN, c), {}).get(year))
        ]
        if not pairs:
            return None
        return sum(r for r, _ in pairs) / sum(n for _, n in pairs)

    def codes_for(self, wanted: tuple[str, ...]) -> list[str]:
        return sorted({c for (r, c) in self.spend if r == SPAIN and _matches(c, wanted)})

    def spend_per_household(self, wanted: tuple[str, ...], region: str) -> tuple[Estimate, list[str]]:
        """Smoothed euros per household per year for the codes in `wanted`, in `region`.

        Returns the estimate and notes on what was approximated.
        """
        notes: list[str] = []
        codes = self.codes_for(wanted)
        if not codes:
            raise KeyError(f"no EPF spending codes match {wanted}")
        latest = self.latest_year or 0
        years = list(range(latest - YEARS_AVERAGED + 1, latest + 1))
        national = self._total(SPAIN, codes, years)
        if not national:
            raise KeyError(f"no national EPF spend for {codes}")
        base = sum(national) / len(national)
        ratio = 1.0
        if region != SPAIN:
            subgroups = sorted({c[:4] for c in codes})
            sub_codes = sorted({c for (r, c) in self.spend if r == SPAIN and c[:4] in subgroups})
            ratios = [r for y in years if (r := self._ratio(region, sub_codes, y)) is not None]
            if ratios:
                ratio = sum(ratios) / len(ratios)
            else:
                notes.append(
                    f"no regional EPF figures for subgroup(s) {', '.join(subgroups)}; national value used"
                )
        cvs = [self.cv.get((region, c[:4])) or self.cv.get((SPAIN, c[:4])) for c in codes]
        cv = max((c for c in cvs if c), default=DEFAULT_CV_PCT)
        return Estimate.around(base * ratio, 1.96 * cv / 100), notes

    def income_ratio(self, region: str, monthly_income: float) -> tuple[Estimate, list[str]]:
        """Spend of households at `monthly_income` relative to the regional average, interpolated
        between the midpoints of INE's four income bands. Applies to total spend, not per category."""
        notes: list[str] = []
        points = self._band_points(region)
        if len(points) < len(BANDS):
            points = self._band_points(SPAIN)
            notes.append("regional income-band spend incomplete; Spain's bands used")
        if not points:
            return Estimate.around(1.0, 0.1), ["no income-band spend; ratio 1 used"]
        ratio = points[0][1]
        if monthly_income <= points[0][0]:
            pass
        elif monthly_income >= points[-1][0]:
            ratio = points[-1][1]
        else:
            for (x0, y0), (x1, y1) in itertools.pairwise(points):
                if x0 <= monthly_income <= x1:
                    ratio = y0 + (y1 - y0) * (monthly_income - x0) / (x1 - x0)
                    break
        return Estimate.around(ratio, 0.10), notes

    def _band_points(self, region: str) -> list[tuple[float, float]]:
        total = self.by_income.get((region, "Total"), {})
        if not total:
            return []
        year = max(total)
        out = []
        for label, mid in BANDS:
            v = self.by_income.get((region, label), {}).get(year)
            if v:
                out.append((mid, v / total[year]))
        return out


def parse_epf(spend: list[Series], by_income: list[Series], cv: list[Series]) -> Epf:
    epf = Epf()
    for s in spend:
        region, code = _region(s), _code(s, _CODE, "Clases COICOP")
        if region and code and _measure(s) == "Gasto medio por hogar":
            vals = {p.year: p.value for p in s.points if p.value}  # 0 means not published
            if vals:
                epf.spend[(region, code)] = vals
    for s in by_income:
        region = _region(s)
        band = next((name for var, (_c, name) in s.dims.items() if var.startswith("Nivel de ingresos")), None)
        if region and band and _measure(s) == "Gasto medio por hogar":
            vals = {p.year: p.value for p in s.points if p.value}
            if vals:
                epf.by_income[(region, band)] = vals
    for s in cv:
        region, sub = _region(s), _code(s, _SUBGROUP, "Subgrupos COICOP")
        latest = s.latest()
        if region and sub and latest and latest.value:
            epf.cv[(region, sub)] = latest.value
    return epf
