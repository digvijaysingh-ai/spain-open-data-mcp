"""price_band, chart, the data-status resource, call logging and startup imports."""

from __future__ import annotations

import base64
import logging
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import Client

from spain_open_data_mcp.core.errors import DataError
from spain_open_data_mcp.engine.model import METHOD_VERSION
from spain_open_data_mcp.server import _Services, mcp
from spain_open_data_mcp.sources.ine.datasets import IneData
from spain_open_data_mcp.sources.osm.snapshot import SnapshotStore
from spain_open_data_mcp.tools import chart as chart_mod
from spain_open_data_mcp.tools.chart import chart
from spain_open_data_mcp.tools.price import price_band, render
from tests.fixtures.osm_mini import make_snapshot

PNG_MAGIC = b"\x89PNG"


@pytest.fixture
def store(tmp_path: Path) -> SnapshotStore:
    return SnapshotStore(fixed_path=make_snapshot(tmp_path / "mini.sqlite"))


# price_band


async def test_price_band_purchasing_power_only(ine: IneData):
    env = await price_band(ine, "Valladolid", 39.9)
    d = env.data
    assert d.inflation_factor == 1.0 and d.reference_price_today == 39.9
    assert d.purchasing_power_index > 1  # Valladolid above Spain's median income per consumption unit
    assert d.band.low == pytest.approx(39.9) and d.band.low <= d.band.base <= d.band.high
    assert d.band.high == pytest.approx(39.9 * d.purchasing_power_index, rel=1e-3)
    assert d.local_annual_inflation_pct is not None
    assert any(c.kind == "estimate" for c in env.checks)
    assert "Suggested range" in render(env)


async def test_price_band_uprates_with_the_category_cpi(ine: IneData):
    env = await price_band(ine, "Valladolid", 40.0, category="gym", price_month="2019-06")
    d = env.data
    assert d.cpi_group and d.cpi_group.startswith("09")
    assert d.inflation_factor > 1.1  # sport and recreation prices rose since 2019
    assert d.reference_price_today == pytest.approx(40.0 * d.inflation_factor, rel=1e-3)
    assert any(s.dataset_id == "78680" for s in env.sources)


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"reference_price": -1}, "reference_price"),
        ({"reference_price": 10, "price_month": "June 2020"}, "not a month"),
        ({"reference_price": 10, "price_month": "1990-01", "category": "gym"}, "No CPI"),
        ({"reference_price": 10, "price_month": "2019-01"}, "No CPI"),  # general index starts in 2025
        ({"reference_price": 10, "category": "lawyer"}, "No price group"),
    ],
)
async def test_price_band_errors(ine: IneData, kwargs, fragment):
    with pytest.raises(DataError) as e:
        await price_band(ine, "Valladolid", **kwargs)
    assert fragment in e.value.problem


# chart


async def test_opportunity_chart_returns_a_png_and_the_text(ine: IneData, store: SnapshotStore):
    r = await chart(ine, store, "opportunity", "Valladolid", "gym")
    images = [c for c in r.content if c.type == "image"]
    texts = [c for c in r.content if c.type == "text"]
    assert images and base64.b64decode(images[0].data).startswith(PNG_MAGIC)
    assert texts and "Sources:" in texts[0].text
    assert r.structured_content and r.structured_content["data"]["category"] == "gym"


async def test_income_chart(ine: IneData):
    r = await chart(ine, SnapshotStore(fixed_path=Path("/nonexistent")), "income", "Valladolid")
    assert any(c.type == "image" for c in r.content)


async def test_failed_chart_keeps_the_answer(ine: IneData, monkeypatch: pytest.MonkeyPatch):
    def broken(*_a, **_k):
        raise RuntimeError("font missing")

    monkeypatch.setattr(chart_mod, "draw_income", broken)
    r = await chart(ine, SnapshotStore(fixed_path=Path("/nonexistent")), "income", "Valladolid")
    assert not any(c.type == "image" for c in r.content)
    text = next(c for c in r.content if c.type == "text").text
    assert "Chart unavailable: font missing" in text and "€16,356" in text


async def test_opportunity_chart_needs_a_category(ine: IneData, store: SnapshotStore):
    with pytest.raises(DataError):
        await chart(ine, store, "opportunity", "Valladolid")


# Operations


async def test_status_resource(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SPAIN_MCP_OSM_DB", str(make_snapshot(tmp_path / "s.sqlite")))
    async with Client(mcp) as c:
        res = await c.read_resource("spain-open-data://status")
    text = res.contents[0].text  # type: ignore[union-attr]
    assert "OpenStreetMap snapshot" in text and METHOD_VERSION in text and "Release calendar" in text


async def test_every_call_is_logged_without_arguments(ine: IneData, caplog: pytest.LogCaptureFixture):
    _Services.set_ine(ine)
    caplog.set_level(logging.INFO, logger="spain_open_data_mcp.server")
    async with Client(mcp) as c:
        await c.call_tool("income", {"place": "Valladolid"})
        await c.call_tool("income", {"place": "Atlantis"})
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("tool=")]
    assert "tool=income outcome=ok" in lines[0]
    assert "tool=income outcome=PlaceNotFound" in lines[1]
    assert not any("Valladolid" in line or "Atlantis" in line for line in lines)


def test_server_import_does_not_load_heavy_libraries():
    code = (
        "import sys, spain_open_data_mcp.server\n"
        "heavy = [m for m in ('matplotlib', 'shapely', 'numpy') if m in sys.modules]\n"
        "print(','.join(heavy))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""
