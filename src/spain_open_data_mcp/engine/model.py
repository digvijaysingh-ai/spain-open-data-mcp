"""The opportunity model: market in euros, calibrated supply, and the opportunity score.

Pure functions over Estimates, no network and no files, so the tools and the atlas build
compute identical numbers. The method is documented in docs/designs/opportunity-engine.md;
any change to it bumps METHOD_VERSION.

    households ──┐
    spend/hh  ───┼──▶ market (€/yr) ──┐
    income band ─┤                    ├──▶ density = market / (supply + 1) ──▶ score = density / region density
    family share ┘                    │
    DIRCE companies(region) ──┐       │
    OSM places(area, region) ─┴──▶ supply (businesses)
"""

from __future__ import annotations

from dataclasses import dataclass

from .estimate import Estimate

METHOD_VERSION = "opportunity-1.0"
PSEUDO_BUSINESSES = 1.0  # keeps the score finite where no business is mapped
SUPPLY_LOW_FACTOR = 0.8
SUPPLY_HIGH_FACTOR = 1.25
MAPPED_ONLY_HIGH_FACTOR = 1.5  # no DIRCE yardstick: the true count is above what is mapped
COMPLETENESS_LOW = (
    0.5  # mapped well-covered places per resident vs the region, below which the map looks thin
)


def market(
    households: Estimate, spend_per_household: Estimate, income_ratio: Estimate, family_share: Estimate
) -> Estimate:
    """Yearly household spending pool for a category, in euros.

    family_share splits a pool shared by several categories (restaurants, cafés and bars share
    EPF code 11.1.1) by their national share of mapped places.
    """
    return households * spend_per_household * income_ratio * family_share


@dataclass(frozen=True)
class SupplyInputs:
    """Counts behind a supply estimate for one category in area A of region R."""

    osm_area: int  # mapped places of the category in A
    osm_region_family: int  # mapped places of the category's DIRCE family in R
    osm_region_category: int  # mapped places of the category in R
    companies_region: int | None  # DIRCE companies of the family's CNAE code in R
    population_share: float  # A's share of R's residents, used when R has nothing mapped


def supply(x: SupplyInputs) -> tuple[Estimate, str]:
    """Number of businesses competing for the market in A, and the method used.

    With a DIRCE yardstick: the region's company count, allocated to A by A's share of the
    region's mapped places of the category (DIRCE counts the whole family, e.g. CNAE 56 for
    restaurants, cafés and bars, so the category's slice is its share of the family's places).
    Range: base × 0.8 to base × 1.25 (coverage is already in the base), never below what is
    mapped.

    Without one (chains such as supermarkets, pharmacies, clothes): the mapped count, with a
    high end of × 1.5. Scaling by another sector's map coverage was tried and dropped: for
    Madrid pharmacies it turned 1,857 mapped places into 15,298 (the real figure is about
    2,900), because pharmacies are mapped far better than medical practices.
    """
    if x.companies_region:
        category_companies = x.companies_region * (
            x.osm_region_category / x.osm_region_family if x.osm_region_family else 0.0
        )
        if x.osm_region_category:
            base = category_companies * x.osm_area / x.osm_region_category
            method = "dirce_calibrated"
        else:
            base = category_companies * x.population_share
            method = "dirce_population_share"
        est = Estimate(base * SUPPLY_LOW_FACTOR, base, base * SUPPLY_HIGH_FACTOR)
        return est.clamp_low(min(x.osm_area, base)), method
    base = float(x.osm_area)
    return Estimate(base, base, base * MAPPED_ONLY_HIGH_FACTOR), "mapped_only"


def density(market_eur: Estimate, businesses: Estimate) -> Estimate:
    """Market euros per business (plus one pseudo-business so empty areas stay finite)."""
    return market_eur / businesses.plus(PSEUDO_BUSINESSES)


def score(area_density: Estimate, reference_density: Estimate) -> Estimate:
    """Area density relative to the reference (region) density. 1.0 = as much market per
    business as the region; the reference uses its base value only."""
    if reference_density.base <= 0:
        raise ZeroDivisionError("reference density is 0")
    return area_density / reference_density.base


def completeness(area_places_per_10k: float | None, region_places_per_10k: float | None) -> float | None:
    """How well an area is mapped relative to its region, from well-covered categories
    (pharmacies, bars). Below COMPLETENESS_LOW the map looks thin and supply is unreliable."""
    if area_places_per_10k is None or not region_places_per_10k:
        return None
    return area_places_per_10k / region_places_per_10k
