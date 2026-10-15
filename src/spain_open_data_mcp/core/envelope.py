"""The response envelope every tool returns: data plus sources, checks and confidence.

The model reads the text rendering; MCP clients that support structured output also
get the same content as typed JSON (`structuredContent`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, Field

Confidence = Literal["high", "medium", "low"]
CheckKind = Literal[
    "suppressed",  # upstream withheld a value (statistical secrecy)
    "missing",  # upstream has no value for this unit or year
    "stale",  # data is older than the question implies
    "thin_sample",  # small population or count, figures are volatile
    "estimate",  # a modelled figure, not an observed one
    "cross_check",  # two sources compared
    "privacy",  # personal data withheld
    "scope",  # what the figure does and does not cover
]
Severity = Literal["info", "warning", "critical"]

T = TypeVar("T")


class Source(BaseModel):
    """Where a figure comes from. Every number in `data` must trace to one of these."""

    publisher: str = Field(description="Organisation that publishes the data, e.g. 'INE'.")
    dataset: str = Field(description="Human-readable dataset name.")
    dataset_id: str = Field(description="Upstream identifier, e.g. INE table id '29005'.")
    url: str = Field(description="URL a person can open to see the data.")
    licence: str
    attribution: str = Field(description="Attribution text the licence requires.")
    data_period: str = Field(description="Reference date or year of the data, e.g. '2025-01-01' or '2023'.")
    retrieved_at: datetime = Field(description="When this server fetched the data from upstream (UTC).")


class Check(BaseModel):
    kind: CheckKind
    severity: Severity
    message: str


class Envelope(BaseModel, Generic[T]):
    data: T
    sources: list[Source]
    checks: list[Check] = Field(default_factory=list)
    confidence: Confidence = Field(
        description="Lowest confidence across inputs: high, medium or low. Never state a "
        "conclusion more strongly than this."
    )


def confidence_from(checks: list[Check], floor: Confidence = "high") -> Confidence:
    """A result is never more confident than its weakest input."""
    order: list[Confidence] = ["low", "medium", "high"]
    level = order.index(floor)
    for c in checks:
        if c.severity == "critical":
            level = min(level, 0)
        elif c.severity == "warning" or c.kind == "estimate":
            level = min(level, 1)
    return order[level]


def dedupe_sources(sources: list[Source]) -> list[Source]:
    seen: set[tuple[str, str]] = set()
    out: list[Source] = []
    for s in sources:
        key = (s.dataset_id, s.data_period)
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


def render_footer(sources: list[Source], checks: list[Check], confidence: Confidence) -> str:
    """Plain-text 'Sources and caveats' block appended to every tool's text output."""
    lines = ["", "Sources:"]
    for s in sources:
        lines.append(
            f"- {s.publisher}, {s.dataset} (id {s.dataset_id}), data {s.data_period}, "
            f"retrieved {s.retrieved_at:%Y-%m-%d}. {s.url} Licence: {s.licence}. {s.attribution}"
        )
    if checks:
        lines.append("Checks and caveats:")
        for c in checks:
            lines.append(f"- [{c.severity}] {c.kind}: {c.message}")
    lines.append(f"Confidence: {confidence}")
    return "\n".join(lines)
