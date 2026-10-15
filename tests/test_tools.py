from spain_open_data_mcp.sources.ine.datasets import IneData
from spain_open_data_mcp.tools.demographics import demographics
from spain_open_data_mcp.tools.demographics import render as render_demo
from spain_open_data_mcp.tools.income import income, top_coded
from spain_open_data_mcp.tools.income import render as render_income


async def test_demographics_valladolid(ine: IneData):
    env = await demographics(ine, "Valladolid", "district")
    a = env.data.area
    assert a.place.code == "47186"
    assert a.population == 301798 and a.population_year == 2025
    assert a.official_population == 302614
    assert a.male is not None and a.female is not None and a.male + a.female == a.population
    assert sum(b.population or 0 for b in a.age_bands) == a.population
    assert a.households is not None and a.households.avg_household_size == 2.3
    assert len(env.data.sub_areas) == 12
    kinds = {c.kind for c in env.checks}
    assert {"cross_check", "estimate"} <= kinds
    assert {s.dataset_id for s in env.sources} >= {"65034", "29005"}
    text = render_demo(env)
    assert "301,798" in text and "4718612" in text


async def test_income_valladolid_with_benchmarks(ine: IneData):
    env = await income(ine, "47186", "district")
    f = env.data.figures
    assert f.year == 2023
    assert f.net_income_per_person == 16356
    assert f.gini == 30.5
    assert [b.code for b in env.data.benchmarks] == ["47", "CA07", "ES"]
    assert env.data.trend[0].year == 2019 and len(env.data.trend) == 5
    top = env.data.sub_areas[0]
    assert top.code == "4718601" and top.figures.net_income_per_person == 27922
    assert env.data.sub_areas_total == 12
    assert any(c.kind == "stale" for c in env.checks)
    assert "Benchmark España" in render_income(env)


async def test_every_figure_has_a_dated_source(ine: IneData):
    env = await income(ine, "Valladolid")
    for s in env.sources:
        assert s.url.startswith("https://www.ine.es/") and s.data_period and s.licence == "CC BY 4.0"


def test_top_coding_detected():
    assert top_coded([36918, 36918, 36918, 20000]) == (36918, 3)
    assert top_coded([36918, 30000, 20000]) is None
    assert top_coded([None, None]) is None
