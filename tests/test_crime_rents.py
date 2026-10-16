"""crime (bundled Interior ministry balance) and rents (INE rent index, IPVA)."""

from datetime import UTC, datetime

import pytest

from spain_open_data_mcp.core.errors import DataError
from spain_open_data_mcp.sources.ine.datasets import IPVA_TABLES
from spain_open_data_mcp.sources.ine.tempus import TableResult, _parse_series
from spain_open_data_mcp.tools import crime as crime_mod
from spain_open_data_mcp.tools import rents as rents_mod


def _values(total: float, prev: float, theft: float = 10) -> dict:
    return {
        "total": [prev, total],
        "conventional": [prev * 0.8, total * 0.8],
        "theft": [theft, theft],
        "homicide": [1, 0],
    }


PERIOD = {
    "label": "enero-diciembre 2025",
    "previous_label": "enero-diciembre 2024",
    "quarter": "20254",
    "url": "https://x.test/y",
}
FIXTURE = {
    "built_at": "2026-10-16T10:00:00+00:00",
    "publisher": "Ministerio del Interior, Portal Estadístico de Criminalidad",
    "dataset": "Balance de Criminalidad",
    "licence": "Ley 37/2007",
    "periods": {
        "year": {**PERIOD, "areas": {
            "47186": {"name": "Valladolid", "values": _values(11781, 11103)},
            "47": {"name": "Provincia de VALLADOLID", "values": _values(15000, 14800, 500)},
            "CA07": {"name": "CASTILLA Y LEÓN", "values": _values(80000, 79000, 5000)},
            "CA13": {"name": "MADRID", "values": _values(392168, 399088, 5000)},
            "ES": {"name": "NACIONAL", "values": _values(2474156, 2454206, 9000)},
        }},
        "latest": {**PERIOD, "label": "enero-junio 2026", "previous_label": "enero-junio 2025", "quarter": "20262",
                   "areas": {"47186": {"name": "Valladolid", "values": _values(6000, 5800)}}},
    },
}  # fmt: skip


@pytest.fixture(autouse=True)
def crime_fixture(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(crime_mod, "crime_data", lambda: FIXTURE)


def test_crime_municipality_rates_and_benchmarks():
    env = crime_mod.crime("Valladolid")
    a = env.data.area
    assert a.code == "47186" and a.level == "municipality"
    total = next(r for r in a.rates if r.type == "total")
    assert total.count == 11781 and total.change_pct == pytest.approx((11781 - 11103) / 11103 * 100)
    assert total.per_1000_residents == pytest.approx(11781 / a.population * 1000)
    assert [b.code for b in env.data.benchmarks] == ["47", "CA07", "ES"]
    assert env.data.benchmarks[-1].level == "country" and env.data.benchmarks[-1].name == "España"
    assert env.data.latest and env.data.latest.period == "enero-junio 2026"
    assert any(c.kind == "thin_sample" and "Theft" in c.message for c in env.checks)
    assert "per 1,000" in crime_mod.render(env)


def test_crime_small_town_and_district_fall_back():
    env = crime_mod.crime("Tordesillas")  # under 20,000 residents
    assert env.data.area.code == "47"
    assert any("over 20,000" in c.message for c in env.checks)
    env = crime_mod.crime("4718601")
    assert env.data.area.code == "47186"
    assert any("not by district" in c.message for c in env.checks)


def test_crime_single_province_region_is_the_region():
    env = crime_mod.crime("Getafe")  # not in the fixture; Madrid province is published as the region
    assert env.data.area.code == "CA13"


def _series(var: str, code: str, name: str, kind: str, values: dict[int, float]) -> dict:
    return {
        "Nombre": f"{name}. {kind}. Total",
        "MetaData": [
            {"T3_Variable": var, "Codigo": code, "Nombre": name},
            {"T3_Variable": "Tipo de dato", "Codigo": "", "Nombre": kind},
            {"T3_Variable": "Tamaño de la vivienda", "Codigo": "", "Nombre": "Total"},
        ],
        "Data": [{"Anyo": y, "Valor": v, "T3_Periodo": "A"} for y, v in values.items()],
    }


INDEX = {2015: 100.0, 2019: 108.0, 2023: 115.0, 2024: 118.0}
TABLES = {
    "municipality": [
        _series("Municipios", "47186", "Valladolid", "Índice", INDEX),
        _series("Municipios", "47186", "Valladolid", "Variación anual", {2024: 2.6}),
        _series("Municipios", "47186", "Valladolid", "Índice", {2024: 999}) | {"MetaData": [
            {"T3_Variable": "Municipios", "Codigo": "47186", "Nombre": "Valladolid"},
            {"T3_Variable": "Tipo de dato", "Codigo": "", "Nombre": "Índice"},
            {"T3_Variable": "Tamaño de la vivienda", "Codigo": "", "Nombre": "Hasta 50 m2"}]},
    ],
    "district": [],
    "province": [_series("Provincias", "47", "Valladolid", "Índice", INDEX)],
    "ccaa": [
        _series("Total Nacional", "00", "Total Nacional", "Índice", {2015: 100.0, 2019: 110.0, 2024: 122.9}),
        _series("Comunidades y Ciudades Autónomas", "07", "Castilla y León", "Índice", INDEX),
    ],
}  # fmt: skip


class FakeIne:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def rent_index(self, level: str) -> TableResult:
        self.calls.append(level)
        return TableResult(
            table_id=IPVA_TABLES[level],
            series=[_parse_series(s) for s in TABLES[level]],
            retrieved_at=datetime(2026, 10, 16, tzinfo=UTC),
            stale_cache=False,
            url="https://www.ine.es/jaxiT3/Tabla.htm?t=59060",
        )


async def test_rents_municipality():
    env = await rents_mod.rents(FakeIne(), "Valladolid")  # type: ignore[arg-type]
    a = env.data.area
    assert a.code == "47186" and a.index == 118.0  # the size breakdown is ignored
    assert a.annual_change_pct == 2.6
    assert a.change_5y_pct == pytest.approx((118 / 108 - 1) * 100)
    assert a.change_since_2015_pct == pytest.approx(18.0)
    assert [b.code for b in env.data.benchmarks] == ["47", "CA07", "ES"]
    assert env.data.benchmarks[-1].name == "España"
    assert env.confidence == "medium"


async def test_rents_falls_back_and_excludes_foral_regions():
    env = await rents_mod.rents(FakeIne(), "4718601")  # type: ignore[arg-type]
    assert env.data.area.code == "47186"
    assert any("districts of provincial capitals" in c.message for c in env.checks)
    with pytest.raises(DataError, match="Basque Country and Navarra"):
        await rents_mod.rents(FakeIne(), "Bilbao")  # type: ignore[arg-type]
