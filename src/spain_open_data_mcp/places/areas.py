"""Area logic shared by the tools: which areas to compare a place with, and how big they are.

The comparison ladder runs outward from a place, nearest first:

    tract / district ──▶ municipality ──▶ province ──▶ region ──▶ España ("ES")
                         (only for            (skipped when the region has
                          tracts/districts)    a single province, e.g. Madrid)

The place itself is never in its own ladder.
"""

from __future__ import annotations

from typing import Literal

from .codes import PROVINCES
from .resolver import Place, population_in

SubUnit = Literal["district", "tract"]
# A district code is the municipality code + 2 digits; a tract code adds 3 more.
SUB_UNIT_CODE_LENGTH: dict[SubUnit, int] = {"district": 7, "tract": 10}
SPAIN = "ES"


def provinces_in_ccaa(ccaa_code: str) -> list[str]:
    """Province codes in a region ('CA07' -> ['05', '09', ...])."""
    return [p for p, (_n, ccaa, _a) in PROVINCES.items() if f"CA{ccaa}" == ccaa_code]


def benchmark_ladder(p: Place) -> list[str]:
    """Codes to compare `p` with, nearest first, ending with 'ES'."""
    codes: list[str] = []
    if p.level in ("district", "tract") and p.municipality_code:
        codes.append(p.municipality_code)
    if p.level in ("municipality", "district", "tract") and p.province_code:
        codes.append(p.province_code)
    if p.level != "ccaa" and p.ccaa_code and len(provinces_in_ccaa(p.ccaa_code)) > 1:
        codes.append(p.ccaa_code)
    codes.append(SPAIN)
    return codes


def code_prefixes(code: str) -> tuple[str, ...]:
    """INE code prefixes covering an area: a region is its provinces, Spain is everything."""
    if code == SPAIN:
        return ()
    if code.startswith("CA"):
        return tuple(provinces_in_ccaa(code))
    return (code,)


def population_of(code: str) -> tuple[int, str]:
    """Official population (Padrón) and its reference date for a municipality, province,
    region or Spain. Districts and tracts are not in the Padrón list; use the income atlas."""
    return population_in("" if code == SPAIN else code)
