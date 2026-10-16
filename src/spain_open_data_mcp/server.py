"""The MCP server: tool, resource and prompt registration."""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from importlib import resources
from typing import Annotated, ParamSpec

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, ToolAnnotations
from pydantic import Field

from . import __version__
from .categories import BY_KEY, MARKET, SECTORS
from .core.envelope import Check, Envelope, confidence_from
from .core.errors import DataError
from .core.http import HttpClient
from .places.resolver import Place, resolve
from .sources.ine.datasets import IneData, register_source
from .sources.ine.tempus import TempusClient
from .sources.osm.snapshot import SnapshotStore
from .tools import businesses as businesses_mod
from .tools import demographics as demographics_mod
from .tools import income as income_mod
from .tools import market as market_mod
from .tools.common import to_result

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
Answers market questions about Spain from official public data (INE) and OpenStreetMap:
population, households, income, businesses, market size in euros and where the
opportunity is, down to municipality, district and census tract.

How to use:
- For "how big is the market" use market_size; for "where should I open" use
  compare_areas. Both give low, base and high values: report the range, not just the base.
- Start with resolve_place to turn a place name into an INE code, then pass the code
  to other tools. Tools also accept names directly.
- Every result lists its sources (dataset id, URL, data year, retrieval date) and
  checks (suppressed values, small samples, stale data, estimates). End your answer
  with a short "Sources and caveats" block built from them.
- Never state a conclusion more strongly than the lowest `confidence` among the
  results you used.
- This server returns aggregate statistics only. It holds no data about individuals;
  decline requests to identify or profile people.
"""

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)
LOCAL_READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

mcp = MCPServer(
    name="spain-open-data",
    title="Spain Open Data",
    description="Market questions about Spain from public data, with every figure sourced and checked.",
    instructions=INSTRUCTIONS,
    website_url="https://github.com/digvijaysingh-ai/spain-open-data-mcp",
    version=__version__,
)


class _Services:
    """Created on first use so importing the module has no side effects."""

    _ine: IneData | None = None
    _osm: SnapshotStore | None = None

    @classmethod
    def ine(cls) -> IneData:
        if cls._ine is None:
            cls._ine = IneData(TempusClient(HttpClient()))
        return cls._ine

    @classmethod
    def set_ine(cls, ine: IneData) -> None:
        cls._ine = ine

    @classmethod
    def osm(cls) -> SnapshotStore:
        if cls._osm is None:
            cls._osm = SnapshotStore()
        return cls._osm

    @classmethod
    def set_osm(cls, store: SnapshotStore) -> None:
        cls._osm = store


P = ParamSpec("P")


def guarded(fn: Callable[P, Awaitable[CallToolResult]]) -> Callable[P, Awaitable[CallToolResult]]:
    """Turn every failure into a 'problem / cause / fix' message the model can act on."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> CallToolResult:
        try:
            return await fn(*args, **kwargs)
        except DataError as e:
            raise ToolError(e.render()) from e
        except Exception as e:
            log.exception("tool %s failed", fn.__name__)
            raise ToolError(
                f"Problem: the {fn.__name__} tool failed unexpectedly.\n"
                f"Cause: {type(e).__name__} inside the server (a bug, not your input).\n"
                "Fix: try again; if it repeats, report it at "
                "https://github.com/digvijaysingh-ai/spain-open-data-mcp/issues"
            ) from e

    return wrapper


PlaceArg = Annotated[
    str,
    Field(
        description="Place name or INE code: municipality ('Valladolid' or '47186'), province ('41'; a "
        "bare name like 'Sevilla' means the city), autonomous community ('Cataluña' or 'CA09'), district "
        "('4718603') or census tract ('4718603012'). 'Name, Province' picks between same-named towns."
    ),
]


