"""Geocoding checks, tract geometry and the catchment tool, all offline."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from spain_open_data_mcp.core.errors import DataError, PlaceNotFound
from spain_open_data_mcp.sources.geo import geocode
from spain_open_data_mcp.sources.geo.geocode import locate, strip_unit_details
from spain_open_data_mcp.sources.geo.geometry import Geometry, GeometryStore
from spain_open_data_mcp.sources.ine.datasets import IneData
from spain_open_data_mcp.sources.osm.snapshot import SnapshotStore
from spain_open_data_mcp.tools.catchment import catchment, render
from tests.fixtures.geometry_mini import make_geometry
from tests.fixtures.osm_mini import make_snapshot

# Geocoding


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Calle Santiago 10, 3º B, Valladolid", "Calle Santiago 10, Valladolid"),
        ("Gran Vía 28 piso 4 puerta 2, Madrid", "Gran Vía 28, Madrid"),
        ("Calle Mayor 3, esc. A, 2ª planta, Alcalá de Henares", "Calle Mayor 3, Alcalá de Henares"),
        ("Plaza Mayor 1, bajo B, Valladolid", "Plaza Mayor 1, Valladolid"),
    ],
)
def test_floor_and_door_are_stripped(raw: str, clean: str):
    assert strip_unit_details(raw) == clean


async def test_coordinates_need_no_geocoder():
    loc = await locate("41.65234, -4.72461")
    assert (loc.lat, loc.lon) == (41.652, -4.725) and loc.source == "coordinates"


@pytest.mark.parametrize("bad", ["48.85, 2.35", "-4.72, 41.65"])
async def test_coordinates_outside_spain(bad: str):
    with pytest.raises(PlaceNotFound):
        await locate(bad)


async def test_address_needs_a_municipality():
    with pytest.raises(PlaceNotFound) as e:
        await locate("Calle Sierpes 40")
    assert "municipality" in e.value.problem


def _transport(cartociudad: list[dict], nominatim: list[dict] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if "cartociudad" in request.url.host:
            return httpx.Response(200, text="callback(" + json.dumps(cartociudad) + ")")
        return httpx.Response(200, json=nominatim or [])

    return httpx.MockTransport(handler)


def _candidate(
    address: str, muni: str, code: str, kind: str = "portal", lat: float = 41.65, lng: float = -4.72
) -> dict:
    return {
        "id": "x",
        "type": kind,
        "address": address,
        "muni": muni,
        "muniCode": code,
        "lat": lat,
        "lng": lng,
    }


async def test_cartociudad_result_in_another_town_is_rejected(monkeypatch: pytest.MonkeyPatch):
    t = _transport([_candidate("GRAN VIA GRAN VIA 27", "Mula", "30029")])
    with pytest.raises(PlaceNotFound) as e:
        await locate("Gran Vía 28, Madrid", transport=t)
    assert "Mula" in e.value.cause


async def test_partial_street_name_needs_the_same_last_word():
    ok = _transport([_candidate("CALLE MARQUES DE LARIOS 5, Málaga", "Málaga", "29067")])
    loc = await locate("Calle Larios 5, Málaga", transport=ok)
    assert "MARQUES DE LARIOS" in loc.label
    wrong = _transport([_candidate("AVENIDA DE LA GRAN VIA DE HORTALEZA 27, Madrid", "Madrid", "28079")])
    with pytest.raises(PlaceNotFound):
        await locate("Gran Vía 28, Madrid", transport=wrong)


async def test_points_of_interest_are_not_streets():
    t = _transport(
        [_candidate("FUNDACION BETESDA [CALLE SIERPES 3], Sevilla", "Sevilla", "41091", kind="toponimo")]
    )
    with pytest.raises(PlaceNotFound):
        await locate("Calle Sierpes 40, Sevilla", transport=t)


async def test_nominatim_village_with_the_province_name_is_rejected(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(geocode, "_last_nominatim", 0.0)
    village = [
        {"lat": "42.27", "lon": "-5.10", "address": {"road": "Calle Santiago", "village": "Melgar de Arriba"}}
    ]
    with pytest.raises(PlaceNotFound) as e:
        await locate("Calle Santiago 10, Valladolid", transport=_transport([], village))
    assert "Melgar de Arriba" in e.value.cause


async def test_nominatim_fallback_in_the_right_town(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(geocode, "_last_nominatim", 0.0)
    hit = [
        {
            "lat": "40.4199",
            "lon": "-3.7019",
            "address": {"road": "Gran Vía", "house_number": "28", "city": "Madrid"},
        }
    ]
    loc = await locate("Gran Vía 28, Madrid", transport=_transport([], hit))
    assert loc.source == "OpenStreetMap Nominatim" and (loc.lat, loc.lon) == (40.42, -3.702)


# Geometry


def test_circle_shares(tmp_path: Path):
    g = Geometry(make_geometry(tmp_path / "g.sqlite"))
    shares = {t.code: t.share for t in g.circle_shares(-4.7, 41.6, 500)}
    assert set(shares) == {"4718601001", "4718601003", "4718604001", "4718610001"}
    # a 500 m circle at the corner of four ~1.7 x 2.2 km squares covers a quarter circle of each
    assert all(0.04 < s < 0.06 for s in shares.values())
    assert g.tract_at(-4.71, 41.61) == "4718601001"
    assert g.tract_at(-3.0, 43.6) is None


# Catchment tool


@pytest.fixture
def stores(tmp_path: Path) -> tuple[SnapshotStore, GeometryStore]:
    return (
        SnapshotStore(fixed_path=make_snapshot(tmp_path / "s.sqlite")),
        GeometryStore(fixed_path=make_geometry(tmp_path / "g.sqlite")),
    )


async def test_catchment_profile(ine: IneData, stores):
    snap, geo = stores
    env = await catchment(ine, snap, geo, "41.601, -4.705", 800)
    d = env.data
    assert d.tracts == 4 and d.residents > 0 and d.households and d.net_income_per_household
    assert (
        d.businesses == 39
    )  # every mini-snapshot business (all towns) sits at the same point; school excluded
    assert d.by_category[0].count >= 4
    assert d.market is None
    assert "Catchment: 800 m" in render(env)
    assert any("rounded to about 100 m" in c.message for c in env.checks)


async def test_catchment_with_a_category(ine: IneData, stores):
    snap, geo = stores
    env = await catchment(ine, snap, geo, "41.601, -4.705", 800, category="bar")
    m = env.data.market
    assert m and m.market_eur and m.market_eur.base > 0 and m.mapped_places == 6
    assert env.data.method_version


async def test_catchment_rejects_bad_radius_and_sea(ine: IneData, stores):
    snap, geo = stores
    with pytest.raises(DataError):
        await catchment(ine, snap, geo, "41.601, -4.705", 20_000)
    with pytest.raises(PlaceNotFound):
        await catchment(ine, snap, geo, "43.6, -3.0", 800)


# Walking catchments


async def test_walk_area_follows_the_grid(tmp_path: Path):
    from spain_open_data_mcp.sources.geo.walk import WalkGraphs, walk_area
    from tests.fixtures.walk_mini import CENTRE, make_walk_graph, serve

    published = make_walk_graph(tmp_path / "published")
    graphs = WalkGraphs(tmp_path / "cache", transport=serve(published), offline=False)
    assert await graphs.provinces_near(*CENTRE, 500) == ["47"]
    g = await graphs.graph("47")
    area = walk_area([g], *CENTRE, 5)  # 375 m on a 100 m grid: Manhattan distance <= 3 blocks
    assert area.reached_nodes == 25  # the diamond |dx| + |dy| <= 3
    assert 0 < area.area_km2 < 3.1416 * 0.375**2  # smaller than the straight-line circle


async def test_walk_graph_checksum_is_enforced(tmp_path: Path):
    import json as _json

    from spain_open_data_mcp.core.errors import UpstreamError
    from spain_open_data_mcp.sources.geo.walk import MANIFEST_NAME, WalkGraphs
    from tests.fixtures.walk_mini import make_walk_graph, serve

    published = make_walk_graph(tmp_path / "published")
    m = _json.loads((published / MANIFEST_NAME).read_text())
    m["provinces"]["47"]["sha256"] = "0" * 64
    (published / MANIFEST_NAME).write_text(_json.dumps(m))
    with pytest.raises(UpstreamError):
        await WalkGraphs(tmp_path / "cache", transport=serve(published), offline=False).graph("47")


async def test_far_from_any_street(tmp_path: Path):
    from spain_open_data_mcp.sources.geo.walk import WalkGraphs, walk_area
    from tests.fixtures.walk_mini import make_walk_graph, serve

    g = await WalkGraphs(
        tmp_path / "c", transport=serve(make_walk_graph(tmp_path / "p")), offline=False
    ).graph("47")
    with pytest.raises(PlaceNotFound):
        walk_area([g], -4.65, 41.6, 10)  # about 4 km east of the grid


async def test_catchment_walking_network_and_fallback(ine: IneData, stores, tmp_path: Path):
    from spain_open_data_mcp.sources.geo.walk import WalkGraphs
    from tests.fixtures.walk_mini import make_walk_graph, serve

    snap, geo = stores
    walk_store = WalkGraphs(tmp_path / "wc", transport=serve(make_walk_graph(tmp_path / "wp")), offline=False)
    env = await catchment(ine, snap, geo, "41.601, -4.705", walk_minutes=10, walk_store=walk_store)
    assert env.data.area_method == "walking network" and env.data.walk_minutes == 10
    assert "minutes' walk" in render(env)
    empty = WalkGraphs(tmp_path / "none", transport=serve(tmp_path / "missing"), offline=False)
    env = await catchment(ine, snap, geo, "41.601, -4.705", walk_minutes=10, walk_store=empty)
    assert env.data.area_method == "walking estimate (circle)"
    assert any("No walking graph" in c.message for c in env.checks)
    with pytest.raises(DataError):
        await catchment(ine, snap, geo, "41.601, -4.705", walk_minutes=90, walk_store=walk_store)
