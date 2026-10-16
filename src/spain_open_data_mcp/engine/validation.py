"""Scoring a registered prediction against what happened (pure functions, no I/O).

A registered atlas ranks areas by opportunity. The test: in the quarters after registration,
did the top fifth of areas see more business openings (relative to their existing businesses)
than the bottom fifth? Lift = mean opening rate of the top quintile / of the bottom quintile.
The same is computed for a population-only ranking, the baseline any useful score must beat.
Intervals come from a bootstrap over areas (resampling within each quintile).
"""

from __future__ import annotations

import random
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# INE business demography (CODEM) division for each atlas category.
CATEGORY_DIVISION: Mapping[str, str] = {
    "restaurant": "56", "cafe": "56", "bar": "56", "fast_food": "56",
    "bakery": "47", "butcher": "47", "greengrocer": "47", "supermarket": "47", "pharmacy": "47",
    "optician": "47", "clothes": "47", "home": "47",
    "dentist": "86", "physiotherapy": "86",
    "hairdresser": "96", "beauty_salon": "96",
    "veterinary": "75", "gym": "93", "childcare": "85", "private_school": "85", "guest_house": "55",
}  # fmt: skip


@dataclass(frozen=True)
class Lift:
    lift: float
    ci_low: float
    ci_high: float
    top_mean: float
    bottom_mean: float
    n_areas: int


def quintile_lift(
    ranking: Mapping[str, float], outcome: Mapping[str, float], *, draws: int = 2000, seed: int = 7
) -> Lift | None:
    """Top-fifth vs bottom-fifth mean outcome for areas ranked by `ranking` (higher = better)."""
    areas = [a for a in ranking if a in outcome]
    if len(areas) < 10:
        return None
    ordered = sorted(areas, key=lambda a: ranking[a], reverse=True)
    k = max(2, len(ordered) // 5)
    top = [outcome[a] for a in ordered[:k]]
    bottom = [outcome[a] for a in ordered[-k:]]
    b_mean = statistics.fmean(bottom)
    if b_mean <= 0:
        return None
    rng = random.Random(seed)
    lifts = []
    for _ in range(draws):
        t = statistics.fmean(rng.choices(top, k=len(top)))
        b = statistics.fmean(rng.choices(bottom, k=len(bottom)))
        if b > 0:
            lifts.append(t / b)
    lifts.sort()
    lo = lifts[int(0.025 * len(lifts))]
    hi = lifts[int(0.975 * len(lifts)) - 1]
    return Lift(statistics.fmean(top) / b_mean, lo, hi, statistics.fmean(top), b_mean, len(areas))


def weighted_scores(rows: Sequence[Mapping[str, str]], group_len: int) -> dict[str, float]:
    """Population-weighted mean score per area prefix (2 = province) from atlas CSV rows."""
    sums: dict[str, list[float]] = {}
    for r in rows:
        if (
            r["level"] != "municipality"
            or r["too_small"] == "True"
            or not r["score_base"]
            or r["status"] != "ok"
        ):
            continue
        pop = float(r["population"])
        acc = sums.setdefault(r["code"][:group_len], [0.0, 0.0])
        acc[0] += float(r["score_base"]) * pop
        acc[1] += pop
    return {k: v[0] / v[1] for k, v in sums.items() if v[1]}


def quarter_after(quarter: tuple[int, int], registered: str) -> bool:
    """True if the quarter (year, n) starts after the registration date (YYYY-MM-DD)."""
    y, m = int(registered[:4]), int(registered[5:7])
    reg_q = (y, (m - 1) // 3 + 1)
    return quarter > reg_q
