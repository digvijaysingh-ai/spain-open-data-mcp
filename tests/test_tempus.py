import pytest

from spain_open_data_mcp.core.errors import UpstreamError
from spain_open_data_mcp.sources.ine.tempus import parse_series_list
from tests.fixtures.ine_mock import load


def test_parses_codes_from_metadata():
    series = parse_series_list(load("t29005_nult1_tv19-495.json"))
    total = next(s for s in series if s.dim_name("Sexo") == "Total")
    assert total.dim_code("Municipios") == "47186"
    point = total.latest()
    assert point is not None
    assert point.value == 302614
    assert point.date == "2025-01-01"
    assert point.status == "ok"


def test_suppressed_and_missing_are_distinct():
    raw = [
        {
            "COD": "X1",
            "Nombre": "A",
            "MetaData": [{"T3_Variable": "Secciones", "Codigo": "4700101001", "Nombre": "A"}],
            "Data": [
                {
                    "Anyo": 2023,
                    "Fecha": "2023-01-01T00:00:00.000+01:00",
                    "Valor": None,
                    "Notas": [
                        {"texto": "Dato bajo secreto estadístico, por tamaño de la Unidad Territorial."}
                    ],
                }
            ],
        },
        {
            "COD": "X2",
            "Nombre": "B",
            "MetaData": [],
            "Data": [{"Anyo": 2023, "Fecha": "2023-01-01", "Valor": None}],
        },
        {"COD": "X3", "Nombre": "C", "MetaData": [], "Data": []},
    ]
    s1, s2, s3 = parse_series_list(raw)
    assert s1.points[0].status == "suppressed"
    assert s2.points[0].status == "missing"
    assert s3.points == []


def test_volume_restriction_is_an_error():
    with pytest.raises(UpstreamError, match="too large"):
        parse_series_list({"status": "No puede mostrarse por restricciones de volumen"})
