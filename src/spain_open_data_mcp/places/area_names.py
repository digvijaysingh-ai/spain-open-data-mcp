"""Neighbourhood names for INE districts and census tracts, from OpenStreetMap.

INE numbers districts and tracts but does not name them. area_names.json.gz (built by
scripts/build_area_names.py) lists the OpenStreetMap neighbourhoods, quarters, suburbs,
villages and city districts inside each one, so 'Valladolid distrito 11' can be shown with
the names people use, and those names can be searched.

The names describe what lies inside an INE unit; they are not official INE names, and a
neighbourhood can straddle two districts.
"""

from __future__ import annotations

import gzip
import json
import re
from functools import lru_cache
from importlib import resources
from typing import Any

FILE_NAME = "area_names.json.gz"
# Broadest kind first: official sub-municipal boundaries and suburbs before neighbourhoods.
KIND_RANK = {
    "admin9": 0,
    "suburb": 1,
    "borough": 1,
    "town": 1,
    "admin10": 2,
    "quarter": 2,
    "village": 2,
    "neighbourhood": 3,
    "hamlet": 4,
}


@lru_cache(maxsize=1)
def _data() -> dict[str, Any]:
    path = resources.files("spain_open_data_mcp.data").joinpath(FILE_NAME)
    try:
        return json.loads(gzip.decompress(path.read_bytes()))
    except FileNotFoundError:
        return {"meta": {}, "labels": {}, "near": {}, "places": []}


def label(code: str) -> str | None:
    """'Parquesol, La Rubia' for a district or tract, 'near Parquesol' for a tract with no
    name inside, or None."""
    d = _data()
    names = d["labels"].get(code)
    if names:
        return ", ".join(names)
    near = d["near"].get(code)
    return f"near {near}" if near else None


def with_names(code: str, name: str) -> str:
    """'Valladolid distrito 10' -> 'Valladolid distrito 10 (Parquesol, Girón, La Victoria)'
    for district and tract codes; other names unchanged."""
    if len(code) not in (7, 10) or "(" in name:
        return name
    names = label(code)
    return f"{name} ({names})" if names else name


def short_label(code: str) -> str | None:
    """The first name only, for chart axes: 'Parquesol' or 'near Fuente el Sol'."""
    d = _data()
    names = d["labels"].get(code)
    if names:
        return names[0]
    near = d["near"].get(code)
    return f"near {near}" if near else None


def meta() -> dict[str, str]:
    return _data()["meta"]


@lru_cache(maxsize=1)
def index() -> dict[str, list[tuple[str, str]]]:
    """normalized name -> [(name as written, tract code)]"""
    from .resolver import normalize

    out: dict[str, list[tuple[str, str]]] = {}
    for name, tract in _data()["places"]:
        key = normalize(name)
        for k in {key, strip_article(key)}:
            out.setdefault(k, []).append((name, tract))
    return out


def strip_article(normalized: str) -> str:
    """'el poblenou' -> 'poblenou', so either form finds it."""
    return re.sub(r"^(el|la|los|las|l|els|les|o|a|os|as|es|sa|ses) (?=\S)", "", normalized)