@mcp.tool(
    title="Resolve a Spanish place",
    annotations=LOCAL_READ_ONLY,
)
@guarded
async def resolve_place(
    query: Annotated[
        str, Field(description="Place name or INE code, e.g. 'A Coruña', 'Mérida, Badajoz', '08'.")
    ],
) -> Annotated[CallToolResult, Envelope[list[Place]]]:
    """Find the INE code and level (CCAA, province, municipality, district, tract) for a place in Spain.

    Use this first when a question names a place. Returns the best match first, with
    alternatives (e.g. 'Valencia' is both a city and a province). Handles accents, Basque,
    Catalan and Galician names, and common English names (Seville, Catalonia).
    Neighbourhood names are not supported; use districts or tracts from `demographics`
    or `income` with `sub_areas`.
    """
    places = resolve(query, limit=6)
    lines = [f"Matches for '{query}' (best first):"]
    for p in places:
        extra = f", {p.province}" if p.province and p.level != "province" else ""
        pop = f", population {p.population:,} ({p.population_date})" if p.population else ""
        lines.append(f"- {p.name} ({p.level}{extra}): code {p.code}{pop}")
    checks = []
    if places[0].match == "fuzzy":
        checks.append(
            Check(
                kind="scope",
                severity="warning",
                message=f"No exact match for '{query}'; these are spelling guesses.",
            )
        )
    env: Envelope[list[Place]] = Envelope(
        data=places, sources=[register_source()], checks=checks, confidence=confidence_from(checks)
    )
    return to_result(env, "\n".join(lines))


@mcp.tool(title="Population and households", annotations=READ_ONLY)
@guarded
async def demographics(
    place: PlaceArg,
    sub_areas: Annotated[
        demographics_mod.SubAreaLevel,
        Field(
            description="For a municipality, also list its 'district's or census 'tract's. Default 'none'."
        ),
    ] = "none",
) -> Annotated[CallToolResult, Envelope[demographics_mod.DemographicsData]]:
    """Population, sex, five-year age bands, 5-year trend, and household profile for a place in Spain.

    Municipality, province and CCAA: census population by age band (latest 1 January),
    cross-checked against the official Padrón figure. Municipality, district and tract:
    household size, single-person households, mean age, share under 18 and 65+ from INE's
    income atlas. Use sub_areas to compare a city's districts or tracts, e.g. to find
    where young families or older residents concentrate.
    """
    return await demographics_mod.demographics_tool(_Services.ine(), place, sub_areas)


@mcp.tool(title="Household income", annotations=READ_ONLY)
@guarded
async def income(
    place: PlaceArg,
    sub_areas: Annotated[
        income_mod.SubAreaLevel,
        Field(description="For a municipality, rank its 'district's or census 'tract's. Default 'none'."),
    ] = "none",
    rank_by: Annotated[
        income_mod.RankBy, Field(description="Metric used to rank sub-areas.")
    ] = "net_income_per_person",
    years: Annotated[int, Field(ge=1, le=9, description="Years of history for the trend (max 9).")] = 5,
) -> Annotated[CallToolResult, Envelope[income_mod.IncomeData]]:
    """Household income for a place in Spain from INE's income atlas, down to census tract.

    Returns net and gross income per person and per household, mean and median per
    consumption unit, inequality (Gini, P80/P20), income sources (wages, pensions,
    benefits), a trend, and benchmarks against the province, region and Spain. Use it
    for purchasing power, price positioning and choosing between districts. Figures are
    euros of the data year; the atlas runs about two years behind.
    """
    return await income_mod.income_tool(_Services.ine(), place, sub_areas, rank_by, years)


