import pytest

from spain_open_data_mcp.places.areas import (
    SPAIN,
    benchmark_ladder,
    code_prefixes,
    population_of,
    provinces_in_ccaa,
)
from spain_open_data_mcp.places.resolver import place_from_code


@pytest.mark.parametrize(
    ("code", "ladder"),
    [
        ("4718603012", ["47186", "47", "CA07", "ES"]),  # tract
        ("4718603", ["47186", "47", "CA07", "ES"]),  # district
        ("47186", ["47", "CA07", "ES"]),  # municipality
        ("47", ["CA07", "ES"]),  # province
        ("CA07", ["ES"]),  # region
        ("28079", ["28", "ES"]),  # Madrid: one-province region is skipped
        ("28", ["ES"]),
    ],
)
def test_benchmark_ladder(code: str, ladder: list[str]):
    assert benchmark_ladder(place_from_code(code)) == ladder


def test_ladder_never_contains_the_place_itself():
    for code in ("4718603012", "47186", "47", "CA07", "CA13"):
        assert code not in benchmark_ladder(place_from_code(code))


def test_code_prefixes():
    assert code_prefixes(SPAIN) == ()
    assert code_prefixes("47186") == ("47186",)
    assert set(code_prefixes("CA07")) == set(provinces_in_ccaa("CA07"))
    assert len(provinces_in_ccaa("CA07")) == 9  # Castilla y León
    assert provinces_in_ccaa("CA13") == ["28"]


def test_population_of_adds_up():
    spain, date = population_of(SPAIN)
    regions = sum(population_of(f"CA{n:02d}")[0] for n in range(1, 20))
    assert spain == regions and spain > 49_000_000
    assert date.startswith("2025")
    assert population_of("47186")[0] == 302_614
