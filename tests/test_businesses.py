from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from spain_open_data_mcp.core.errors import DataError, UpstreamError
from spain_open_data_mcp.sources.ine.datasets import IneData
from spain_open_data_mcp.sources.osm.categories import CATEGORIES, classify, find_category
from spain_open_data_mcp.sources.osm.snapshot import (
    DB_NAME,
    MANIFEST_NAME,
    SCHEMA_VERSION,
    Area,
    Selection,
    Snapshot,
    SnapshotStore,
)
from spain_open_data_mcp.tools.businesses import businesses, render, resolve_what
from tests.fixtures.osm_mini import make_snapshot


@pytest.fixture
def snapshot_path(tmp_path: Path) -> Path:
    return make_snapshot(tmp_path / "mini.sqlite")


@pytest.fixture
def store(snapshot_path: Path) -> SnapshotStore:
    return SnapshotStore(fixed_path=snapshot_path)


# Categories


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"amenity": "restaurant", "name": "Casa Pepe"}, ("amenity=restaurant", "restaurant")),
        ({"amenity": "pharmacy", "healthcare": "pharmacy"}, ("amenity=pharmacy", "pharmacy")),
        ({"amenity": "parking", "shop": "bakery"}, ("shop=bakery", "bakery")),
        ({"shop": "yes"}, ("shop=yes", None)),
        ({"shop": "gift"}, ("shop=gift", None)),
        ({"office": "government"}, None),
        ({"shop": "mall"}, None),
        ({"shop": "vacant"}, None),
        ({"amenity": "bench"}, None),
        ({"shop": "clothes", "disused": "yes"}, None),
    ],
)
def test_classify(tags: dict[str, str], expected: tuple[str, str | None] | None):
    assert classify(tags) == expected


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("gyms", "gym"),
        ("Gimnasios", "gym"),
        ("peluquerías", "hairdresser"),
        ("hair salons", "hairdresser"),
        ("bakeries", "bakery"),
        ("agencias de viajes", "travel_agency"),
        ("estate_agent", "estate_agent"),
    ],
)
def test_find_category(text: str, key: str):
    c = find_category(text)
    assert c is not None and c.key == key


def test_category_keys_and_tags_are_unique():
    keys = [c.key for c in CATEGORIES]
    tags = [t for c in CATEGORIES for t in c.tags]
    assert len(keys) == len(set(keys))
    assert len(tags) == len(set(tags))


def test_unknown_category_lists_the_options():
    with pytest.raises(DataError) as e:
        resolve_what("spaceports")
    assert "gym" in e.value.fix and "shop=bakery" in e.value.fix


def test_bad_tag_is_rejected():
    with pytest.raises(DataError):
        resolve_what("shop=")


# Snapshot queries


def test_area_prefix_matching(snapshot_path: Path):
    snap = Snapshot(snapshot_path)
    gyms = Selection.categories(["gym"])
    assert snap.count(Area(("47186",)), gyms) == 6
    assert snap.count(Area(("47",)), gyms) == 7
    assert snap.count(Area(()), gyms) == 17
    assert snap.count(Area(("47", "28")), gyms) == 17
    assert snap.count_by_unit(Area(("47186",)), 7, gyms) == {"4718601": 3, "4718604": 1, "4718612": 2}
    assert snap.area_km2(Area(("47186",))) == pytest.approx(6.0)
    assert snap.brands(Area(()), gyms) == ([("Basic-Fit", 12)], 12)


def test_all_businesses_leaves_out_public_services(snapshot_path: Path):
    snap = Snapshot(snapshot_path)
    assert snap.count(Area(("47186",))) == 20
    assert snap.count(Area(("47186",)), Selection.all_businesses()) == 19


# The tool


async def test_gyms_in_valladolid(ine: IneData, store: SnapshotStore):
    env = await businesses(ine, store, "Valladolid", "gyms")
    d = env.data
    assert d.query == "gyms and fitness centres"
    assert d.area.count == 6
    assert d.area.population == 302_614  # bundled Padrón
    assert d.area.per_10k_residents == pytest.approx(0.2)
    assert d.area.area_km2 == pytest.approx(6.0)
    assert [b.code for b in d.benchmarks] == ["47", "CA07", "ES"]
    assert [b.count for b in d.benchmarks] == [7, 7, 17]
    assert d.top_brands[0].brand == "Basic-Fit" and d.top_brands[0].count == 2
    assert d.coverage is None  # no DIRCE activity matches gyms closely enough
    assert env.confidence == "medium"
    publishers = {s.publisher for s in env.sources}
    assert any("OpenStreetMap" in p for p in publishers)
    assert any(s.licence == "ODbL 1.0" for s in env.sources)


