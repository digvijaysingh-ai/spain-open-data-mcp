"""chart: PNG charts of tool results, returned as MCP image content.

Each kind reuses a tool's function, so a chart shows exactly the numbers the tool returns,
and the same sources and checks travel with it. Rendering uses matplotlib's object API with
the Agg backend (never pyplot, which is not thread-safe) in a worker thread. If drawing
fails, the text answer and data still come back, with a check saying why the chart is
missing.

    kind="opportunity"        compare_areas: score per area with its low-high range
    kind="income"             income by district or tract
    kind="age_pyramid"        demographics: population by sex and five-year age band
    kind="population_trend"   demographics: population over five years
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
from typing import Any, Literal

from mcp_types import CallToolResult, ImageContent, TextContent

from ..core.envelope import Check, Envelope, confidence_from, render_footer
from ..core.errors import DataError
from ..sources.ine.datasets import IneData
from ..sources.osm import snapshot as osm
from . import demographics as demo_mod
from . import income as income_mod
from . import market as market_mod

log = logging.getLogger(__name__)

ChartKind = Literal["opportunity", "income", "age_pyramid", "population_trend"]
ChartSubAreas = Literal["district", "tract"]
INK, ACCENT, MUTED = "#1f2933", "#1565c0", "#9aa5b1"
MAX_BARS = 25


def _figure(height: float = 4.5) -> Any:
    from matplotlib.figure import Figure  # imported here: most calls never draw

    fig = Figure(figsize=(8, height), dpi=110)
    fig.patch.set_facecolor("white")
    return fig


def _style(ax: Any, title: str, source: str) -> None:
    ax.set_title(title, loc="left", fontsize=12, color=INK, pad=10)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.tick_params(colors=INK, labelsize=8)
    ax.figure.text(0.01, 0.01, source, fontsize=7, color=MUTED, ha="left", va="bottom")


def _png(fig: Any) -> str:
    buf = io.BytesIO()
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(buf, format="png")
    return base64.b64encode(buf.getvalue()).decode()


def draw_opportunity(d: market_mod.CompareData, source: str) -> str:
    # Thinly mapped areas get huge, unreliable scores that would flatten every other bar, so
    # they are listed under the chart instead of drawn.
    thin = [a for a in d.areas if a.score and a.thin_map]
    areas = [a for a in d.areas if a.score and not a.thin_map][:MAX_BARS]
    if not areas:
        raise ValueError("no reliably mapped areas to draw")
    fig = _figure(max(3.0, 0.32 * len(areas) + 1.5))
    ax = fig.add_subplot(111)
    names = [a.name for a in areas][::-1]
    base = [a.score.base for a in areas if a.score][::-1]
    lo = [a.score.base - a.score.low for a in areas if a.score][::-1]
    hi = [a.score.high - a.score.base for a in areas if a.score][::-1]
    ax.barh(names, base, xerr=[lo, hi], color=ACCENT, error_kw={"ecolor": MUTED, "capsize": 2, "lw": 1})
    ax.axvline(1.0, color=INK, lw=0.8, ls="--")
    label = "Opportunity score vs region (1.0 = region average, dashed); whiskers show low-high"
    if thin:
        label += f"\nNot drawn, map too thin to trust: {', '.join(a.name for a in thin[:4])}"
    ax.set_xlabel(label, fontsize=8, color=INK)
    _style(ax, f"{d.label}: opportunity by {d.level}, {d.within.name}", source)
    return _png(fig)


def draw_income(d: income_mod.IncomeData, source: str, metric: str) -> str:
    rows = [r for r in d.sub_areas if getattr(r.figures, metric) is not None][:MAX_BARS]
    if not rows:
        raise ValueError("no sub-areas with income to draw")
    fig = _figure(max(3.0, 0.3 * len(rows) + 1.5))
    ax = fig.add_subplot(111)
    ax.barh([r.name for r in rows][::-1], [getattr(r.figures, metric) for r in rows][::-1], color=ACCENT)
    own = getattr(d.figures, metric)
    if own:
        ax.axvline(own, color=INK, lw=0.8, ls="--")
    ax.xaxis.set_major_formatter(lambda v, _pos: f"{v:,.0f}")
    ax.set_xlabel(f"{metric.replace('_', ' ')} (€, dashed = {d.place.name})", fontsize=8, color=INK)
    _style(ax, f"Household income by {d.sub_area_level}, {d.place.name}, {d.figures.year}", source)
    return _png(fig)


def draw_age_pyramid(d: demo_mod.DemographicsData, source: str) -> str:
    bands = d.area.age_bands
    if not bands:
        raise ValueError("no age bands for this place (available for municipalities, provinces and regions)")
    fig = _figure(5.5)
    ax = fig.add_subplot(111)
    labels = [b.band for b in bands]
    shares = [b.share_pct or 0 for b in bands]
    ax.barh(labels, shares, color=ACCENT)
    ax.set_xlabel("Share of population (%)", fontsize=8, color=INK)
    _style(ax, f"Age structure, {d.area.place.name}, {d.area.population_year}", source)
    return _png(fig)


def draw_trend(d: demo_mod.DemographicsData, source: str) -> str:
    pts = d.area.population_trend
    if len(pts) < 2:
        raise ValueError("fewer than two years of population")
    fig = _figure(3.8)
    ax = fig.add_subplot(111)
    ax.plot([p.year for p in pts], [p.value for p in pts], marker="o", color=ACCENT)
    ax.set_xticks([p.year for p in pts])
    ax.yaxis.set_major_formatter(lambda v, _pos: f"{v:,.0f}")
    _style(ax, f"Population, {d.area.place.name}", source)
    return _png(fig)


def _source_line(env: Envelope[Any]) -> str:
    pubs = []
    for s in env.sources:
        pub = "© OpenStreetMap contributors" if "OpenStreetMap" in s.publisher else "Fuente: INE"
        if pub not in pubs:
            pubs.append(pub)
    return "; ".join(pubs) + ". Made with spain-open-data-mcp"


async def chart(
    ine: IneData,
    store: osm.SnapshotStore,
    kind: ChartKind,
    place: str,
    category: str | None = None,
    sub_areas: ChartSubAreas = "district",
) -> CallToolResult:
    env: Envelope[Any]
    if kind == "opportunity":
        if not category:
            raise DataError(
                problem="An opportunity chart needs a category.",
                cause="It ranks areas for one business type.",
                fix="Pass category, e.g. 'gym'.",
            )
        env = await market_mod.compare_areas(ine, store, place, category)
        text = market_mod.render_compare(env)
        draw = lambda: draw_opportunity(env.data, _source_line(env))  # noqa: E731
    elif kind == "income":
        env = await income_mod.income(ine, place, sub_areas, "net_income_per_person")
        text = income_mod.render(env)
        draw = lambda: draw_income(env.data, _source_line(env), "net_income_per_person")  # noqa: E731
    else:
        env = await demo_mod.demographics(ine, place)
        text = demo_mod.render(env)
        if kind == "age_pyramid":
            draw = lambda: draw_age_pyramid(env.data, _source_line(env))  # noqa: E731
        else:
            draw = lambda: draw_trend(env.data, _source_line(env))  # noqa: E731

    content: list[Any] = []
    try:
        png = await asyncio.to_thread(draw)
        content.append(ImageContent(type="image", data=png, mime_type="image/png"))
    except Exception as e:  # a broken picture must never cost the answer
        log.warning("chart %s for %r failed: %s", kind, place, e)
        env.checks.append(Check(kind="scope", severity="info", message=f"Chart unavailable: {e}."))
        env.confidence = confidence_from(env.checks, env.confidence)
    body = text.rstrip() + "\n" + render_footer(env.sources, env.checks, env.confidence)
    content.append(TextContent(type="text", text=body))
    return CallToolResult(content=content, structured_content=env.model_dump(mode="json"))