@mcp.tool(title="Businesses and competitors", annotations=READ_ONLY)
@guarded
async def businesses(
    place: PlaceArg,
    category: Annotated[
        str | None,
        Field(
            description="What to count. A category: "
            + ", ".join(BY_KEY)
            + ". Plain words work too ('gyms', 'peluquerías'). Or a sector: "
            + ", ".join(SECTORS)
            + ". Or OpenStreetMap tags: 'shop=bakery' or 'amenity=bar,amenity=pub'. Leave empty for all "
            "businesses, broken down by category."
        ),
    ] = None,
    sub_areas: Annotated[
        businesses_mod.SubAreaLevel,
        Field(description="For a municipality, also count by 'district' or census 'tract'. Default 'none'."),
    ] = "none",
    rank_by: Annotated[
        businesses_mod.RankBy,
        Field(description="How to rank sub-areas: 'count', 'per_10k_residents' or 'per_km2'."),
    ] = "count",
) -> Annotated[CallToolResult, Envelope[businesses_mod.BusinessesData]]:
    """Count businesses of a kind in a place in Spain, per 10,000 residents and per km², from OpenStreetMap.

    Use it to size competition and supply: how many gyms, cafés or dentists an area has,
    how that compares with the province, region and Spain, which brands dominate, and
    which districts or tracts are over- or under-served (rank sub-areas by
    per_10k_residents). Counts are lower bounds because OpenStreetMap is mapped by
    volunteers; where INE's company directory covers the same activity, the result says
    how complete the map looks in that region. Returns counts and brands only, never
    lists of individual premises.
    """
    return await businesses_mod.businesses_tool(
        _Services.ine(), _Services.osm(), place, category, sub_areas, rank_by
    )


MarketCategoryArg = Annotated[
    str,
    Field(
        description="Business category: "
        + ", ".join(k for k, m in MARKET.items() if not m.visitor_only)
        + ". Plain words work too ('gyms', 'peluquerías')."
    ),
]


@mcp.tool(title="Market size in euros", annotations=READ_ONLY)
@guarded
async def market_size(
    place: PlaceArg,
    category: MarketCategoryArg,
    spend_per_household: Annotated[
        float | None,
        Field(description="Override INE's yearly spend per household on the category, in euros."),
    ] = None,
    capture_share: Annotated[
        float | None,
        Field(
            description="Share of the market one business could win (0-1), to estimate obtainable revenue."
        ),
    ] = None,
    competitors: Annotated[
        float | None, Field(description="Override the estimated number of competing businesses.")
    ] = None,
) -> Annotated[CallToolResult, Envelope[market_mod.MarketData]]:
    """Size the yearly market for a business category in a place in Spain, in euros, with a range.

    Market = households × INE household spending on the category × an income adjustment.
    Supply = competing businesses, calibrated against INE's company directory. Returns market
    per business and an opportunity score against the region (1.0 = same as the region), with
    every assumption listed. Assumptions can be overridden to test scenarios. Use for "how big
    is the market for X in Y" and "is Y under-served for X".
    """
    return await market_mod.market_size_tool(
        _Services.ine(), _Services.osm(), place, category, spend_per_household, capture_share, competitors
    )


@mcp.tool(title="Compare areas for a business", annotations=READ_ONLY)
@guarded
async def compare_areas(
    within: Annotated[
        str,
        Field(
            description="A municipality (compares its districts or tracts) or a province or region (compares "
            "its municipalities)."
        ),
    ],
    category: MarketCategoryArg,
    level: Annotated[
        market_mod.CompareLevel,
        Field(
            description="'district' or 'tract' within a municipality; 'municipality' within a province or region."
        ),
    ] = "auto",
    limit: Annotated[int, Field(ge=1, le=40, description="How many areas to return, best first.")] = 20,
) -> Annotated[CallToolResult, Envelope[market_mod.CompareData]]:
    """Rank areas by opportunity for a business category: where is there most market per business.

    Each area gets a yearly market in euros, estimated competing businesses and an opportunity
    score against its region, all with ranges. Areas are ranked by the cautious (low) end of
    the score; areas too small for the category are left out and counted. Use for "where in
    Valladolid should I open a gym" or "which towns in Málaga province lack pharmacies".
    """
    return await market_mod.compare_areas_tool(
        _Services.ine(), _Services.osm(), within, category, level, limit
    )


@mcp.resource(
    "spain-open-data://data-licences",
    name="data-licences",
    title="Data sources and licences",
    mime_type="text/markdown",
)
def data_licences() -> str:
    """Where each figure comes from, its licence and the attribution it requires."""
    return (
        resources.files("spain_open_data_mcp.data").joinpath("data_licences.md").read_text(encoding="utf-8")
    )