async def test_food_and_drink_cross_check_uses_the_whole_division(ine: IneData, store: SnapshotStore):
    env = await businesses(ine, store, "Valladolid", "bar")
    cov = env.data.coverage
    assert cov is not None
    # restaurants, bars and cafés together, against 12,969 food and drink companies in Castilla y León
    assert (cov.cnae, cov.osm_count, cov.dirce_companies) == ("56", 12, 12969)
    assert env.confidence == "low"
    assert any(c.kind == "cross_check" and c.severity == "warning" for c in env.checks)
    assert any(s.dataset_id == "73020" for s in env.sources)


async def test_all_businesses_by_category(ine: IneData, store: SnapshotStore):
    env = await businesses(ine, store, "47186")
    d = env.data
    assert d.area.count == 19
    cats = {c.category: c.count for c in d.by_category}
    assert cats == {"bar": 6, "gym": 6, "restaurant": 4, "cafe": 2, "other": 1}
    assert d.coverage is None
    assert env.confidence == "medium"  # never high for volunteer-mapped data


async def test_districts_ranked_per_resident(ine: IneData, store: SnapshotStore):
    env = await businesses(ine, store, "Valladolid", "gym", sub_areas="district", rank_by="per_10k_residents")
    subs = env.data.sub_areas
    assert {s.code for s in subs} >= {"4718601", "4718604", "4718605", "4718612"}
    assert all(s.population for s in subs)  # residents from the income atlas
    rates = [s.per_10k_residents for s in subs]
    assert rates == sorted(rates, reverse=True)  # type: ignore[type-var]
    assert next(s for s in subs if s.code == "4718605").count == 0
    text = render(env)
    assert "Districts (" in text and "per 10,000 residents" in text


async def test_stale_snapshot_is_flagged(ine: IneData, tmp_path: Path):
    old = make_snapshot(tmp_path / "old.sqlite", osm_date=datetime.now(UTC) - timedelta(days=90))
    env = await businesses(ine, SnapshotStore(fixed_path=old), "Valladolid", "cafe")
    assert any(c.kind == "stale" and c.severity == "warning" for c in env.checks)


# Snapshot download


def _release(
    snapshot_path: Path, *, sha: str | None = None, schema: int = SCHEMA_VERSION
) -> httpx.MockTransport:
    raw = snapshot_path.read_bytes()
    manifest = {
        "schema_version": schema,
        "file": DB_NAME,
        "sha256": sha or hashlib.sha256(raw).hexdigest(),
        "osm_timestamp": "2026-10-15T20:21:00+00:00",
    }
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith(MANIFEST_NAME):
            return httpx.Response(200, text=json.dumps(manifest))
        if request.url.path.endswith(DB_NAME + ".gz"):
            return httpx.Response(200, content=gzip.compress(raw))
        return httpx.Response(404)

    t = httpx.MockTransport(handler)
    t.calls = calls  # type: ignore[attr-defined]
    return t


async def test_store_downloads_and_verifies(tmp_path: Path, snapshot_path: Path):
    transport = _release(snapshot_path)
    store = SnapshotStore(tmp_path / "cache", transport=transport, offline=False)
    snap = await store.get()
    assert snap.count(Area(()), Selection.categories(["gym"])) == 17
    assert (tmp_path / "cache" / DB_NAME).exists()
    # A second store in the same directory does not download again within a day.
    again = SnapshotStore(tmp_path / "cache", transport=transport, offline=False)
    await again.get()
    assert sum(1 for c in transport.calls if c.endswith(".gz")) == 1  # type: ignore[attr-defined]


async def test_store_rejects_a_corrupt_download(tmp_path: Path, snapshot_path: Path):
    store = SnapshotStore(tmp_path / "cache", transport=_release(snapshot_path, sha="0" * 64), offline=False)
    with pytest.raises(UpstreamError) as e:
        await store.get()
    assert "checksum" in e.value.cause
    assert not (tmp_path / "cache" / DB_NAME).exists()


async def test_store_refuses_a_newer_format(tmp_path: Path, snapshot_path: Path):
    store = SnapshotStore(tmp_path / "cache", transport=_release(snapshot_path, schema=99), offline=False)
    with pytest.raises(UpstreamError) as e:
        await store.get()
    assert "Upgrade" in e.value.fix


async def test_store_offline_without_a_snapshot(tmp_path: Path):
    with pytest.raises(UpstreamError) as e:
        await SnapshotStore(tmp_path / "cache", offline=True).get()
    assert "SPAIN_MCP_OSM_DB" in e.value.fix


async def test_store_fixed_path_missing(tmp_path: Path):
    with pytest.raises(UpstreamError):
        await SnapshotStore(fixed_path=tmp_path / "nope.sqlite").get()
