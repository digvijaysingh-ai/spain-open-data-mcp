"""Parse income atlas (ADRH) series into per-unit indicator values."""

from __future__ import annotations

from dataclasses import dataclass, field

from .datasets import unit_code
from .tempus import Series

INDICATORS: dict[str, str] = {
    "Renta neta media por persona": "net_income_per_person",
    "Renta neta media por hogar": "net_income_per_household",
    "Renta bruta media por persona": "gross_income_per_person",
    "Renta bruta media por hogar": "gross_income_per_household",
    "Media de la renta por unidad de consumo": "mean_income_per_consumption_unit",
    "Mediana de la renta por unidad de consumo": "median_income_per_consumption_unit",
    "Índice de Gini": "gini",
    "Distribución de la renta P80/P20": "p80_p20",
    "Fuente de ingreso: salario": "pct_income_wages",
    "Fuente de ingreso: pensiones": "pct_income_pensions",
    "Fuente de ingreso: prestaciones por desempleo": "pct_income_unemployment_benefits",
    "Fuente de ingreso: otras prestaciones": "pct_income_other_benefits",
    "Fuente de ingreso: otros ingresos": "pct_income_other",
    "Edad media de la población": "mean_age",
    "Porcentaje de población menor de 18 años": "pct_under_18",
    "Porcentaje de población de 65 y más años": "pct_65_plus",
    "Tamaño medio del hogar": "avg_household_size",
    "Porcentaje de hogares unipersonales": "pct_single_person_households",
    "Población": "population",
    "Porcentaje de población española": "pct_spanish_nationality",
}


def level_of(code: str) -> str:
    if code == "ES":
        return "country"
    if code.startswith("CA"):
        return "ccaa"
    return {2: "province", 5: "municipality", 7: "district", 10: "tract"}.get(len(code), "unknown")


@dataclass
class UnitValues:
    code: str
    name: str
    level: str
    values: dict[str, dict[int, float]] = field(default_factory=dict)  # indicator -> year -> value
    suppressed: dict[str, set[int]] = field(default_factory=dict)  # indicator -> years withheld

    def latest(self, indicator: str) -> tuple[int, float] | None:
        years = self.values.get(indicator)
        if not years:
            return None
        y = max(years)
        return y, years[y]

    def value(self, indicator: str, year: int) -> float | None:
        return self.values.get(indicator, {}).get(year)

    def is_suppressed(self, indicator: str, year: int) -> bool:
        return year in self.suppressed.get(indicator, set())


def indicator_of(series: Series) -> str | None:
    for _code, name in series.dims.values():
        key = INDICATORS.get(name.strip())
        if key:
            return key
    return None


def by_unit(series: list[Series], into: dict[str, UnitValues] | None = None) -> dict[str, UnitValues]:
    units = into if into is not None else {}
    for s in series:
        key = indicator_of(s)
        unit = unit_code(s)
        if key is None or unit is None or not unit[0]:
            continue
        code, name = unit
        u = units.get(code)
        if u is None:
            u = units[code] = UnitValues(code=code, name=name.strip(), level=level_of(code))
        for p in s.points:
            if p.status == "ok" and p.value is not None:
                u.values.setdefault(key, {})[p.year] = p.value
            elif p.status == "suppressed":
                u.suppressed.setdefault(key, set()).add(p.year)
    return units
