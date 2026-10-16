"""The opportunity engine's arithmetic: Estimate rules and the model's promises."""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from spain_open_data_mcp.engine import model
from spain_open_data_mcp.engine.estimate import Estimate
from spain_open_data_mcp.engine.model import SupplyInputs

positive = st.floats(min_value=0.01, max_value=1e7, allow_nan=False, allow_infinity=False)
non_negative = st.floats(min_value=0.0, max_value=1e7, allow_nan=False, allow_infinity=False)


@st.composite
def estimates(draw: st.DrawFn, low_min: float = 0.0) -> Estimate:
    vals = sorted(draw(st.lists(st.floats(min_value=low_min, max_value=1e7), min_size=3, max_size=3)))
    return Estimate(*vals)


# Estimate


def test_estimate_rejects_disorder_and_negatives():
    with pytest.raises(ValueError):
        Estimate(2, 1, 3)
    with pytest.raises(ValueError):
        Estimate(-1, 0, 1)
    with pytest.raises(ValueError):
        Estimate(0, math.nan, 1)


def test_division_pairs_the_crossed_ends():
    a, b = Estimate(10, 20, 30), Estimate(2, 4, 5)
    assert a / b == Estimate(2, 5, 15)


def test_around_caps_the_spread():
    assert Estimate.around(100, 0.9) == Estimate(50, 100, 150)


@given(estimates(), estimates())
def test_sums_and_products_stay_ordered(a: Estimate, b: Estimate):
    for r in (a + b, a * b):
        assert r.low <= r.base <= r.high


@given(estimates(), estimates(low_min=0.01))
def test_quotients_stay_ordered(a: Estimate, b: Estimate):
    r = a / b
    assert r.low <= r.base <= r.high


@given(estimates(), positive)
def test_clamp_low_never_passes_base(a: Estimate, floor: float):
    c = a.clamp_low(floor)
    assert c.low <= c.base == a.base and c.low >= min(a.low, a.base)


# Model promises


def _supply(osm_area: int, companies: int | None = 100, osm_region: int = 50) -> Estimate:
    est, _ = model.supply(SupplyInputs(osm_area, osm_region, osm_region, companies, 0.1))
    return est


def test_supply_calibrates_to_dirce():
    est, method = model.supply(SupplyInputs(10, 50, 25, 200, 0.1))
    # 200 companies in the family, this category is 25 of 50 mapped -> 100 companies, 10 of 25 here -> 40
    assert method == "dirce_calibrated" and est.base == pytest.approx(40)
    assert est.low == pytest.approx(32) and est.high == pytest.approx(50)


def test_supply_never_below_mapped():
    est, _ = model.supply(SupplyInputs(10, 1000, 1000, 50, 0.1))  # DIRCE would say 0.5
    assert est.low >= 0.5 and est.low <= est.base


def test_supply_uses_population_when_region_has_nothing_mapped():
    est, method = model.supply(SupplyInputs(0, 10, 0, 100, 0.2))
    assert method == "dirce_population_share" and est.base == 0.0


def test_supply_without_yardstick_is_the_mapped_count():
    est, method = model.supply(SupplyInputs(18, 0, 0, None, 0.1))
    assert method == "mapped_only" and (est.low, est.base, est.high) == (18, 18, 27)


@given(
    st.integers(min_value=0, max_value=500), st.integers(min_value=1, max_value=500), estimates(low_min=1.0)
)
def test_more_competitors_never_raise_the_score(osm_area: int, extra: int, market: Estimate):
    ref = Estimate(1000, 1000, 1000)
    fewer = model.score(model.density(market, _supply(osm_area)), ref)
    more = model.score(model.density(market, _supply(osm_area + extra)), ref)
    assert more.low <= fewer.low and more.base <= fewer.base and more.high <= fewer.high


@given(positive, positive, positive)
def test_more_households_never_lower_the_market(h: float, extra: float, spend: float):
    one = Estimate.exact(1.0)
    small = model.market(Estimate.exact(h), Estimate.exact(spend), one, one)
    big = model.market(Estimate.exact(h + extra), Estimate.exact(spend), one, one)
    assert big.base >= small.base


@given(estimates(), st.integers(min_value=0, max_value=10_000))
def test_scores_are_always_finite(market: Estimate, mapped: int):
    s = model.score(model.density(market, _supply(mapped)), Estimate.exact(1.0))
    assert all(math.isfinite(v) for v in (s.low, s.base, s.high))


def test_completeness():
    assert model.completeness(5.0, 10.0) == 0.5
    assert model.completeness(0.0, 10.0) == 0.0
    assert model.completeness(None, 10.0) is None
    assert model.completeness(5.0, 0.0) is None
