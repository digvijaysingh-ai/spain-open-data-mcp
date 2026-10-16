"""Golden set, layer 2: Claude answers each question using this server; answers are scored.

    ANTHROPIC_API_KEY=... uv run --group evals python evals/run_model.py [--model claude-sonnet-5-5] [--limit 5]

For each question, Claude gets the server's tools (called in-process over MCP) and answers
in its own words. The answer is then scored:
- facts: every 'answer_must_include' number appears in the answer (separators ignored);
- rubric: a separate grading call marks each rubric item met or not, from the question,
  the answer and the item alone.

Metrics, each with a 95% Wilson interval: fact accuracy, rubric items met, sourcing items
met (rubric items about sources), caveat items met (ranges, lower bounds, estimates, data
year), and refusal items met (privacy questions). Results go to evals/results/.

This makes paid API calls: about 33 questions x (2-6 tool turns + 1 grading call).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from evals.layer1 import load  # noqa: E402

RESULTS = Path(__file__).with_name("results")
MAX_TURNS = 8
TOOL_RESULT_CHARS = 20_000
GRADER = """You grade an assistant's answer against rubric items. For each item, decide strictly
whether the answer meets it. Reply with JSON only: {"met": [true or false for each item, in order]}.

Question: {question}

Answer:
<answer>
{answer}
</answer>

Rubric items:
{items}"""


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    """Proportion and its 95% Wilson score interval."""
    if n == 0:
        return 0.0, 0.0, 0.0
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return p, max(0.0, centre - half), min(1.0, centre + half)


_SEPARATORS = re.compile("(?<=\\d)[\\s.,\u202f\u00a0](?=\\d{3}\\b)")  # 302,614 / 302.614 / 302 614


def number_present(answer: str, wanted: str) -> bool:
    digits = re.sub(r"\D", "", wanted)
    return digits in _SEPARATORS.sub("", answer)


_SOURCING = re.compile(r"\b(source|sources|INE|atlas|OpenStreetMap)\b")
_CAVEATS = re.compile(r"\b(range|lower bound|estimate|assumption|data year|2023|evenly)\b", re.IGNORECASE)


async def answer(
    client: Any, mcp_client: Any, model: str, system: str, tools: list[dict], question: str
) -> tuple[str, list[str]]:
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
    used: list[str] = []
    for _ in range(MAX_TURNS):
        resp = await client.messages.create(
            model=model, max_tokens=2000, system=system, tools=tools, messages=messages
        )
        messages.append({"role": "assistant", "content": resp.content})
        calls = [b for b in resp.content if b.type == "tool_use"]
        if not calls:
            return "".join(b.text for b in resp.content if b.type == "text"), used
        results = []
        for call in calls:
            used.append(call.name)
            r = await mcp_client.call_tool(call.name, call.input)
            text = " ".join(getattr(c, "text", "") for c in r.content)[:TOOL_RESULT_CHARS]
            results.append(
                {"type": "tool_result", "tool_use_id": call.id, "content": text, "is_error": bool(r.is_error)}
            )
        messages.append({"role": "user", "content": results})
    return "(no final answer within the turn limit)", used


async def grade(client: Any, model: str, question: str, text: str, rubric: list[str]) -> list[bool]:
    prompt = GRADER.format(
        question=question, answer=text, items="\n".join(f"{i + 1}. {r}" for i, r in enumerate(rubric))
    )
    resp = await client.messages.create(
        model=model, max_tokens=300, messages=[{"role": "user", "content": prompt}]
    )
    raw = "".join(b.text for b in resp.content if b.type == "text")
    try:
        met = json.loads(raw[raw.index("{") : raw.rindex("}") + 1])["met"]
        return [bool(m) for m in met][: len(rubric)] + [False] * max(0, len(rubric) - len(met))
    except (ValueError, KeyError):
        return [False] * len(rubric)


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def metric(pairs: list[bool]) -> dict[str, float]:
        p, lo, hi = wilson(sum(pairs), len(pairs))
        return {"rate": round(p, 3), "ci_low": round(lo, 3), "ci_high": round(hi, 3), "n": len(pairs)}

    facts = [r["facts_ok"] for r in rows if r["facts_total"]]
    items = [(r["tags"], it, m) for r in rows for it, m in zip(r["rubric"], r["met"], strict=True)]
    return {
        "fact_accuracy": metric(facts),
        "rubric_items_met": metric([m for _t, _i, m in items]),
        "sourcing_items_met": metric([m for _t, i, m in items if _SOURCING.search(i)]),
        "caveat_items_met": metric([m for _t, i, m in items if _CAVEATS.search(i)]),
        "refusal_items_met": metric([m for t, _i, m in items if "refusal" in t]),
    }


def markdown(summary: dict[str, Any], model: str, when: str, n: int) -> str:
    lines = [
        f"# Golden set results: {model}, {when[:10]}",
        "",
        f"{n} questions. 95% Wilson intervals.",
        "",
        "| Metric | Rate | 95% interval | n |",
        "|---|---|---|---|",
    ]
    for name, m in summary.items():
        lines.append(
            f"| {name.replace('_', ' ')} | {m['rate']:.0%} | {m['ci_low']:.0%} to {m['ci_high']:.0%} | {m['n']} |"
        )
    return "\n".join(lines) + "\n"


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--ids", nargs="*")
    args = ap.parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("Set ANTHROPIC_API_KEY. This run makes paid API calls.")
    import anthropic  # pyright: ignore[reportMissingImports]  # the evals group, not installed in CI
    from mcp import Client

    from spain_open_data_mcp.server import INSTRUCTIONS, mcp

    questions = load()
    if args.ids:
        questions = [q for q in questions if q["id"] in set(args.ids)]
    questions = questions[: args.limit] if args.limit else questions
    client = anthropic.AsyncAnthropic()
    rows = []
    async with Client(mcp) as mcp_client:
        listed = (await mcp_client.list_tools()).tools
        tools = [
            {"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
            for t in listed
        ]
        for q in questions:
            text, used = await answer(client, mcp_client, args.model, INSTRUCTIONS, tools, q["question"])
            must = q.get("answer_must_include", [])
            present = [number_present(text, w) for w in must]
            met = await grade(client, args.model, q["question"], text, q["rubric"])
            rows.append(
                {
                    "id": q["id"],
                    "tags": q["tags"],
                    "question": q["question"],
                    "answer": text,
                    "tools": used,
                    "facts_total": len(must),
                    "facts_ok": all(present) if must else None,
                    "rubric": q["rubric"],
                    "met": met,
                }
            )
            print(f"{q['id']:32} facts {'ok' if all(present) else 'MISS'}  rubric {sum(met)}/{len(met)}")
    when = datetime.now(UTC).isoformat(timespec="seconds")
    summary = summarise(rows)
    RESULTS.mkdir(exist_ok=True)
    stem = f"{when[:10]}-{args.model}"
    (RESULTS / f"{stem}.json").write_text(
        json.dumps(
            {"model": args.model, "run_at": when, "summary": summary, "rows": rows},
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    md = markdown(summary, args.model, when, len(rows))
    (RESULTS / "latest.md").write_text(md, encoding="utf-8")
    print("\n" + md)


if __name__ == "__main__":
    asyncio.run(main())
