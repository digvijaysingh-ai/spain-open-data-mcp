"""Data assembly for the opportunity engine: fetches and shapes inputs, then calls the pure model.

One MarketContext per request (or per atlas run) holds what is shared across areas: the EPF
tables, DIRCE companies per region, the OSM snapshot, and income-atlas units per province.
`evaluate()` turns one area into an AreaResult with every input it used, so tools can show
the assumptions behind each number.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..categories import BY_KEY, MARKET, Category, MarketProfile, family
from ..core.envelope import Source
from ..core.errors import DataError
from ..places.areas import SPAIN, code_prefixes, population_of, provinces_in_ccaa
from ..places.resolver import Place
from ..sources.ine.adrh import UnitValues, by_unit
from ..sources.ine.datasets import ADRH_NAME, DIRCE_NAME, EPF_NAME, IneData, latest_year, source_for
from ..sources.ine.epf import Epf
from ..sources.osm import snapshot as osm
from . import model
from .estimate import Estimate

HOUSEHOLD_SIZE_REL = 0.03  # income-atlas household size is two years older than the population
COMPLETENESS_CATEGORIES = ("pharmacy", "bar")  # well mapped everywhere; a thin count means a thin map
PARTIAL_INCOME_REGIONS = {"CA15": "Navarra", "CA16": "País Vasco"}  # own tax systems: ADRH partial


@dataclass
class Overrides:
    """User-set assumptions (market_size); each is bounds-checked by the tool."""

    spend_per_household: float | None = None
    capture_share: float | None = None
    competitors: float | None = None


@dataclass
class AreaResult:
    code: str
    name: str
    level: str
    region: str
    population: int | None
    households: Estimate | None
    monthly_income: float | None
    spend_per_household: Estimate
    income_ratio: Estimate
    family_share: Estimate
    market: Estimate | None
    mapped_places: int
    supply: Estimate
    supply_method: str
    density: Estimate | None
    score: Estimate | None
    completeness: float | None
    too_small: bool
    notes: list[str] = field(default_factory=list)


@dataclass
class MarketContext:
    ine: IneData
    snap: osm.Snapshot
    category: Category
    profile: MarketProfile
    epf: Epf
    sources: list[Source] = field(default_factory=list)
    _dirce: dict[str, dict[str, int]] = field(default_factory=dict)
    _demo: dict[str, dict[str, UnitValues]] = field(default_factory=dict)  # province -> units
    _income: dict[str, dict[str, UnitValues]] = field(default_factory=dict)
    _aggregates: dict[str, dict[str, UnitValues]] = field(default_factory=dict)  # kind -> units
    _family_share: Estimate | None = None
    _reference: dict[str, Estimate] = field(default_factory=dict)  # region -> density

    @classmethod
    async def create(cls, ine: IneData, snap: osm.Snapshot, category_key: str) -> MarketContext:
        category = BY_KEY[category_key]
        profile = MARKET.get(category_key)
        if profile is None:
            raise DataError(
                problem=f"No market model for '{category.label}'.",
                cause="Market sizing covers the atlas categories, whose household spending INE measures.",
                fix=f"Use one of: {', '.join(k for k in MARKET if k != 'guest_house')}.",
            )
        if profile.visitor_only:
            raise DataError(
                problem=f"'{category.label}' is bought by visitors, not residents.",
                cause="Its market needs visitor demand from INE's tourism statistics, which is not built yet.",
                fix="Use the businesses tool for supply, or a resident-spending category.",
            )
        epf, results = await ine.epf()
        year = str(epf.latest_year)
        ctx = cls(ine, snap, category, profile, epf)
        ctx.sources += [source_for(r, EPF_NAME, year) for r in results]
        return ctx

    # Inputs from INE

    async def dirce(self, region: str) -> dict[str, int]:
        if region not in self._dirce:
            result, counts = await self.ine.dirce_companies(region)
            self._dirce[region] = counts
            year = latest_year(result.series)
            self.sources.append(source_for(result, DIRCE_NAME, f"{year}-01-01" if year else "latest"))
        return self._dirce[region]

    async def _units(self, kind: str, province: str) -> dict[str, UnitValues]:
        store = self._demo if kind == "demographics" else self._income
        if province not in store:
            p = Place(code=province, level="province", name=province, province_code=province)
            res = await self.ine.adrh(kind, p, whole_province=True)  # type: ignore[arg-type]
            store[province] = by_unit(res.series)
            self.sources.append(source_for(res, f"{ADRH_NAME}: {kind}", str(latest_year(res.series))))
        return store[province]

    async def _aggregate(self, kind: str) -> dict[str, UnitValues]:
        if kind not in self._aggregates:
            res = await self.ine.adrh_benchmarks(kind, last_n=1)  # type: ignore[arg-type]
            self._aggregates[kind] = by_unit(res.series)
            self.sources.append(
                source_for(
                    res, f"{ADRH_NAME}: {kind}, Spain, regions and provinces", str(latest_year(res.series))
                )
            )
        return self._aggregates[kind]

    async def household_profile(self, code: str, level: str) -> tuple[float | None, float | None, list[str]]:
        """(average household size, net monthly income per household) from the income atlas."""
        notes: list[str] = []
        if level in ("province", "ccaa", "country"):
            demo, inc = await self._aggregate("demographics"), await self._aggregate("income")
            key = code
        else:
            prov = code[:2]
            demo, inc = await self._units("demographics", prov), await self._units("income", prov)
            key = code
            if _latest(demo, key, "avg_household_size") is None:
                notes.append(f"no income-atlas household figures for {code}; province values used")
                demo, inc, key = await self._aggregate("demographics"), await self._aggregate("income"), prov
        size = _latest(demo, key, "avg_household_size")
        income = _latest(inc, key, "net_income_per_household")
        return size, (income / 12 if income else None), notes

    # Counts from the OSM snapshot

    def osm_count(self, area_code: str, keys: list[str]) -> int:
        return self.snap.count(osm.Area(code_prefixes(area_code)), osm.Selection.categories(keys))

    def family_share(self) -> Estimate:
        """The category's slice of a shared spending pool, from national mapped places."""
        if self._family_share is None:
            members = [c.key for c in family(self.category.key)]
            if len(members) == 1:
                self._family_share = Estimate.exact(1.0)
            else:
                total = self.osm_count(SPAIN, members)
                own = self.osm_count(SPAIN, [self.category.key])
                share = own / total if total else 1 / len(members)
                self._family_share = Estimate.around(share, 0.10)
        return self._family_share

    async def supply_inputs(self, area_code: str, region: str, population_share: float) -> model.SupplyInputs:
        key = self.category.key
        osm_area = self.osm_count(area_code, [key])
        cnae = self.category.cnae
        if cnae:
            cnae_family = [c.key for c in BY_KEY.values() if c.cnae == cnae]
            return model.SupplyInputs(
                osm_area=osm_area,
                osm_region_family=self.osm_count(region, cnae_family),
                osm_region_category=self.osm_count(region, [key]),
                companies_region=(await self.dirce(region)).get(cnae),
                population_share=population_share,
            )
        return model.SupplyInputs(osm_area, 0, 0, None, population_share)

    # Evaluation

    async def evaluate(
        self,
        code: str,
        name: str,
        level: str,
        region: str,
        population: int | None,
        overrides: Overrides | None = None,
        reference: bool = True,
    ) -> AreaResult:
        ov = overrides or Overrides()
        notes: list[str] = []
        spend, spend_notes = self.epf.spend_per_household(self.profile.ecoicop, region)
        notes += spend_notes
        if ov.spend_per_household is not None:
            spend = Estimate.exact(ov.spend_per_household)
        size, monthly_income, hh_notes = await self.household_profile(code, level)
        notes += hh_notes
        households = Estimate.around(population / size, HOUSEHOLD_SIZE_REL) if population and size else None
        income_ratio, ratio_notes = (
            self.epf.income_ratio(region, monthly_income) if monthly_income else (Estimate.exact(1.0), [])
        )
        notes += ratio_notes
        share = self.family_share()
        market = model.market(households, spend, income_ratio, share) if households else None

        region_pop = population_of(region)[0]
        inputs = await self.supply_inputs(code, region, (population or 0) / region_pop if region_pop else 0.0)
        supply, method = model.supply(inputs)
        if ov.competitors is not None:
            supply, method = Estimate.exact(ov.competitors), "user"
        too_small = (population or 0) < self.profile.min_population
        density = model.density(market, supply) if market else None
        score = None
        if density is not None and not too_small and reference:
            ref = await self.reference_density(region)
            score = model.score(density, ref) if ref and ref.base > 0 else None
        return AreaResult(
            code=code,
            name=name,
            level=level,
            region=region,
            population=population,
            households=households,
            monthly_income=round(monthly_income, 0) if monthly_income else None,
            spend_per_household=spend,
            income_ratio=income_ratio,
            family_share=share,
            market=market,
            mapped_places=inputs.osm_area,
            supply=supply,
            supply_method=method,
            density=density,
            score=score,
            completeness=self.completeness(code, population, region, region_pop),
            too_small=too_small,
            notes=notes,
        )

    async def reference_density(self, region: str) -> Estimate | None:
        """Market per business across the whole region: the yardstick for the score."""
        if region not in self._reference:
            pop, _date = population_of(region)
            r = await self.evaluate(region, region, "ccaa", region, pop, reference=False)
            if r.density is None:
                return None
            self._reference[region] = r.density
        return self._reference[region]

    def completeness(self, code: str, population: int | None, region: str, region_pop: int) -> float | None:
        if not population or not region_pop:
            return None
        here = self.osm_count(code, list(COMPLETENESS_CATEGORIES)) / population * 10_000
        there = self.osm_count(region, list(COMPLETENESS_CATEGORIES)) / region_pop * 10_000
        return model.completeness(here, there)


def _latest(units: dict[str, UnitValues], code: str, indicator: str) -> float | None:
    u = units.get(code)
    found = u.latest(indicator) if u else None
    return found[1] if found else None


def region_of(p: Place) -> str:
    if p.level == "ccaa":
        return p.code
    if not p.ccaa_code:
        raise DataError(
            problem=f"No region for {p.name}.",
            cause="Market sizing needs the autonomous community for regional spending.",
            fix="Pass a municipality, province or region.",
        )
    return p.ccaa_code


def provinces_of(code: str) -> list[str]:
    return provinces_in_ccaa(code) if code.startswith("CA") else [code[:2]]
