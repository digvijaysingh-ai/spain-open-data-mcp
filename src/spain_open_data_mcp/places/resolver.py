"""Resolve place names or INE codes to INE geography.

Levels and code formats:
- ccaa: 'CA' + 2 digits (e.g. 'CA09' Cataluña)
- province: 2 digits ('47')
- municipality: 5 digits ('47186')
- district: 7 digits ('4718603'), municipality + district number
- tract (sección censal): 10 digits ('4718603012')

Names are matched accent- and case-insensitively, handling INE's 'Coruña, A' style and
bilingual 'Alacant/Alicante' names. 'Name, Province' narrows the search.
"""

from __future__ import annotations

import csv
import difflib
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..core.errors import AmbiguousPlace, PlaceNotFound
from .codes import CCAA, MUNICIPALITY_ALIASES, PROVINCES

Level = Literal["ccaa", "province", "municipality", "district", "tract"]
FUZZY_ACCEPT = 0.9  # below this similarity a spelling guess is not used without confirmation
LEVEL_RANK: dict[Level, int] = {"municipality": 0, "province": 1, "ccaa": 2, "district": 3, "tract": 4}


class Place(BaseModel):
    code: str = Field(description="INE code. Pass this to other tools.")
    level: Level
    name: str
    province_code: str | None = None
    province: str | None = None
    ccaa_code: str | None = None
    ccaa: str | None = None
    municipality_code: str | None = None
    municipality: str | None = None
    population: int | None = Field(default=None, description="Official population (Padrón), when known.")
    population_date: str | None = None
    match: Literal["code", "exact", "prefix", "fuzzy"] = Field(
        default="exact", description="How the query matched. 'fuzzy' means a spelling guess: confirm it."
    )
    match_score: float = Field(default=1.0, description="Name similarity, 0-1.")


@dataclass(frozen=True)
class _Muni:
    code: str
    name: str
    population: int | None
    population_date: str
    census_value_id: int | None


