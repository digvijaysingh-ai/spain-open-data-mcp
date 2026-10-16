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

# 1.0: residents only. 1.1: adds visitors' food and drink spend (hotel nights, EGATUR, ETR).
METHOD_VERSION = "opportunity-1.1"
PSEUDO_BUSINESSES = 1.0  # keeps the score finite where no business is mapped
SUPPLY_LOW_FACTOR = 0.8
SUPPLY_HIGH_FACTOR = 1.25
MAPPED_ONLY_HIGH_FACTOR = 1.5  # no DIRCE yardstick: the true count is above what is mapped
# Mapped well-covered places per resident vs the region, below which the map looks thin.
COMPLETENESS_LOW = 0.5
POPULATION_FLOOR = 0.5  # supply is never below half the population-proportional share
# Share of visitors' food and drink spend (EGATUR "manutención", which includes groceries)
# that goes to restaurants, cafés and bars. An assumption, shown in every answer that uses it.
EATING_OUT_SHARE = (0.6, 0.8, 1.0)


def market(
    households: Estimate, spend_per_household: Estimate, income_ratio: Estimate, family_share: Estimate
) -> Estimate:
    """Yearly household spending pool for a category, in euros.

    family_share splits a pool shared by several categories (restaurants, cafés and bars share
    EPF code 11.1.1) by their national share of mapped places.
    """
    return households * spend_per_household * income_ratio * family_share


def visitor_food_market(
    nights: float, daily_spend: float, food_share: float, family_share: Estimate
) -> Estimate:
    """Visitors' yearly spend in a category's food-and-drink pool, in euros:
    nights × spend per visitor per day × food-and-drink share × eating-out share × category share."""
    return Estimate.exact(nights * daily_spend * food_share) * Estimate(*EATING_OUT_SHARE) * family_share


def with_visitors(residents: Estimate, foreign: Estimate, domestic: Estimate) -> Estimate:
    """Residents plus foreign visitors; Spanish visitors raise only the high end, because their
    spending away from home is partly already in residents' household budgets."""
    return Estimate(
        residents.low + foreign.low,
        residents.base + foreign.base,
        residents.high + foreign.high + domestic.high,
    )


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
        by_places = category_companies * x.osm_area / x.osm_region_category if x.osm_region_category else 0.0
        # Floor: an area has at least half its population's share of the region's companies. A
        # town of thousands with no mapped bar has unmapped bars, not none; the floor also keeps
        # supply rising with every mapped place, so a new competitor never raises the score.
        floor = category_companies * x.population_share * POPULATION_FLOOR
        if by_places >= floor:
            base, method = by_places, "dirce_calibrated"
        else:
            base, method = floor, "dirce_population_floor"
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
