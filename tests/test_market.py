"""EPF parsing and the market_size / compare_areas tools, offline against recorded INE data."""

from __future__ import annotations

from pathlib import Path

import pytest

from spain_open_data_mcp.categories import ATLAS_CATEGORIES, MARKET, family
from spain_open_data_mcp.core.errors import DataError
from spain_open_data_mcp.engine.model import METHOD_VERSION
from spain_open_data_mcp.sources.ine.datasets import IneData
from spain_open_data_mcp.sources.ine.epf import Epf
from spain_open_data_mcp.sources.osm.snapshot import SnapshotStore
from spain_open_data_mcp.tools.market import compare_areas, market_size, render_compare, render_market
from tests.fixtures.osm_mini import make_snapshot


@pytest.fixture
def store(tmp_path: Path) -> SnapshotStore:
    return SnapshotStore(fixed_path=make_snapshot(tmp_path / "mini.sqlite"))


@pytest.fixture
async def epf(ine: IneData) -> Epf:
    return (await ine.epf())[0]


# Category registry


def test_every_atlas_category_has_a_complete_profile():
    assert len(ATLAS_CATEGORIES) == 20
    for key in MARKET:
        m = MARKET[key]
        assert m.ecoicop and m.scope in ("local", "destination") and m.min_population >= 0


def test_food_and_drink_share_one_pool():
    assert {c.key for c in family("bar")} == {"restaurant", "cafe", "bar", "fast_food"}


# EPF


async def test_epf_spend_per_household(epf: Epf):
    assert epf.latest_year == 2025
    es, notes = epf.spend_per_household(("11.1.1",), "ES")
    assert 2_600 < es.base < 2_900 and not notes  # restaurants and cafés, euros per household
    cyl, _ = epf.spend_per_household(("11.1.1",), "CA07")
    assert cyl.base < es.base  # Castilla y León spends less than Spain on eating out
    assert cyl.low < cyl.base < cyl.high


async def test_epf_prefix_codes_sum_subgroups(epf: Epf):
    food, _ = epf.spend_per_household(("01.",), "ES")
    bread, _ = epf.spend_per_household(("01.1.1",), "ES")
    assert food.base > 5 * bread.base  # bread is about 13% of food and soft drinks


async def test_epf_unpublished_zero_is_missing_not_zero(epf: Epf):
    for (region, _code), years in epf.spend.items():
        assert all(v > 0 for v in years.values()), region


async def test_epf_income_ratio_interpolates_and_clamps(epf: Epf):
    low, _ = epf.income_ratio("CA07", 400)
    mid, _ = epf.income_ratio("CA07", 2_000)
    high, _ = epf.income_ratio("CA07", 9_000)
    assert low.base < mid.base < high.base
    assert epf.income_ratio("CA07", 100)[0].base == low.base  # clamped below the first midpoint


async def test_epf_unknown_code(epf: Epf):
    with pytest.raises(KeyError):
        epf.spend_per_household(("99.9.9",), "ES")


# market_size


async def test_market_size_gyms_in_valladolid(ine: IneData, store: SnapshotStore):
    env = await market_size(ine, store, "Valladolid", "gimnasios", capture_share=0.05)
    d = env.data
    a = d.area
    assert d.method_version == METHOD_VERSION and d.category == "gym" and d.region == "CA07"
    assert a.population == 302_614
    assert a.market_eur and 20e6 < a.market_eur.base < 60e6
    assert a.market_eur.low < a.market_eur.base < a.market_eur.high
    assert d.obtainable_eur and d.obtainable_eur.base == pytest.approx(a.market_eur.base * 0.05, rel=1e-3)
    assert a.mapped_places == 6 and a.supply_method == "mapped_only"
    assert a.score is not None
    assert any(s.user_set for s in d.assumptions)
    assert env.confidence in ("medium", "low")
    assert any("upper bounds" in c.message for c in env.checks)
    assert any(s.dataset_id == "73993" for s in env.sources)
    text = render_market(env)
    assert "Yearly household spending pool" in text and "user assumption" in text


async def test_market_size_restaurants_use_dirce_and_flag_destination(ine: IneData, store: SnapshotStore):
    env = await market_size(ine, store, "4718601", "restaurants")
    assert env.data.area.supply_method == "dirce_calibrated"
    assert 0 < env.data.family_share.base < 1
    assert any("destination category" in c.message for c in env.checks)
    assert any("visitors are not counted" in c.message for c in env.checks)


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"spend_per_household": -5}, "spend_per_household"),
        ({"spend_per_household": 1e9}, "spend_per_household"),
        ({"capture_share": 1.5}, "capture_share"),
        ({"competitors": -1}, "competitors"),
    ],
)
async def test_market_size_rejects_out_of_range_overrides(
    ine: IneData, store: SnapshotStore, kwargs, fragment
):
    with pytest.raises(DataError) as e:
        await market_size(ine, store, "Valladolid", "gym", **kwargs)
    assert fragment in e.value.problem


async def test_market_size_refuses_unknown_and_visitor_only(ine: IneData, store: SnapshotStore):
    with pytest.raises(DataError):
        await market_size(ine, store, "Valladolid", "spaceports")
    with pytest.raises(DataError):
        await market_size(ine, store, "Valladolid", "lawyer")  # no household spending code
    with pytest.raises(DataError) as e:
        await market_size(ine, store, "Valladolid", "guest_house")
    assert "visitors" in e.value.problem


async def test_competitor_override_changes_supply_only(ine: IneData, store: SnapshotStore):
    base = await market_size(ine, store, "Valladolid", "gym")
    alt = await market_size(ine, store, "Valladolid", "gym", competitors=60)
    assert alt.data.area.market_eur == base.data.area.market_eur
    assert alt.data.area.supply.base == 60 and alt.data.area.supply_method == "user"
    assert alt.data.area.score and base.data.area.score
    assert alt.data.area.score.base < base.data.area.score.base


# compare_areas


async def test_compare_districts_ranks_by_cautious_score(ine: IneData, store: SnapshotStore):
    env = await compare_areas(ine, store, "Valladolid", "gym")
    d = env.data
    assert d.level == "district" and d.areas
    reliable = [a for a in d.areas if not a.thin_map]
    lows = [a.score.low for a in reliable if a.score]
    assert lows == sorted(lows, reverse=True)
    # thinly mapped districts come after every reliable one
    flags = [a.thin_map for a in d.areas]
    assert flags == sorted(flags)
    assert "Ranked by the low end" in render_compare(env)


async def test_compare_rejects_impossible_levels(ine: IneData, store: SnapshotStore):
    with pytest.raises(DataError):
        await compare_areas(ine, store, "47", "gym", level="district")
