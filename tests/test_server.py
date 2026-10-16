from pathlib import Path

from mcp import Client

from spain_open_data_mcp.server import _Services, mcp
from spain_open_data_mcp.sources.ine.datasets import IneData
from spain_open_data_mcp.sources.osm.snapshot import SnapshotStore
from tests.fixtures.osm_mini import make_snapshot


async def test_tools_are_annotated_for_the_connector_directory():
    async with Client(mcp) as c:
        tools = (await c.list_tools()).tools
    assert {t.name for t in tools} == {
        "resolve_place",
        "demographics",
        "income",
        "businesses",
        "market_size",
        "compare_areas",
        "price_band",
        "chart",
        "catchment",
        "find_datasets",
        "read_dataset",
        "rents",
        "crime",
    }
    for t in tools:
        assert t.title, t.name
        assert t.annotations and t.annotations.read_only_hint is True, t.name
        assert t.annotations.destructive_hint is False
        assert len(t.name) <= 64
        assert t.output_schema
        assert t.description and len(t.description) > 80


async def test_call_income_through_mcp(ine: IneData):
    _Services.set_ine(ine)
    async with Client(mcp) as c:
        r = await c.call_tool("income", {"place": "Valladolid"})
    assert not r.is_error
    text = r.content[0].text  # type: ignore[union-attr]
    assert "€16,356" in text and "Sources:" in text and "Confidence:" in text
    assert r.structured_content and r.structured_content["data"]["figures"]["net_income_per_person"] == 16356


async def test_call_businesses_through_mcp(ine: IneData, tmp_path: Path):
    _Services.set_ine(ine)
    _Services.set_osm(SnapshotStore(fixed_path=make_snapshot(tmp_path / "mini.sqlite")))
    async with Client(mcp) as c:
        r = await c.call_tool("businesses", {"place": "Valladolid", "category": "gimnasios"})
    assert not r.is_error
    text = r.content[0].text  # type: ignore[union-attr]
    assert "Count: 6" in text and "© OpenStreetMap contributors" in text
    assert r.structured_content and r.structured_content["data"]["area"]["count"] == 6


async def test_errors_are_actionable():
    async with Client(mcp) as c:
        r = await c.call_tool("demographics", {"place": "Atlantis"})
    assert r.is_error
    text = r.content[0].text  # type: ignore[union-attr]
    assert "Problem:" in text and "Fix:" in text


async def test_licence_resource():
    async with Client(mcp) as c:
        res = await c.read_resource("spain-open-data://data-licences")
    assert "CC BY 4.0" in res.contents[0].text  # type: ignore[union-attr]


async def test_prompts():
    async with Client(mcp) as c:
        listed = {p.name for p in (await c.list_prompts()).prompts}
        assert listed == {"size_a_market", "pick_a_location", "explore_public_datasets"}
        got = await c.get_prompt(
            "pick_a_location", {"business": "gym", "area": "Valladolid", "near": "Plaza Mayor, Valladolid"}
        )
    text = got.messages[0].content.text  # type: ignore[union-attr]
    assert "compare_areas" in text and "catchment" in text and "Sources and caveats" in text
