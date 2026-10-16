"""find_datasets and read_dataset against a mocked datos.gob.es."""

from pathlib import Path

import httpx
import pytest

from spain_open_data_mcp.core import http as http_mod
from spain_open_data_mcp.core.errors import DataError
from spain_open_data_mcp.core.http import HostPolicy, HttpClient
from spain_open_data_mcp.sources import datosgob
from spain_open_data_mcp.sources.datosgob import DatosGob, parse_table, screen_columns
from spain_open_data_mcp.tools.datasets import find_datasets, read_dataset

PUB = "http://datos.gob.es/recurso/sector-publico/org/Organismo/L01280796"


def item(
    slug: str, title: str, *, spatial: str = "", formats=("csv",), url="https://files.test/data.csv"
) -> dict:
    return {
        "_about": f"https://datos.gob.es/catalogo/{slug}",
        "title": [{"_value": title, "_lang": "es"}, {"_value": title + " (en)", "_lang": "en"}],
        "description": [{"_value": f"Descripción de {title}", "_lang": "es"}],
        "publisher": PUB,
        "license": "http://creativecommons.org/licenses/by/4.0/",
        "modified": "2026-09-01T00:00:00Z",
        "accrualPeriodicity": "http://purl.org/cld/freq/monthly",
        "spatial": spatial,
        "distribution": [
            {
                "accessURL": url,
                "format": f"http://publications.europa.eu/resource/authority/file-type/{f.upper()}",
            }
            for f in formats
        ],
    }


CATALOGUE = {
    "title/aparcamiento": [
        item("l01280796-aparcamientos-publicos", "Aparcamientos públicos",
             spatial="http://datos.gob.es/recurso/sector-publico/territorio/Provincia/Madrid"),
        item("l01080193-aparcamientos-barcelona", "Aparcamientos de Barcelona",
             spatial="http://datos.gob.es/recurso/sector-publico/territorio/Provincia/Barcelona"),
    ],
    "keyword/aparcamiento": [
        item("l01280796-aparcamientos-publicos", "Aparcamientos públicos"),  # duplicate
        item("e00001-plazas-aparcamiento-ue", "Plazas de aparcamiento", formats=("pdf",)),
    ],
    "l01280796-aparcamientos-publicos": [
        item("l01280796-aparcamientos-publicos", "Aparcamientos públicos", formats=("pdf", "csv"))
    ],
    "e00001-plazas-aparcamiento-ue": [item("e00001-plazas-aparcamiento-ue", "Plazas", formats=("pdf",))],
}  # fmt: skip


def handler(req: httpx.Request) -> httpx.Response:
    if "sparql" in req.url.path:
        assert PUB in req.url.params["query"]
        rows = [{"p": {"value": PUB}, "name": {"value": "Ayuntamiento de Madrid"}}]
        return httpx.Response(200, json={"results": {"bindings": rows}})
    key = req.url.path.split("/catalog/dataset/", 1)[1]
    return httpx.Response(200, json={"result": {"items": CATALOGUE.get(key, [])}})


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DatosGob:
    monkeypatch.setitem(http_mod.POLICIES, "datos.gob.es", HostPolicy(min_interval_s=0))
    return DatosGob(HttpClient(tmp_path / "c.sqlite", transport=httpx.MockTransport(handler), offline=False))


CSV = (
    "Nombre;Apellidos;Distrito;Plazas;Contacto;Código\n"
    "Ana;Pérez;Centro;120;ana@example.com;A1\n"
    "Luis;Gil;Retiro;80;luis@example.com;A2\n"
).encode("cp1252")


def files(body: bytes = CSV) -> httpx.MockTransport:
    return httpx.MockTransport(lambda req: httpx.Response(200, content=body))


async def test_find_merges_ranks_and_names_publishers(client: DatosGob):
    env = await find_datasets(client, "aparcamiento")
    ids = [d.id for d in env.data.datasets]
    assert ids == [
        "l01280796-aparcamientos-publicos",
        "l01080193-aparcamientos-barcelona",
        "e00001-plazas-aparcamiento-ue",
    ]
    first = env.data.datasets[0]
    assert first.publisher == "Ayuntamiento de Madrid"
    assert first.licence == "CC BY 4.0"
    assert first.formats == ["CSV"] and first.update_frequency == "monthly"
    assert first.title == "Aparcamientos públicos"  # Spanish preferred


async def test_find_prefers_the_place(client: DatosGob):
    env = await find_datasets(client, "aparcamiento", place="Barcelona")
    assert [d.id for d in env.data.datasets] == ["l01080193-aparcamientos-barcelona"]
    env = await find_datasets(client, "aparcamiento", place="Soria")
    assert env.data.total_found == 3
    assert any("Soria" in c.message for c in env.checks)


async def test_find_rejects_short_query(client: DatosGob):
    with pytest.raises(DataError):
        await find_datasets(client, "ab")


async def test_read_skips_pdf_and_withholds_personal_columns(client: DatosGob):
    env = await read_dataset(client, "l01280796-aparcamientos-publicos", transport=files())
    d = env.data
    assert d.file_format == "CSV"
    assert d.columns == ["Distrito", "Plazas", "Código"]
    assert d.withheld_columns == ["Nombre", "Apellidos", "Contacto"]
    assert d.sample[0] == {"Distrito": "Centro", "Plazas": "120", "Código": "A1"}
    assert "ana" not in str(env.model_dump()).lower()
    assert env.sources[0].publisher == "Ayuntamiento de Madrid"
    assert any(c.kind == "privacy" for c in env.checks)


async def test_read_errors(client: DatosGob):
    with pytest.raises(DataError, match="no CSV or JSON"):
        await read_dataset(client, "e00001-plazas-aparcamiento-ue", transport=files())
    with pytest.raises(DataError, match="is PDF"):
        await read_dataset(client, "l01280796-aparcamientos-publicos", file_index=0, transport=files())
    with pytest.raises(DataError, match=r"not a datos\.gob\.es dataset id"):
        await read_dataset(client, "../../etc/passwd", transport=files())
    with pytest.raises(DataError, match="No dataset"):
        await read_dataset(client, "l09999999-missing", transport=files())


async def test_download_truncates_and_refuses_private_hosts(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(datosgob, "MAX_BYTES", 10)
    raw, truncated = await datosgob.download("https://files.test/x.csv", transport=files(b"a,b\n" * 10))
    assert truncated and len(raw) == 10
    for url in (
        "http://127.0.0.1/x.csv",
        "http://169.254.169.254/latest",
        "file:///etc/passwd",
        "ftp://x.test/a",
    ):
        with pytest.raises(DataError, match="not a public"):
            await datosgob.download(url)


def test_parse_json_and_value_screening():
    header, rows = parse_table(
        b'{"data": [{"zona": "Norte", "tel": "612 345 678"}, {"zona": "Sur", "tel": "699111222"}]}',
        "JSON",
        False,
    )
    assert header == ["zona", "tel"] and rows[1] == ["Sur", "699111222"]
    assert screen_columns(header, rows) == [1]
    header, rows = parse_table(b"id,doc\n1,12345678Z\n2,X1234567L\n", "CSV", False)
    assert screen_columns(header, rows) == [1]
    # A lone 'Nombre' is a car park's name, not a person's.
    header, rows = parse_table(b"Nombre;Nombre_vial;Plazas\nParking Centro;Calle Mayor;120\n", "CSV", False)
    assert screen_columns(header, rows) == []
    # A cut last line is dropped when the file was truncated.
    header, rows = parse_table(b"a,b\n1,2\n3,", "CSV", True)
    assert rows == [["1", "2"]]
