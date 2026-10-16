"""Address to coordinates: CartoCiudad (Spain's official geocoder, IGN), checked, with a
Nominatim fallback.

CartoCiudad's free-text search returns near misses confidently ("Gran Via 28 Madrid" comes
back as Gran Vía in Mula, Murcia), so a candidate is used only if it is in the municipality
the address names and its street name matches. Otherwise OpenStreetMap's Nominatim is tried
(at most one request per second, per its usage policy), its result checked the same way;
otherwise the near misses are returned as an error, never a guess.

Privacy: floor, door and staircase details are stripped before anything is sent; addresses
are never logged; returned coordinates are rounded to about 100 m.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass

import httpx

from ...config import settings
from ...core.errors import AmbiguousPlace, PlaceNotFound
from ...places.resolver import normalize, resolve_one

CARTOCIUDAD = "https://www.cartociudad.es/geocoder/api/geocoder/candidatesJsonp"
CARTOCIUDAD_FIND = "https://www.cartociudad.es/geocoder/api/geocoder/find"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
ROUND_DIGITS = 3  # about 100 m
_COORDS = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
# "3º B", "2ºIzq", "piso 4", "puerta 2", "esc. A", "bajo", "1ª planta": personal detail, not location
_UNIT = re.compile(
    r"(?i)\b(\d+\s*[ºª°]\s*[a-z]?(?:\s*(?:izq|izda|dcha|der|dch)\.?)?|piso\s*\d+\w*|puerta\s*\w+|pta\.?\s*\w+|"
    r"esc(?:alera)?\.?\s*\w+|planta(?:\s*\w+)?|bajo\s*\w?|ático|atico|entresuelo|local\s*\d*)\b"
)
_STREET_WORDS = {
    "calle", "c", "cl", "avenida", "av", "avda", "plaza", "pl", "pza", "paseo", "po", "pso", "camino", "carretera",
    "ctra", "ronda", "travesia", "glorieta", "de", "del", "la", "las", "el", "los", "y", "carrer", "rua",
    "rúa", "kalea", "plaça", "placa", "passeig", "avinguda",
}  # fmt: skip
_last_nominatim = 0.0
_nominatim_lock = asyncio.Lock()


@dataclass(frozen=True)
class Located:
    lon: float
    lat: float
    label: str  # what was found, without floor or door
    municipality_code: str
    source: str  # "coordinates", "CartoCiudad (IGN)", "OpenStreetMap Nominatim"


def strip_unit_details(address: str) -> str:
    """Remove floor, door and staircase details, which identify a home, not a location."""
    text = _UNIT.sub("", address)
    text = re.sub(r"\s+,", ",", text)
    text = re.sub(r",(\s*,)+", ",", text)  # empty parts left by removed details
    return re.sub(r"\s{2,}", " ", text).strip(" ,")


def _street_key(text: str) -> str:
    words = [w for w in normalize(text).split() if w not in _STREET_WORDS and not w.isdigit()]
    return " ".join(words)


def _street_matches(found: str, wanted: str) -> bool:
    return _street_key(found) == wanted


def _contains_all(found: str, wanted: str) -> bool:
    """'Calle Larios' is within 'Calle Marqués de Larios': every word given, and the same last
    word (street names end in their distinctive word; 'Gran Via' is not 'Gran Via de Hortaleza')."""
    got, want = _street_key(found).split(), wanted.split()
    return bool(want) and set(want) <= set(got) and got[-1] == want[-1]


def _point(found: dict) -> tuple[float, float] | None:
    """(lon, lat) of a CartoCiudad result: its point, or the centre of a street's line."""
    lat, lon = found.get("lat"), found.get("lng")
    if lat and lon:
        return float(lon), float(lat)
    geom = found.get("geom")
    if geom:
        from shapely import wkt

        c = wkt.loads(geom).centroid
        if c.x and c.y:
            return float(c.x), float(c.y)
    return None


def _parts(address: str) -> tuple[str, str | None, str]:
    """(street part, house number, municipality part). The municipality is the last comma part."""
    if "," not in address:
        raise PlaceNotFound(
            problem="The address has no municipality.",
            cause="Addresses are read as 'street number, municipality'.",
            fix="Write e.g. 'Calle Santiago 10, Valladolid', or pass coordinates as 'lat, lon'.",
        )
    street, muni = (s.strip() for s in address.rsplit(",", 1))
    number = next(iter(re.findall(r"\b(\d{1,4})\b", street)), None)
    return street, number, muni


def _round(v: float) -> float:
    return round(v, ROUND_DIGITS)


