import pytest

from spain_open_data_mcp.core.errors import AmbiguousPlace, PlaceNotFound
from spain_open_data_mcp.places.resolver import (
    name_variants,
    normalize,
    place_from_code,
    resolve,
    resolve_one,
)


@pytest.mark.parametrize(
    ("query", "code", "level"),
    [
        ("Valladolid", "47186", "municipality"),
        ("valencia", "46250", "municipality"),
        ("A Coruña", "15030", "municipality"),
        ("La Coruña", "15030", "municipality"),
        ("Seville", "41091", "municipality"),
        ("San Sebastián", "20069", "municipality"),
        ("L'Hospitalet", "08101", "municipality"),
        ("Alicante", "03014", "municipality"),
        ("Catalonia", "CA09", "ccaa"),
        ("Mérida, Badajoz", "06083", "municipality"),
        ("08", "08", "province"),
        ("CA13", "CA13", "ccaa"),
        ("4718603", "4718603", "district"),
        ("Valladolid distrito 3", "4718603", "district"),
        ("4718603012", "4718603012", "tract"),
    ],
)
def test_resolves(query: str, code: str, level: str):
    best = resolve(query)[0]
    assert (best.code, best.level) == (code, level)


def test_city_and_province_alternatives():
    places = resolve("Valencia")
    assert [p.level for p in places[:2]] == ["municipality", "province"]


def test_inverted_article_names():
    assert "a coruna" in name_variants("Coruña, A")
    assert "l hospitalet de llobregat" in name_variants("Hospitalet de Llobregat, L'")
    assert normalize("  Málaga ") == "malaga"


def test_hierarchy_from_code():
    p = place_from_code("4718603012")
    assert (p.province_code, p.ccaa_code, p.municipality_code) == ("47", "CA07", "47186")


def test_same_name_municipalities_need_a_province():
    with pytest.raises(AmbiguousPlace, match="Santiago"):
        resolve_one("Santiago")


def test_dominant_municipality_wins():
    best, others = resolve_one("Jerez")
    assert best.code == "11020"  # Jerez de la Frontera
    assert others


def test_close_typo_is_accepted_but_flagged():
    best, _ = resolve_one("Vallladolid")
    assert best.code == "47186"
    assert best.match == "fuzzy"


def test_weak_guess_is_refused():
    with pytest.raises(PlaceNotFound, match="Alanís"):
        resolve_one("Atlantis")


def test_bad_code():
    with pytest.raises(PlaceNotFound):
        place_from_code("123")
