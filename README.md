# spain-open-data-mcp

<!-- mcp-name: io.github.digvijaysingh-ai/spain-open-data-mcp -->

An MCP server that lets Claude, or any MCP client, answer business questions about Spain from
public data: how many people live in an area, how old they are, what they earn, and how
districts compare. Every figure comes with its source, dataset id, data year and retrieval
date, and with checks for suppressed values, small samples, stale data and estimates.

**Status: early development (milestone 2 of 4).** Population, households and income from INE,
business counts from OpenStreetMap, and market size in euros with an opportunity score work
today, down to census tract. The national
open-data catalogue (datos.gob.es), market sizing, price bands and charts are next. See
[the roadmap](#roadmap).

## What it can answer today

- "How many people live in Valladolid, and how is the population changing?"
- "Which districts of Valladolid have the highest household income?"
- "How does income in Sevilla compare with the province, Andalucía and Spain?"
- "Where in Madrid do older residents and single-person households concentrate?"
- "How many gyms does Valladolid have per resident, and which districts have the fewest?"
- "Which supermarket chains dominate in Las Palmas?"
- "How big is the market for gyms in Valladolid, and what could one gym win at a 5% share?"
- "Which towns in Valladolid province are under-served for pharmacies?"
- "A gym charged €40 a month in Madrid in 2023. What fits in Sevilla today?"

## Tools

| Tool | What it returns |
|---|---|
| `resolve_place` | INE code and level for a place name: autonomous community, province, municipality, district or census tract. Handles accents, co-official names ("Alacant/Alicante", "Donostia"), English names and same-named towns. |
| `demographics` | Population by sex and five-year age band, 5-year trend, household size and type. Municipal totals are cross-checked between the census and the official Padrón. Optional district or tract breakdown. |
| `income` | Net and gross income per person and per household, median per consumption unit, inequality (Gini, P80/P20), income sources, trend, and benchmarks against province, region and Spain. Optional district or tract ranking. |
| `market_size` | Yearly household spending on a category in a place, in euros, with low, base and high values: households × INE household spending × an income adjustment. Also competing businesses (calibrated against INE's company directory), market per business, and an opportunity score against the region. Every assumption is listed and can be overridden. |
| `compare_areas` | Ranks a municipality's districts or tracts, or a province's or region's municipalities, by opportunity for a category, using the cautious end of each score. Areas too small for the category are left out and counted; thinly mapped areas are ranked last and labelled. |
| `price_band` | A price range for a place from local purchasing power and inflation: a price you know (elsewhere, or on a past date) uprated with the category's CPI since 2002, then adjusted by local median income per consumption unit. Labelled as an estimate: public data has no competitor prices. |
| `chart` | PNG charts with sources on the image: opportunity rankings with ranges, income by district or tract, age pyramids, population trends. If drawing fails, the text answer still comes back. |
| `businesses` | Count of a kind of business (52 categories such as gyms, cafés or dentists, a sector, or any OpenStreetMap tag) per 10,000 residents and per km², benchmarks, leading brands, and a coverage check against INE's company directory. Optional district or tract ranking. |

All tools are read-only. Each result has the same shape:

```
data        the figures
sources     publisher, dataset, dataset id, URL, licence, data period, retrieval date
checks      suppressed | missing | stale | thin_sample | estimate | cross_check | scope, with severity
confidence  high | medium | low: the weakest input sets it
```

Example (abridged) for `income("Valladolid", sub_areas="district")`:

```
Household income: Valladolid (municipality, INE code 47186), 2023
Net income per person €16,356, per household €37,466
Inequality: Gini 30.5, P80/P20 2.6
Net income per person by year: 2019 €14,099 ... 2023 €16,356 (+16.0%)
Benchmark Valladolid (province): net per person €15,590; Valladolid is +4.9%
Benchmark España (country): net per person €15,036; Valladolid is +8.8%
Districts: 12 total. Net income per person across them: min €12,884, median €18,466, max €27,922
...
Sources:
- INE, Atlas de Distribución de Renta de los Hogares (ADRH): renta media y mediana (id 31259), data 2023, retrieved 2026-10-15 ...
Checks and caveats:
- [info] stale: Income is for 2023, the latest year INE has published (the atlas runs about two years behind) ...
Confidence: high
```

Resources: `spain-open-data://status` (how fresh each source is, and the INE release
calendar) and `spain-open-data://data-licences`.

## How the data is checked

- **Suppressed is not zero.** INE withholds values for small areas. These are reported as suppressed and left out of rankings, with a count.
- **Top-coded incomes are flagged.** INE caps the highest incomes in its income atlas. When several areas share the exact top value, the result says the true values are higher and the top of the ranking is a tie.
- **Two sources, one number.** Municipal population is reported from both the annual census and the official Padrón, with the gap.
- **Spelling guesses are never silent.** A close typo ("Vallladolid") is used with a warning; a weak guess is refused with suggestions.
- **Map gaps are measured.** OpenStreetMap counts are lower bounds. Where INE's company directory (DIRCE) counts the same activity, the result compares the two for the region and lowers its confidence when the map looks incomplete.
- **Upstream changes fail loudly.** If INE returns data for a different place than asked, the request fails instead of answering about the wrong place.
- **No personal data.** The server returns aggregate statistics only. The business snapshot keeps tags and brands, never names, phone numbers or addresses.

## Install

Requires [uv](https://docs.astral.sh/uv/). No API keys are needed.

**Claude Code**

```bash
claude mcp add spain-open-data -- uvx --from git+https://github.com/digvijaysingh-ai/spain-open-data-mcp spain-open-data-mcp
```

**Claude Desktop**: add this to `claude_desktop_config.json` and restart Claude.

```json
{
  "mcpServers": {
    "spain-open-data": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/digvijaysingh-ai/spain-open-data-mcp", "spain-open-data-mcp"]
    }
  }
}
```

A PyPI release (`uvx spain-open-data-mcp`) and a hosted URL for use as a custom connector in
Claude are planned for milestone 4.

### Settings

| Variable | Default | Purpose |
|---|---|---|
| `SPAIN_MCP_CACHE_DIR` | `~/.cache/spain-open-data-mcp` | On-disk cache of upstream responses |
| `SPAIN_MCP_HTTP_TIMEOUT` | `60` | Seconds per upstream request |
| `SPAIN_MCP_OFFLINE` | unset | `1` serves only from cache (tests, evals) |
| `SPAIN_MCP_OSM_DB` | unset | Path to an OpenStreetMap snapshot file. By default it is downloaded (about 21 MB) on first use and refreshed monthly. |

## Architecture

```
src/spain_open_data_mcp/
  server.py        MCP tools, resource and error handling
  tools/           one module per tool: fetch, check, render
  sources/ine/     INE Tempus API client and table definitions
  sources/osm/     OpenStreetMap snapshot: business categories, download, queries
  places/          place resolver and INE codes
  core/            cached HTTP client, response envelope, errors
  data/            bundled municipality list and INE table map (built by scripts/build_reference.py)
```

- Upstream responses are cached on disk with their original retrieval time, so cached figures still report when they were fetched. If an upstream service is down, the last cached copy is served and marked stale.
- Requests are rate limited per host, retried with backoff, and identify the project in the User-Agent.
- Business counts come from a monthly snapshot rather than live Overpass queries, which are slow and rate limited. `scripts/build_osm_snapshot.py` reads the Geofabrik extracts for Spain and the Canary Islands, keeps shops and services, and places each one in an INE census tract. A [GitHub Action](.github/workflows/osm-snapshot.yml) publishes it on the [osm-snapshot-v1 release](https://github.com/digvijaysingh-ai/spain-open-data-mcp/releases/tag/osm-snapshot-v1) each month.
- INE table ids are not hard-coded in the tools. `scripts/build_reference.py` rebuilds the map when INE publishes new tables, for example each October's income atlas.

## Development

```bash
uv sync
uv run pytest            # offline tests against recorded INE responses
uv run ruff check . && uv run ruff format --check . && uv run pyright
uv run spain-open-data-mcp          # stdio
uv run spain-open-data-mcp http     # Streamable HTTP on 127.0.0.1:8000/mcp
```

Refresh the recorded test fixtures with `uv run python tests/fixtures/record_ine.py`. Build the
OpenStreetMap snapshot locally (a 1.5 GB download, then about 5 minutes) with
`uv run --group snapshot python scripts/build_osm_snapshot.py`, then point `SPAIN_MCP_OSM_DB` at
`build/osm/osm_businesses.sqlite`.

## Roadmap

1. **Skeleton**: place resolver, `demographics`, `income`, CI.
2. **Supply and catalogue** (in progress): OpenStreetMap business counts from a monthly snapshot, cross-checked against INE's company directory (DIRCE) (done); datos.gob.es search and dataset reading; `market_size`, `price_band`, `compare_areas`, `chart`; rental reference index and crime statistics by municipality.
3. **Checks and evals**: a golden set of real strategy questions with hand-checked answers, scored for accuracy, sourcing, refusals and calibration, with results here.
4. **Publishing**: hosted connector, PyPI, MCP Registry, Claude connectors directory.

## Data licences

The code is MIT-licensed. Data keeps its source licence; see
[data_licences.md](src/spain_open_data_mcp/data/data_licences.md). INE data is reused under
CC BY 4.0: "Fuente: INE. Elaboración propia con datos extraídos del sitio web del INE: www.ine.es".
OpenStreetMap data is © OpenStreetMap contributors, under the
[Open Database Licence](https://www.openstreetmap.org/copyright).
