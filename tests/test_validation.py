"""Forward-test statistics (engine/validation.py)."""

from __future__ import annotations

import pytest

from spain_open_data_mcp.categories import ATLAS_CATEGORIES
from spain_open_data_mcp.engine.validation import (
    CATEGORY_DIVISION,
    quarter_after,
    quintile_lift,
    weighted_scores,
)


def test_every_atlas_category_has_a_division():
    assert set(ATLAS_CATEGORIES) <= set(CATEGORY_DIVISION)


def test_lift_when_the_ranking_predicts_the_outcome():
    ranking = {f"p{i}": float(i) for i in range(50)}
    outcome = {f"p{i}": 1.0 + i / 10 for i in range(50)}  # higher score, more openings
    lift = quintile_lift(ranking, outcome)
    assert lift and lift.lift > 2 and lift.ci_low > 1 and lift.n_areas == 50


def test_no_lift_from_a_useless_ranking():
    ranking = {f"p{i}": float(i) for i in range(50)}
    outcome = {f"p{i}": 1.0 for i in range(50)}
    lift = quintile_lift(ranking, outcome)
    assert lift and lift.lift == pytest.approx(1.0) and lift.ci_low <= 1 <= lift.ci_high


def test_too_few_areas():
    assert quintile_lift({"a": 1.0}, {"a": 1.0}) is None


def test_quarters_after_registration():
    assert not quarter_after((2026, 4), "2026-10-16")  # the registration's own quarter
    assert quarter_after((2027, 1), "2026-10-16")
    assert not quarter_after((2026, 2), "2026-10-16")


def test_population_weighted_province_scores():
    rows = [
        {
            "code": "47186",
            "level": "municipality",
            "too_small": "False",
            "score_base": "2.0",
            "status": "ok",
            "population": "300",
        },
        {
            "code": "47010",
            "level": "municipality",
            "too_small": "False",
            "score_base": "1.0",
            "status": "ok",
            "population": "100",
        },
        {
            "code": "47001",
            "level": "municipality",
            "too_small": "True",
            "score_base": "",
            "status": "ok",
            "population": "50",
        },
        {
            "code": "4718601",
            "level": "district",
            "too_small": "False",
            "score_base": "9.0",
            "status": "ok",
            "population": "10",
        },
    ]
    assert weighted_scores(rows, 2) == {"47": pytest.approx(1.75)}
