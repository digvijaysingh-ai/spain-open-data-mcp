"""Golden set, layer 1: call the tools a correct answer needs and check the facts they return.

    uv run python evals/layer1.py            # every question, live data
    uv run python evals/layer1.py --tag income

No model is involved: this checks the server, not the assistant. Results per question are
pass, fail, or needs_update (INE published a newer year than the fact was checked for,
which means the golden fact should be re-checked, not that the server is wrong).
Exit code 1 if anything fails.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
GOLDEN = Path(__file__).with_name("golden.json")


@dataclass
class Outcome:
    id: str
    status: str  # pass | fail | needs_update
    notes: list[str] = field(default_factory=list)


def load(tag: str | None = None) -> list[dict[str, Any]]:
    items = json.loads(GOLDEN.read_text(encoding="utf-8"))["questions"]
    return [q for q in items if tag is None or tag in q["tags"]]


def at_path(doc: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(doc, list):
            doc = doc[int(part)] if part.isdigit() and int(part) < len(doc) else None
        elif isinstance(doc, dict):
            doc = doc.get(part)
        else:
            return None
    return doc


def newer_data(structured: dict[str, Any], year: int) -> bool:
    """True if an INE population or income source in the result is for a later year than the
    golden fact (INE published new data since the fact was checked)."""
    for s in structured.get("sources", []):
        dataset = s.get("dataset", "")
        m = re.match(r"(\d{4})", str(s.get("data_period", "")))
        if m and any(k in dataset for k in ("ADRH", "Padrón", "Censo")) and int(m.group(1)) > year:
            return True
    return False


def check(expect: dict[str, Any], structured: dict[str, Any]) -> tuple[bool, str]:
    if "check_kind" in expect:
        found = [
            c for c in structured.get("checks", [])
            if c["kind"] == expect["check_kind"] and c["severity"] == expect.get("severity", c["severity"])
        ]  # fmt: skip
        return bool(
            found
        ), f"check {expect['check_kind']}/{expect.get('severity')}: {'found' if found else 'missing'}"
    value = at_path(structured, expect["path"])
    if "equals" in expect:
        want = expect["equals"]
        ok = (
            abs(float(value) - float(want)) <= abs(float(want)) * 0.0005
            if isinstance(want, int | float) and isinstance(value, int | float)
            else value == want
        )
        return ok, f"{expect['path']} = {value} (want {want})"
    if "between" in expect:
        lo, hi = expect["between"]
        ok = isinstance(value, int | float) and lo <= value <= hi
        return ok, f"{expect['path']} = {value} (want {lo} to {hi})"
    if "at_least" in expect:
        ok = isinstance(value, int | float) and value >= expect["at_least"]
        return ok, f"{expect['path']} = {value} (want >= {expect['at_least']})"
    if "at_least_items" in expect:
        ok = isinstance(value, list) and len(value) >= expect["at_least_items"]
        return ok, f"{expect['path']} has {len(value) if isinstance(value, list) else 'no'} items"
    return False, f"unknown expectation {expect}"


async def run_question(client: Any, q: dict[str, Any]) -> Outcome:
    out = Outcome(q["id"], "pass")
    for call in q["calls"]:
        result = await client.call_tool(call["tool"], call["args"])
        text = " ".join(getattr(c, "text", "") for c in result.content)
        if "expect_error" in call:
            ok = result.is_error and call["expect_error"] in text
            out.notes.append(f"{call['tool']}: error {'as expected' if ok else 'missing: ' + text[:120]}")
            if not ok:
                out.status = "fail"
            continue
        if result.is_error:
            out.status, out.notes = "fail", [*out.notes, f"{call['tool']} failed: {text[:200]}"]
            continue
        structured = result.structured_content or {}
        for expect in call.get("expect", []):
            ok, note = check(expect, structured)
            if not ok and "data_year" in expect and newer_data(structured, expect["data_year"]):
                out.notes.append(note + " [newer INE data than the golden fact: re-check]")
                if out.status == "pass":
                    out.status = "needs_update"
                continue
            out.notes.append(("ok " if ok else "FAIL ") + note)
            if not ok:
                out.status = "fail"
    return out


async def run(questions: list[dict[str, Any]], client: Any) -> list[Outcome]:
    return [await run_question(client, q) for q in questions if q["calls"]]


def report(outcomes: list[Outcome]) -> str:
    lines = []
    for o in outcomes:
        lines.append(f"{o.status:12} {o.id}")
        lines += [f"             {n}" for n in o.notes]
    counts = {s: sum(1 for o in outcomes if o.status == s) for s in ("pass", "fail", "needs_update")}
    lines.append(
        f"\n{counts['pass']} pass, {counts['fail']} fail, {counts['needs_update']} need updating, of {len(outcomes)}"
    )
    return "\n".join(lines)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag")
    args = ap.parse_args()
    from mcp import Client

    from spain_open_data_mcp.server import mcp

    async with Client(mcp) as client:
        outcomes = await run(load(args.tag), client)
    print(report(outcomes))
    sys.exit(1 if any(o.status == "fail" for o in outcomes) else 0)


if __name__ == "__main__":
    asyncio.run(main())
