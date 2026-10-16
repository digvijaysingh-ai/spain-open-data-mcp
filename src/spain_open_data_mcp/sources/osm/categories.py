"""Business categories: how OpenStreetMap tags map to the categories the tools accept.

Shared by the snapshot builder (scripts/build_osm_snapshot.py), which stores a category
for each place, and by the `businesses` tool, which counts them. Changing a mapping
needs a snapshot rebuild; bump CATEGORIES_VERSION so the tool can tell.

`cnae` is the CNAE 2009 code that INE's company directory (DIRCE) counts for the same
activity. It is set only where most businesses run a single site, so a count of
companies is a fair yardstick for a count of premises. Chains (supermarkets, banks,
petrol stations) are left without one.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass

CATEGORIES_VERSION = 1

# OSM keys whose objects are kept in the snapshot. Within `amenity`, `tourism` and
# `leisure` only the values in CATEGORIES are kept; every `shop`, `office`, `craft` and
# `healthcare` value is kept so raw tags like 'shop=bakery' can be queried too.
OPEN_KEYS = ("shop", "office", "craft", "healthcare")
LISTED_KEYS = ("amenity", "tourism", "leisure")
# Not businesses, or containers of businesses (a mall is a building full of shops).
EXCLUDED_TAGS = frozenset(
    {
        "office=government",
        "office=association",
        "office=ngo",
        "office=religion",
        "office=political_party",
        "office=diplomatic",
        "office=educational_institution",
        "office=union",
        "office=foundation",
        "shop=mall",
        "shop=vacant",
        "shop=no",
    }
)


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    sector: str
    tags: tuple[str, ...]  # 'key=value'
    cnae: str | None = None
    cnae_label: str | None = None
    # False for categories that are mostly public services (state schools, hospitals),
    # which are left out of 'all businesses' totals.
    in_total: bool = True


_FOOD_SERVICE = ("56", "Servicios de comidas y bebidas")

CATEGORIES: tuple[Category, ...] = (
    # Food and drink. Many Spanish bar-restaurants register as bars (CNAE 563), so the
    # check compares the whole division 56 with all food and drink places.
    Category(
        "restaurant",
        "Restaurants",
        "food_drink",
        ("amenity=restaurant", "amenity=food_court"),
        *_FOOD_SERVICE,
    ),
    Category("fast_food", "Fast food and takeaways", "food_drink", ("amenity=fast_food",), *_FOOD_SERVICE),
    Category("cafe", "Cafés", "food_drink", ("amenity=cafe",), *_FOOD_SERVICE),
    Category(
        "bar",
        "Bars and pubs",
        "food_drink",
        ("amenity=bar", "amenity=pub", "amenity=biergarten"),
        *_FOOD_SERVICE,
    ),
    Category(
        "ice_cream", "Ice cream shops", "food_drink", ("amenity=ice_cream", "shop=ice_cream"), *_FOOD_SERVICE
    ),
    Category("nightclub", "Nightclubs", "food_drink", ("amenity=nightclub",), *_FOOD_SERVICE),
    # Food retail.
    Category("supermarket", "Supermarkets", "food_retail", ("shop=supermarket",)),
    Category("convenience", "Convenience stores", "food_retail", ("shop=convenience",)),
    Category(
        "bakery",
        "Bakeries and pastry shops",
        "food_retail",
        ("shop=bakery", "shop=pastry", "shop=confectionery"),
        "472",
        "Comercio al por menor de productos alimenticios, bebidas y tabaco en establecimientos especializados",
    ),
    Category(
        "butcher",
        "Butchers and fishmongers",
        "food_retail",
        ("shop=butcher", "shop=seafood", "shop=deli", "shop=cheese"),
        "472",
        "Comercio al por menor de productos alimenticios, bebidas y tabaco en establecimientos especializados",
    ),
    Category(
        "greengrocer",
        "Greengrocers",
        "food_retail",
        ("shop=greengrocer", "shop=farm"),
        "472",
        "Comercio al por menor de productos alimenticios, bebidas y tabaco en establecimientos especializados",
    ),
    Category(
        "drinks_tobacco",
        "Wine, drinks and tobacco shops",
        "food_retail",
        ("shop=alcohol", "shop=wine", "shop=beverages", "shop=tobacco", "shop=e-cigarette"),
        "472",
        "Comercio al por menor de productos alimenticios, bebidas y tabaco en establecimientos especializados",
    ),
    # Other retail.
    Category(
        "clothes",
        "Clothes and shoe shops",
        "retail",
        (
            "shop=clothes",
            "shop=shoes",
            "shop=boutique",
            "shop=fashion_accessories",
            "shop=bag",
            "shop=leather",
        ),
    ),
    Category("pharmacy", "Pharmacies", "retail", ("amenity=pharmacy", "healthcare=pharmacy")),
    Category("optician", "Opticians", "retail", ("shop=optician",)),
    Category(
        "beauty_shop",
        "Cosmetics and perfume shops",
        "retail",
        ("shop=cosmetics", "shop=perfumery", "shop=chemist"),
    ),
    Category("jewellery", "Jewellers", "retail", ("shop=jewelry", "shop=watches")),
    Category("florist", "Florists and garden centres", "retail", ("shop=florist", "shop=garden_centre")),
    Category("pet_shop", "Pet shops", "retail", ("shop=pet",)),
    Category(
        "electronics",
        "Electronics and phone shops",
        "retail",
        ("shop=electronics", "shop=mobile_phone", "shop=computer"),
    ),
    Category(
        "home",
        "Furniture, hardware and home shops",
        "retail",
        (
            "shop=furniture",
            "shop=hardware",
            "shop=doityourself",
            "shop=houseware",
            "shop=kitchen",
            "shop=bed",
            "shop=interior_decoration",
            "shop=lighting",
            "shop=paint",
            "shop=appliance",
            "shop=bathroom_furnishing",
            "shop=tiles",
            "shop=curtain",
        ),
    ),
    Category(
        "books_leisure",
        "Book, stationery, toy and sports shops",
        "retail",
        (
            "shop=books",
            "shop=stationery",
            "shop=newsagent",
            "shop=kiosk",
            "shop=toys",
            "shop=sports",
            "shop=bicycle",
            "shop=games",
        ),
    ),
    Category(
        "variety_store",
        "Variety stores and bazaars",
        "retail",
        ("shop=variety_store", "shop=general", "shop=department_store"),
    ),
    # Personal services.
    Category(
        "hairdresser",
        "Hairdressers and barbers",
        "personal_services",
        ("shop=hairdresser", "shop=barber"),
        "960",
        "Otros servicios personales",
    ),
    Category(
        "beauty_salon",
        "Beauty salons, nails and massage",
        "personal_services",
        ("shop=beauty", "shop=massage", "shop=tattoo", "shop=nails"),
        "960",
        "Otros servicios personales",
    ),
    Category(
        "laundry",
        "Laundries and dry cleaners",
        "personal_services",
        ("shop=laundry", "shop=dry_cleaning"),
        "960",
        "Otros servicios personales",
    ),
    # Health.
    Category(
        "dentist",
        "Dental clinics",
        "health",
        ("amenity=dentist", "healthcare=dentist"),
        "862",
        "Actividades médicas y odontológicas",
    ),
    Category(
        "doctor",
        "Medical practices and clinics",
        "health",
        ("amenity=doctors", "amenity=clinic", "healthcare=doctor", "healthcare=clinic"),
        "862",
        "Actividades médicas y odontológicas",
    ),
    Category(
        "physiotherapy",
        "Physiotherapy, psychology and other therapy",
        "health",
        (
            "healthcare=physiotherapist",
            "healthcare=psychotherapist",
            "healthcare=podiatrist",
            "healthcare=speech_therapist",
            "healthcare=alternative",
        ),
        "869",
        "Otras actividades sanitarias",
    ),
    Category("hospital", "Hospitals", "health", ("amenity=hospital", "healthcare=hospital"), in_total=False),
    Category("veterinary", "Vets", "health", ("amenity=veterinary",), "750", "Actividades veterinarias"),
    # Fitness, leisure and education.
    # CNAE 931 also covers every sports club, so it is no yardstick for gyms.
    Category("gym", "Gyms and fitness centres", "leisure", ("leisure=fitness_centre",)),
    Category("sports_centre", "Sports centres", "leisure", ("leisure=sports_centre",), in_total=False),
    Category(
        "dance_studio", "Dance and yoga studios", "leisure", ("leisure=dance", "amenity=dancing_school")
    ),
    Category("cinema_theatre", "Cinemas and theatres", "leisure", ("amenity=cinema", "amenity=theatre")),
    Category(
        "private_school",
        "Language, driving, music and tutoring schools",
        "education",
        ("amenity=language_school", "amenity=driving_school", "amenity=music_school", "amenity=prep_school"),
        "855",
        "Otra educación",
    ),
    Category(
        "childcare", "Nurseries and childcare", "education", ("amenity=kindergarten", "amenity=childcare")
    ),
    Category(
        "school",
        "Schools, colleges and universities",
        "education",
        ("amenity=school", "amenity=college", "amenity=university"),
        in_total=False,
    ),
    # Accommodation.
    Category("hotel", "Hotels", "accommodation", ("tourism=hotel", "tourism=motel")),
    Category(
        "guest_house",
        "Guest houses, hostels and holiday rentals",
        "accommodation",
        ("tourism=guest_house", "tourism=hostel", "tourism=apartment", "tourism=chalet"),
        "552",
        "Alojamientos turísticos y otros alojamientos de corta estancia",
    ),
    Category("camp_site", "Campsites", "accommodation", ("tourism=camp_site", "tourism=caravan_site")),
    # Professional services.
    Category(
        "estate_agent",
        "Estate agents",
        "professional",
        ("office=estate_agent",),
        "683",
        "Actividades inmobiliarias por cuenta de terceros",
    ),
    Category(
        "lawyer",
        "Law firms",
        "professional",
        ("office=lawyer", "office=notary"),
        "691",
        "Actividades jurídicas",
    ),
    Category(
        "accountant",
        "Accountants and tax advisers",
        "professional",
        ("office=accountant", "office=tax_advisor"),
        "692",
        "Actividades de contabilidad, teneduría de libros, auditoría y asesoría fiscal",
    ),
    Category("insurance", "Insurance offices", "professional", ("office=insurance",)),
    Category(
        "travel_agency", "Travel agencies", "professional", ("shop=travel_agency", "office=travel_agent")
    ),
    Category(
        "coworking", "Coworking spaces", "professional", ("amenity=coworking_space", "office=coworking")
    ),
    # Finance and motoring.
    Category("bank", "Bank branches", "finance", ("amenity=bank",)),
    Category("fuel", "Petrol stations", "motoring", ("amenity=fuel",)),
    Category(
        "car_repair",
        "Car repair and tyres",
        "motoring",
        ("shop=car_repair", "shop=tyres"),
        "452",
        "Mantenimiento y reparación de vehículos de motor",
    ),
    Category("car_dealer", "Car and motorbike dealers", "motoring", ("shop=car", "shop=motorcycle")),
    Category("car_wash", "Car washes", "motoring", ("amenity=car_wash",)),
)

BY_KEY: Mapping[str, Category] = {c.key: c for c in CATEGORIES}
BY_TAG: Mapping[str, Category] = {t: c for c in CATEGORIES for t in c.tags}
SECTORS: tuple[str, ...] = tuple(dict.fromkeys(c.sector for c in CATEGORIES))
NOT_IN_TOTAL: frozenset[str] = frozenset(c.key for c in CATEGORIES if not c.in_total)
LISTED_VALUES: Mapping[str, frozenset[str]] = {
    k: frozenset(t.split("=", 1)[1] for t in BY_TAG if t.startswith(k + "=")) for k in LISTED_KEYS
}


def classify(tags: Mapping[str, str]) -> tuple[str, str | None] | None:
    """Return (primary 'key=value' tag, category key or None), or None if out of scope.

    Keys are tried in a fixed order so a shop inside a hotel is a shop, and a pharmacy
    mapped with both amenity=pharmacy and healthcare=pharmacy counts once, as a pharmacy.
    """
    if tags.get("disused") == "yes" or "disused:shop" in tags:
        return None
    for key in ("amenity", "shop", "healthcare", "leisure", "tourism", "office", "craft"):
        value = tags.get(key)
        if not value:
            continue
        tag = f"{key}={value}"
        if key in LISTED_KEYS and value not in LISTED_VALUES[key]:
            continue
        if tag in EXCLUDED_TAGS:
            return None
        cat = BY_TAG.get(tag)
        return tag, cat.key if cat else None
    return None


# Plain words people use, in English and Spanish, singular. Plurals are handled by
# find_category.
SYNONYMS: Mapping[str, str] = {
    "restaurante": "restaurant",
    "takeaway": "fast_food",
    "comida rapida": "fast_food",
    "coffee shop": "cafe",
    "cafeteria": "cafe",
    "pub": "bar",
    "heladeria": "ice_cream",
    "discoteca": "nightclub",
    "club": "nightclub",
    "supermercado": "supermarket",
    "grocery": "supermarket",
    "grocery store": "supermarket",
    "tienda de conveniencia": "convenience",
    "panaderia": "bakery",
    "pasteleria": "bakery",
    "carniceria": "butcher",
    "pescaderia": "butcher",
    "fruteria": "greengrocer",
    "estanco": "drinks_tobacco",
    "vinoteca": "drinks_tobacco",
    "clothing": "clothes",
    "tienda de ropa": "clothes",
    "zapateria": "clothes",
    "farmacia": "pharmacy",
    "chemist": "pharmacy",
    "optica": "optician",
    "perfumeria": "beauty_shop",
    "joyeria": "jewellery",
    "jeweller": "jewellery",
    "floristeria": "florist",
    "tienda de mascota": "pet_shop",
    "mobile phone shop": "electronics",
    "ferreteria": "home",
    "muebleria": "home",
    "furniture": "home",
    "hardware": "home",
    "libreria": "books_leisure",
    "bookshop": "books_leisure",
    "bazar": "variety_store",
    "peluqueria": "hairdresser",
    "barberia": "hairdresser",
    "barber": "hairdresser",
    "hair salon": "hairdresser",
    "estetica": "beauty_salon",
    "centro de estetica": "beauty_salon",
    "nail salon": "beauty_salon",
    "lavanderia": "laundry",
    "tintoreria": "laundry",
    "dentista": "dentist",
    "clinica dental": "dentist",
    "medico": "doctor",
    "clinic": "doctor",
    "clinica": "doctor",
    "fisioterapia": "physiotherapy",
    "fisioterapeuta": "physiotherapy",
    "physio": "physiotherapy",
    "hospital": "hospital",
    "veterinario": "veterinary",
    "vet": "veterinary",
    "gimnasio": "gym",
    "fitness": "gym",
    "polideportivo": "sports_centre",
    "academia de baile": "dance_studio",
    "yoga": "dance_studio",
    "cine": "cinema_theatre",
    "academia": "private_school",
    "autoescuela": "private_school",
    "language school": "private_school",
    "guarderia": "childcare",
    "nursery": "childcare",
    "colegio": "school",
    "hostal": "guest_house",
    "hostel": "guest_house",
    "holiday rental": "guest_house",
    "camping": "camp_site",
    "inmobiliaria": "estate_agent",
    "real estate agency": "estate_agent",
    "abogado": "lawyer",
    "gestoria": "accountant",
    "asesoria": "accountant",
    "seguro": "insurance",
    "agencia de viaje": "travel_agency",
    "banco": "bank",
    "gasolinera": "fuel",
    "petrol station": "fuel",
    "gas station": "fuel",
    "taller": "car_repair",
    "garage": "car_repair",
    "concesionario": "car_dealer",
    "lavadero": "car_wash",
}


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return " ".join(text.replace("_", " ").replace("-", " ").split())


def _singular(word: str) -> list[str]:
    out = [word]
    if word.endswith("ies"):
        out.append(word[:-3] + "y")
    if word.endswith("es"):
        out.append(word[:-2])
    if word.endswith("s"):
        out.append(word[:-1])
    return out


_LOOKUP: dict[str, str] = {}
for _c in CATEGORIES:
    _LOOKUP[_norm(_c.key)] = _c.key
    _LOOKUP[_norm(_c.label)] = _c.key
for _word, _key in SYNONYMS.items():
    _LOOKUP[_norm(_word)] = _key


def find_category(text: str) -> Category | None:
    """A category for a key, label or plain word ('gyms', 'peluquerías'), or None."""
    words = _norm(text).split(" ")
    # Try each word's singular forms, so 'hair salons' -> 'hair salon'.
    for last in _singular(words[-1]):
        for first in [" ".join(_singular(w)[-1] for w in words[:-1]), " ".join(words[:-1])]:
            key = _LOOKUP.get(f"{first} {last}".strip())
            if key:
                return BY_KEY[key]
    return None
