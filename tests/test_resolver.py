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


@pytest.mark.parametrize(
    ("official", "natural"),
    [
        ("Palmas de Gran Canaria, Las", "Las Palmas de Gran Canaria"),
        ("Madrid, Comunidad de", "Comunidad de Madrid"),
        ("Hospitalet de Llobregat, L'", "L'Hospitalet de Llobregat"),
        ("Coruña, A", "A Coruña"),
        ("Pobla de Mafumet, la", "La Pobla de Mafumet"),
        ("Alacant/Alicante", "Alacant/Alicante"),
        ("Valladolid", "Valladolid"),
    ],
)
def test_natural_names(official: str, natural: str):
    from spain_open_data_mcp.places.resolver import natural_name

    assert natural_name(official) == natural


def test_places_carry_both_names():
    p = place_from_code("35016")
    assert p.name == "Las Palmas de Gran Canaria" and p.official_name == "Palmas de Gran Canaria, Las"
    assert p.province == "Las Palmas" and place_from_code("CA13").name == "Comunidad de Madrid"
    assert resolve("Las Palmas de Gran Canaria")[0].code == "35016"


def test_districts_and_tracts_carry_neighbourhood_names():
    d = place_from_code("4718610")
    assert d.name.startswith("Valladolid distrito 10 (") and "Parquesol" in d.name
    assert d.area_names and "Parquesol" in d.area_names
    assert "Parquesol" in place_from_code("4718610030").name
    assert place_from_code("2807901").name.startswith("Madrid distrito 01 (Centro")


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("Parquesol, Valladolid", "4718610"),
        ("Parquesol", "4718610"),
        ("Gràcia, Barcelona", "0801906"),
        ("Poblenou, Barcelona", "0801910"),  # stored as 'El Poblenou'
        ("Triana, Sevilla", "4109106"),
        ("El Palmar, Murcia", "3003005"),  # also a municipality name elsewhere
    ],
)
def test_neighbourhood_names_resolve_to_their_district(query: str, code: str):
    best, _ = resolve_one(query)
    assert best.code == code and best.level == "district" and best.match == "neighbourhood"
    assert best.matched_name


def test_neighbourhood_prefix_needs_a_city():
    best, _ = resolve_one("Delicias, Valladolid")  # 'Delicias - Arco de Ladrillo'
    assert best.code[:5] == "47186" and best.match == "neighbourhood"
    with pytest.raises(PlaceNotFound):
        resolve_one("Parquesol, Madrid")


def test_chart_axis_labels_stay_short():
    from spain_open_data_mcp.tools.chart import axis_label

    assert axis_label("4718610", place_from_code("4718610").name) == "distrito 10 Parquesol"
    assert len(axis_label("0801901", place_from_code("0801901").name)) <= 34
    assert axis_label("47186", "Valladolid") == "Valladolid"
