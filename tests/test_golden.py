"""Golden set layer 1: the offline subset runs in CI against recorded INE data; the checker
itself is tested here too (evals/layer1.py runs every question against live data)."""

from __future__ import annotations

from mcp import Client

from evals import layer1
from spain_open_data_mcp.server import _Services, mcp
from spain_open_data_mcp.sources.ine.datasets import IneData


def test_golden_file_is_well_formed():
    questions = layer1.load()
    assert len(questions) >= 30
    assert len({q["id"] for q in questions}) == len(questions)
    for q in questions:
        assert q["question"] and q["rubric"] and q["checked_by"], q["id"]
        for call in q["calls"]:
            assert call["tool"] and isinstance(call["args"], dict), q["id"]
    assert sum("refusal" in q["tags"] for q in questions) >= 3


async def test_offline_golden_questions_pass(ine: IneData):
    _Services.set_ine(ine)
    async with Client(mcp) as client:
        outcomes = await layer1.run(layer1.load("offline"), client)
    assert outcomes and all(o.status == "pass" for o in outcomes), layer1.report(outcomes)


def test_checker():
    doc = {"data": {"a": [{"b": 5}], "n": 100.0}, "checks": [{"kind": "scope", "severity": "warning"}]}
    assert layer1.at_path(doc, "data.a.0.b") == 5
    assert layer1.check({"path": "data.n", "equals": 100}, doc)[0]
    assert not layer1.check({"path": "data.n", "equals": 101}, doc)[0]
    assert layer1.check({"path": "data.n", "between": [50, 150]}, doc)[0]
    assert layer1.check({"path": "data.a", "at_least_items": 1}, doc)[0]
    assert layer1.check({"check_kind": "scope", "severity": "warning"}, doc)[0]
    newer = {
        "sources": [
            {"dataset": "Atlas de Distribución de Renta de los Hogares (ADRH)", "data_period": "2024"}
        ]
    }
    assert layer1.newer_data(newer, 2023) and not layer1.newer_data(newer, 2024)


def test_model_runner_scoring():
    from evals import run_model

    assert run_model.number_present("Valladolid has 302,614 residents", "302,614")
    assert run_model.number_present("Valladolid tiene 302.614 habitantes", "302,614")
    assert run_model.number_present("about 302 614 people", "302,614")
    assert not run_model.number_present("about 300,000 people", "302,614")
    p, lo, hi = run_model.wilson(30, 33)
    assert 0.9 < p < 0.92 and lo < p < hi and hi <= 1
    rows = [
        {
            "tags": ["income"],
            "facts_total": 1,
            "facts_ok": True,
            "rubric": ["Names INE as the source."],
            "met": [True],
        },
        {
            "tags": ["refusal"],
            "facts_total": 0,
            "facts_ok": None,
            "rubric": ["Declines to identify people."],
            "met": [False],
        },
    ]
    s = run_model.summarise(rows)
    assert s["fact_accuracy"]["n"] == 1 and s["sourcing_items_met"]["rate"] == 1.0
    assert s["refusal_items_met"]["rate"] == 0.0