def normalize(text: str) -> str:
    """Lowercase, strip accents and punctuation, collapse spaces."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def name_variants(name: str) -> set[str]:
    """'Coruña, A' -> {'coruna a', 'a coruna'}; 'Alacant/Alicante' -> both halves too."""
    out: set[str] = set()
    for part in [name, *name.split("/")]:
        part = part.strip()
        out.add(normalize(part))
        m = re.match(r"^(.*), (el|la|los|las|l'|el|els|les|a|o|as|os|lo|sa|ses|es)$", part, re.IGNORECASE)
        if m:
            article = m.group(2)
            joiner = "" if article.endswith("'") else " "
            out.add(normalize(f"{article}{joiner}{m.group(1)}"))
    out.discard("")
    return out


@lru_cache(maxsize=1)
def _municipalities() -> dict[str, _Muni]:
    path = resources.files("spain_open_data_mcp.data").joinpath("municipalities.csv")
    out: dict[str, _Muni] = {}
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[row["code"]] = _Muni(
                code=row["code"],
                name=row["name"],
                population=int(row["population"]) if row["population"] else None,
                population_date=row["population_date"],
                census_value_id=int(row["census_value_id"]) if row["census_value_id"] else None,
            )
    return out


@lru_cache(maxsize=1)
def _index() -> dict[str, list[tuple[Level, str]]]:
    """normalized name -> [(level, code)]"""
    idx: dict[str, list[tuple[Level, str]]] = {}

    def add(names: set[str], level: Level, code: str) -> None:
        for n in names:
            entries = idx.setdefault(n, [])
            if (level, code) not in entries:
                entries.append((level, code))

    for code, (name, aliases) in CCAA.items():
        add(name_variants(name) | {normalize(a) for a in aliases}, "ccaa", f"CA{code}")
    for code, (name, _ccaa, aliases) in PROVINCES.items():
        add(name_variants(name) | {normalize(a) for a in aliases}, "province", code)
    for code, m in _municipalities().items():
        add(
            name_variants(m.name) | {normalize(a) for a in MUNICIPALITY_ALIASES.get(code, ())},
            "municipality",
            code,
        )
    return idx


def census_value_id(municipality_code: str) -> int | None:
    m = _municipalities().get(municipality_code)
    return m.census_value_id if m else None


def municipalities_in(prefix: str) -> list[str]:
    """Municipality codes in a province (2-digit prefix) or CCAA ('CAxx')."""
    if prefix.startswith("CA"):
        provs = {p for p, (_n, ccaa, _a) in PROVINCES.items() if ccaa == prefix[2:]}
        return [c for c in _municipalities() if c[:2] in provs]
    return [c for c in _municipalities() if c.startswith(prefix)]


def place_from_code(code: str) -> Place:
    code = code.strip().upper()
    if re.fullmatch(r"CA\d{2}", code) and code[2:] in CCAA:
        return Place(code=code, level="ccaa", name=CCAA[code[2:]][0], ccaa_code=code)
    if not code.isdigit() or len(code) not in (2, 5, 7, 10):
        raise PlaceNotFound(
            problem=f"'{code}' is not a valid INE place code.",
            cause="INE codes are 'CA'+2 digits (region), 2 digits (province), 5 (municipality), "
            "7 (district) or 10 (census tract).",
            fix="Call resolve_place with the place name to get its code.",
        )
    prov = code[:2]
    if prov not in PROVINCES:
        raise PlaceNotFound(
            problem=f"No province with code {prov}.",
            cause="Province codes run from 01 to 52.",
            fix="Call resolve_place with the place name.",
        )
    pname, ccaa_code, _ = PROVINCES[prov]
    base: dict[str, Any] = {
        "province_code": prov,
        "province": pname,
        "ccaa_code": f"CA{ccaa_code}",
        "ccaa": CCAA[ccaa_code][0],
    }
    if len(code) == 2:
        return Place(code=code, level="province", name=pname, **base)
    muni = _municipalities().get(code[:5])
    if muni is None:
        raise PlaceNotFound(
            problem=f"No municipality with code {code[:5]}.",
            cause="The code is not in INE's current municipality list.",
            fix="Call resolve_place with the place name; municipalities sometimes merge or get new codes.",
        )
    mbase: dict[str, Any] = {**base, "municipality_code": muni.code, "municipality": muni.name}
    if len(code) == 5:
        return Place(
            code=code,
            level="municipality",
            name=muni.name,
            population=muni.population,
            population_date=muni.population_date,
            **mbase,
        )
    if len(code) == 7:
        return Place(code=code, level="district", name=f"{muni.name} distrito {code[5:]}", **mbase)
    return Place(code=code, level="tract", name=f"{muni.name} sección {code[5:7]}-{code[7:]}", **mbase)


def _population(p: Place) -> int:
    return p.population or 0


def resolve(query: str, *, limit: int = 5) -> list[Place]:
    """Return candidate places for a name or code, best first. Raises PlaceNotFound."""
    q = query.strip()
    if not q:
        raise PlaceNotFound(problem="Empty place name.", cause="No text was given.", fix="Pass a place name.")
    if re.fullmatch(r"(?i)ca\d{2}|\d{2}|\d{5}|\d{7}|\d{10}", q):
        return [place_from_code(q).model_copy(update={"match": "code"})]

    # 'Valladolid distrito 3' / 'distrito 03 de Valladolid'
    m = re.match(r"(?i)^(.+?)\s+distrito\s+(\d{1,2})$", q) or None
    if m:
        base = resolve(m.group(1), limit=1)[0]
        if base.level == "municipality":
            return [place_from_code(f"{base.code}{int(m.group(2)):02d}")]

    scope: str | None = None
    if "," in q:  # 'Mérida, Badajoz' or 'Coruña, A'
        head, tail = (s.strip() for s in q.rsplit(",", 1))
        tail_hits = [c for lvl, c in _index().get(normalize(tail), []) if lvl in ("province", "ccaa")]
        if tail_hits and normalize(q) not in _index():
            q, scope = head, tail_hits[0]

    idx = _index()
    nq = normalize(q)
    hits = list(idx.get(nq, []))
    match: Literal["exact", "prefix", "fuzzy"] = "exact"
    scores: dict[str, float] = {}
    if not hits and len(nq) >= 4:  # 'l hospitalet' -> "L'Hospitalet de Llobregat"
        hits = [h for name, hs in idx.items() if name.startswith(nq + " ") for h in hs]
        match = "prefix"
    if not hits:  # spelling mistakes, ranked by similarity
        match = "fuzzy"
        close = difflib.get_close_matches(nq, idx.keys(), n=limit, cutoff=0.8)
        for c in close:
            for h in sorted(idx[c], key=lambda h: LEVEL_RANK[h[0]]):
                hits.append(h)
                scores.setdefault(h[1], round(difflib.SequenceMatcher(None, nq, c).ratio(), 3))
    codes = list(dict.fromkeys(code for _lvl, code in hits))
    places = [
        place_from_code(code).model_copy(update={"match": match, "match_score": scores.get(code, 1.0)})
        for code in codes
    ]
    if scope:
        places = [
            p
            for p in places
            if (p.province_code == scope if not scope.startswith("CA") else p.ccaa_code == scope)
        ]
    if not places:
        raise PlaceNotFound(
            problem=f"No Spanish place matches '{query}'.",
            cause="The name is not an INE municipality, province or autonomous community "
            "(neighbourhood names are not supported yet).",
            fix="Check the spelling, use the official municipality name, or add the province: 'Mérida, Badajoz'.",
        )
    if match != "fuzzy":
        places.sort(key=lambda p: (LEVEL_RANK[p.level], -_population(p)))
    return places[:limit]


def resolve_one(query: str) -> tuple[Place, list[Place]]:
    """Best place plus the alternatives. Raises AmbiguousPlace when two municipalities share a name."""
    places = resolve(query, limit=10)
    best = places[0]
    if best.match == "fuzzy" and best.match_score < FUZZY_ACCEPT:
        options = "; ".join(f"{p.name} ({p.level}, code {p.code})" for p in places[:4])
        raise PlaceNotFound(
            problem=f"No place is named '{query}'.",
            cause=f"The closest names are only similar: {options}.",
            fix="If you meant one of these, pass its code. Otherwise check the spelling.",
        )
    same_level = sorted((p for p in places if p.level == best.level), key=lambda p: -_population(p))
    # A clearly dominant municipality wins (Jerez -> Jerez de la Frontera); callers
    # report the alternatives so the choice is visible.
    dominant = len(same_level) > 1 and _population(same_level[0]) >= 10 * max(_population(same_level[1]), 1)
    if best.level == "municipality" and len(same_level) > 1 and not dominant:
        options = "; ".join(f"{p.name} ({p.province}) code {p.code}" for p in same_level[:6])
        raise AmbiguousPlace(
            problem=f"'{query}' matches {len(same_level)} municipalities.",
            cause=f"Candidates: {options}.",
            fix="Pass the INE code, or the name with its province, e.g. 'Name, Province'.",
        )
    if dominant:
        best = same_level[0]
    return best, [p for p in places if p.code != best.code]
