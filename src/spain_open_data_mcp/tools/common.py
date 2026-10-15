"""Helpers shared by tools: formatting and turning an Envelope into a tool result."""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from typing import Any

from mcp_types import CallToolResult, TextContent

from ..core.envelope import Check, Envelope, render_footer
from ..places.resolver import Place, resolve_one


def fmt_int(v: float | None) -> str:
    return "n/a" if v is None else f"{v:,.0f}"


def fmt_eur(v: float | None) -> str:
    return "n/a" if v is None else f"€{v:,.0f}"


def fmt_pct(v: float | None, digits: int = 1) -> str:
    return "n/a" if v is None else f"{v:.{digits}f}%"


def pct_diff(a: float, b: float) -> float:
    return (a - b) / b * 100 if b else 0.0


def resolve_place_arg(place: str) -> tuple[Place, list[Check]]:
    """Resolve a tool's `place` argument; note any alternative readings as a check."""
    best, others = resolve_one(place)
    checks: list[Check] = []
    if best.match == "fuzzy":
        checks.append(
            Check(
                kind="scope",
                severity="warning",
                message=f"No place is named exactly '{place}'; using the closest name, {best.name} "
                f"({best.level}, code {best.code}). Confirm this is the place meant.",
            )
        )
    if others and not place.strip().isdigit():
        alts = ", ".join(f"{p.name} ({p.level}, code {p.code})" for p in others[:3])
        checks.append(
            Check(
                kind="scope",
                severity="info",
                message=f"'{place}' was read as {best.name} ({best.level}, code {best.code}). "
                f"Other matches: {alts}. Pass a code to choose another.",
            )
        )
    return best, checks


def summary_stats(values: Iterable[float]) -> dict[str, float] | None:
    vals = sorted(values)
    if not vals:
        return None
    q = statistics.quantiles(vals, n=4) if len(vals) >= 4 else [vals[0], statistics.median(vals), vals[-1]]
    return {"min": vals[0], "p25": q[0], "median": statistics.median(vals), "p75": q[-1], "max": vals[-1]}


def to_result(envelope: Envelope[Any], text: str) -> CallToolResult:
    """Text for the model (summary + sources + checks) and the same data as structured content."""
    body = text.rstrip() + "\n" + render_footer(envelope.sources, envelope.checks, envelope.confidence)
    return CallToolResult(
        content=[TextContent(type="text", text=body)],
        structured_content=envelope.model_dump(mode="json"),
    )