async def locate(address: str, *, transport: httpx.AsyncBaseTransport | None = None) -> Located:
    """Coordinates for 'lat, lon' or 'street number, municipality'."""
    if m := _COORDS.match(address):
        lat, lon = float(m.group(1)), float(m.group(2))
        if not (27 <= lat <= 44.5 and -18.5 <= lon <= 4.5):
            raise PlaceNotFound(
                problem=f"{lat}, {lon} is not in Spain.",
                cause="Coordinates are read as 'latitude, longitude'.",
                fix="Check the order: Madrid is about '40.42, -3.70'.",
            )
        return Located(_round(lon), _round(lat), f"{_round(lat)}, {_round(lon)}", "", "coordinates")
    clean = strip_unit_details(address)
    street, number, muni_text = _parts(clean)
    try:
        muni, _ = resolve_one(muni_text)
    except AmbiguousPlace as e:
        raise AmbiguousPlace(
            problem=e.problem, cause=e.cause, fix="Add the province: 'street number, town, province'."
        ) from e
    if muni.level != "municipality":
        raise PlaceNotFound(
            problem=f"'{muni_text}' is a {muni.level}, not a municipality.",
            cause="An address needs the town or city it is in.",
            fix="End the address with the municipality, e.g. ', Sevilla'.",
        )
    wanted = _street_key(street)
    misses: list[str] = []
    async with httpx.AsyncClient(
        headers={"User-Agent": settings.user_agent}, timeout=settings.http_timeout_s, transport=transport
    ) as client:
        found = await _cartociudad(client, f"{street}, {muni.name}", muni.code, wanted, misses)
        if found is None:
            found = await _nominatim(client, street, muni.name, muni.code, wanted, misses)
    if found is None:
        near = "; ".join(dict.fromkeys(misses)) or "none"
        raise PlaceNotFound(
            problem=f"No street matching '{street}' was found in {muni.name}.",
            cause=f"Closest results from the geocoders: {near}.",
            fix="Check the street name, or pass coordinates as 'lat, lon'.",
        )
    lon, lat, label, source = found
    if number and number not in label:
        label = f"{label} (number {number} not matched; street location used)"
    return Located(_round(lon), _round(lat), label, muni.code, source)


async def _cartociudad(
    client: httpx.AsyncClient, query: str, muni_code: str, wanted: str, misses: list[str]
) -> tuple[float, float, str, str] | None:
    try:
        r = await client.get(CARTOCIUDAD, params={"q": query, "limit": 20})
        r.raise_for_status()
        text = r.text.strip()
        candidates = json.loads(
            text[text.index("(") + 1 : text.rindex(")")] if text.startswith("callback") else text
        )
    except (httpx.HTTPError, ValueError) as e:
        misses.append(f"CartoCiudad unavailable ({type(e).__name__})")
        return None
    # Streets and street numbers only: points of interest ("toponimo") name a building, not a street.
    in_town = [
        c for c in candidates if c.get("muniCode") == muni_code and c.get("type") in ("portal", "callejero")
    ]
    misses += [
        f"{(c.get('address') or '').split(',')[0]} in {c.get('muni')}" for c in candidates if c not in in_town
    ]
    exact = [c for c in in_town if _street_matches((c.get("address") or "").split(",")[0], wanted)]
    partial = [c for c in in_town if _contains_all((c.get("address") or "").split(",")[0], wanted)]
    streets = {_street_key((c.get("address") or "").split(",")[0]) for c in partial}
    chosen = exact or (partial if len(streets) == 1 else [])
    misses += [
        f"{(c.get('address') or '').split(',')[0]} in {c.get('muni')}" for c in in_town if c not in chosen
    ]
    for c in chosen:
        point = _point(c)
        if point is None:  # a street without a point: ask for its geometry
            try:
                f = await client.get(CARTOCIUDAD_FIND, params={"id": c["id"], "type": c["type"]})
                f.raise_for_status()
                point = _point(f.json())
            except (httpx.HTTPError, ValueError, KeyError):
                continue
        if point is not None:
            return point[0], point[1], str(c.get("address")), "CartoCiudad (IGN)"
    return None


async def _nominatim(
    client: httpx.AsyncClient, street: str, muni_name: str, muni_code: str, wanted: str, misses: list[str]
) -> tuple[float, float, str, str] | None:
    global _last_nominatim
    async with _nominatim_lock:  # Nominatim's policy: at most one request per second
        wait = 1.0 - (time.monotonic() - _last_nominatim)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            r = await client.get(
                NOMINATIM,
                params={"street": street, "city": muni_name.split("/")[0], "countrycodes": "es", "format": "jsonv2",
                        "addressdetails": 1, "limit": 5},
            )  # fmt: skip
            r.raise_for_status()
            results = r.json()
        except (httpx.HTTPError, ValueError) as e:
            misses.append(f"Nominatim unavailable ({type(e).__name__})")
            return None
        finally:
            _last_nominatim = time.monotonic()
    for res in results:
        addr = res.get("address", {})
        road = addr.get("road") or addr.get("pedestrian") or ""
        town = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality") or ""
        # Nominatim matches "city" loosely (a village in the province of the same name), so the
        # town must be present and must be the municipality asked for.
        if (
            town
            and normalize(town) == normalize(muni_name.split("/")[0])
            and (_street_matches(road, wanted) or _contains_all(road, wanted))
        ):
            label = f"{road} {addr.get('house_number', '')}".strip() + f", {town}"
            return float(res["lon"]), float(res["lat"]), label, "OpenStreetMap Nominatim"
        misses.append(f"{road} in {town}")
    return None
