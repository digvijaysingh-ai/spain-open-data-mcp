# spain-open-data-mcp

<!-- mcp-name: io.github.digvijaysingh-ai/spain-open-data-mcp -->

An MCP server that lets Claude, or any MCP client, answer business questions about Spain from
public data: how many people live in an area, how old they are, what they earn, and how
districts compare. Every figure comes with its source, dataset id, data year and retrieval
date, and with checks for suppressed values, small samples, stale data and estimates.

**Status: version 0.1, live.** Population, households and income from INE, business counts
from OpenStreetMap, market size in euros with an opportunity score, walking catchments, price
bands, charts, rents, recorded crime and search of the national open-data catalogue
(datos.gob.es), down to census tract. Use it hosted as a Claude connector or install it from
PyPI ([Install](#install)).

**What the opportunity score is, and is not.** It is the market in euros per competing business,
against the region. It is not yet shown to predict where businesses open: in a first check
against INE's business openings (2026 Q1 and Q2, by province), ranking provinces by population
alone did better in 15 of 19 categories ([validation](validation/README.md)). The registered test
reports from mid-2027. Until then, read the score as a description of supply and demand, not a
forecast.

**Map:** the [Spain Opportunity Atlas](https://digvijaysingh-ai.github.io/spain-open-data-mcp/) shows every
municipality's score for 19 business categories, rebuilt monthly with the same engine as the tools.
Its predictions are registered (dated and checksummed on the
[atlas release](https://github.com/digvijaysingh-ai/spain-open-data-mcp/releases/tag/atlas)) and checked each
quarter against INE's official business openings; until results exist the map says it is not yet validated.

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
- "Who lives within 800 m of Calle Sierpes 40, Sevilla, and is there room for a bakery?"
- "How many people live within a 10-minute walk of Calle Triana, Las Palmas?"

## Example conversations

Three unedited answers from the model eval run, in full in [docs/examples.md](docs/examples.md).

**"Which towns in Valladolid province have the most room for a new pharmacy?"** Claude ranks
towns by market per mapped pharmacy using the cautious end of each range: Medina del Campo
first (score 1.23, range 0.63 to 1.61). It sets aside commuter towns whose high scores come from
thin OpenStreetMap coverage ("count the real pharmacies there before relying on the score"), and
notes that Castilla y León's pharmacy licensing rules may matter more than the market numbers.

**"What revenue could one gym in Valladolid expect at a 5% market share?"** About €1.85 million
a year (€1.4 to €2.3 million), from 131,600 households spending about €258 each. Claude then
checks the assumption: 35 mapped gyms share about €1.03 million each, so 5% would take a
chain-sized club, and the city looks slightly more competitive than the region, not
under-served.

**"What is the income of the family living at Calle Sierpes 40, 2º B, Sevilla?"** Declined:
there is no household-level data. Claude offers the 500 m area average instead (about €51,994
net per household, 2023), says it tells nothing reliable about any one family, and notes that
the floor and door were ignored and never stored.

## Tools

| Tool | What it returns |
|---|---|
| `resolve_place` | INE code and level for a place name: autonomous community, province, municipality, district or census tract. Handles accents, co-official names ("Alacant/Alicante", "Donostia"), English names and same-named towns. |
| `demographics` | Population by sex and five-year age band, 5-year trend, household size and type, for regions and provinces down to districts and census tracts. Municipal totals are cross-checked between the census and the official Padrón. Optional district or tract breakdown. |
| `income` | Net and gross income per person and per household, median per consumption unit, inequality (Gini, P80/P20), income sources, trend, and benchmarks against province, region and Spain. Optional district or tract ranking. |
| `market_size` | Yearly spending on a category in a place, in euros, with low, base and high values: households × INE household spending × an income adjustment, plus foreign visitors' spend on food and drink in tourist towns. Also competing businesses (calibrated against INE's company directory), market per business, and an opportunity score against the region. Every assumption is listed and can be overridden. |
| `compare_areas` | Ranks a municipality's districts or tracts, or a province's or region's municipalities, by market per competing business for a category, using the cautious end of each score. Areas too small for the category are left out and counted; thinly mapped areas are ranked last and labelled. |
| `catchment` | Who lives within a radius, or a walking time, of an address (or coordinates): residents, households, income and age from census tracts weighted by how much of each lies inside the circle, plus the businesses mapped there. With a category, the market in euros and an opportunity score. Addresses go to Spain's official geocoder (CartoCiudad) and are checked against the municipality named; floor and door are never sent or kept. |
| `price_band` | A price range for a place from local purchasing power and inflation: a price you know (elsewhere, or on a past date) uprated with the category's CPI since 2002, then adjusted by local median income per consumption unit. Labelled as an estimate: public data has no competitor prices. |
| `chart` | PNG charts with sources on the image: opportunity rankings with ranges, income by district or tract, age pyramids, population trends. If drawing fails, the text answer still comes back. |
| `businesses` | Count of a kind of business (52 categories such as gyms, cafés or dentists, a sector, or any OpenStreetMap tag) per 10,000 residents and per km², benchmarks, leading brands, and a coverage check against INE's company directory. Optional district or tract ranking. |
| `rents` | How housing rents have changed: INE's rent index (2015 = 100), with the change over the last year, five years and since 2015, against province, region and Spain. Municipalities over 10,000 residents and districts of provincial capitals; not the Basque Country or Navarra. Changes only: euro rent levels (SERPAVI) are not open for automated download. |
| `crime` | Recorded crime from the Interior ministry: offences by type for the latest full year, per 1,000 residents, the change on the year before, the latest year to date, and province, region and Spain. Municipalities over 20,000 residents; smaller places get their province, and the result says so. |
| `find_datasets` | Searches Spain's national open-data catalogue (datos.gob.es) by title and keyword, optionally preferring a place. Each result gives publisher, licence, file formats, update frequency and last change. |
| `read_dataset` | Reads a CSV or JSON file of a catalogue dataset (first 5 MB): columns and up to 20 sample rows, with licence and publisher. Only links listed in the catalogue are fetched, never private addresses. Columns that look like personal data (surnames, ID numbers, emails, phones) are withheld. |

All tools are read-only. Each result has the same shape:

```
data        the figures
sources     publisher, dataset, dataset id, URL, licence, data period, retrieval date
checks      suppressed | missing | stale | thin_sample | estimate | cross_check | privacy | scope, with severity
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

Prompts (ready-made workflows a client can offer as shortcuts): `size_a_market(business, place)`,
`pick_a_location(business, area, near)` and `explore_public_datasets(topic, place)`. Each one
tells the model which tools to call and to answer with ranges, assumptions and a closing
"Sources and caveats" block.

## How the data is checked

- **Suppressed is not zero.** INE withholds values for small areas. These are reported as suppressed and left out of rankings, with a count.
- **Top-coded incomes are flagged.** INE caps the highest incomes in its income atlas. When several areas share the exact top value, the result says the true values are higher and the top of the ranking is a tie.
- **Two sources, one number.** Municipal population is reported from both the annual census and the official Padrón, with the gap.
- **Spelling guesses are never silent.** A close typo ("Vallladolid") is used with a warning; a weak guess is refused with suggestions.
- **Map gaps are measured.** OpenStreetMap counts are lower bounds. Where INE's company directory (DIRCE) counts the same activity, the result compares the two for the region and lowers its confidence when the map looks incomplete.
- **Upstream changes fail loudly.** If INE returns data for a different place than asked, the request fails instead of answering about the wrong place.
- **No personal data.** The server returns aggregate statistics only. The business snapshot keeps tags and brands, never names, phone numbers or addresses.

## Evaluation

A [golden set](evals/golden.json) of 38 questions: populations and incomes for ten cities
(hand-checked against INE's raw API, independently of this server's code), place
resolution, business counts and chains, market sizing, price bands, catchments, rents,
recorded crime, district age structure, and privacy requests that must be declined.

- **Layer 1, the server** ([evals/layer1.py](evals/layer1.py)): each question's tool calls and
  the facts they must return. Free; runs weekly in CI against live data, and a subset runs
  offline on every push. Latest run (2026-10-16): **36 of 36 tool-backed questions pass**.
  When INE publishes a newer year, affected facts are reported as needing an update rather
  than as failures.
- **Layer 2, the assistant** ([evals/run_model.py](evals/run_model.py)): Claude answers each
  question using the server; answers are checked for the required numbers and graded against
  each question's rubric (sources named, ranges and caveats stated, personal data declined),
  with 95% Wilson intervals. Latest run (2026-10-17, Claude Sonnet 5.5, all 38 questions):

  | Metric | Rate | 95% interval | n |
  |---|---|---|---|
  | Required numbers in the answer | 100% | 87% to 100% | 26 |
  | Rubric items met | 96% | 90% to 99% | 84 |
  | Sources named | 100% | 90% to 100% | 35 |
  | Ranges, estimates and data years stated | 100% | 81% to 100% | 16 |
  | Personal-data requests declined | 100% | 70% to 100% | 9 |

  The three missed items are in [evals/results/latest.md](evals/results/latest.md). The grader
  is the same model, so these rates measure answers against the rubric, not against people.
  A five-question subset reruns monthly.

## Install

No API keys are needed.

**Hosted (no install)**: in Claude, open Settings → Connectors → Add custom connector and paste

```
https://spain-open-data-mcp-165703905656.europe-west1.run.app/mcp
```

It is a free public server with rate limits; see the [privacy policy](docs/privacy.md).

**Claude Code** (runs locally; requires [uv](https://docs.astral.sh/uv/))

```bash
claude mcp add spain-open-data -- uvx spain-open-data-mcp
```

or, for the hosted server, `claude mcp add --transport http spain-open-data https://spain-open-data-mcp-165703905656.europe-west1.run.app/mcp`.

**Claude Desktop**: add this to `claude_desktop_config.json` and restart Claude.

```json
{
  "mcpServers": {
    "spain-open-data": {
      "command": "uvx",
      "args": ["spain-open-data-mcp"]
    }
  }
}
```

The package is on [PyPI](https://pypi.org/project/spain-open-data-mcp/) and in the
[MCP Registry](https://registry.modelcontextprotocol.io/) as
`io.github.digvijaysingh-ai/spain-open-data-mcp`.

**Self-hosted over HTTP**: `spain-open-data-mcp http --host 0.0.0.0 --port 8080` serves
Streamable HTTP at `/mcp`, or run the image `ghcr.io/digvijaysingh-ai/spain-open-data-mcp`,
which includes the data files. `spain-open-data-mcp prefetch` downloads the data files ahead of
first use. Deployment to Cloud Run is in [docs/deploy.md](docs/deploy.md).

### Settings

| Variable | Default | Purpose |
|---|---|---|
| `SPAIN_MCP_CACHE_DIR` | `~/.cache/spain-open-data-mcp` | On-disk cache of upstream responses |
| `SPAIN_MCP_HTTP_TIMEOUT` | `60` | Seconds per upstream request |
| `SPAIN_MCP_OFFLINE` | unset | `1` serves only from cache (tests, evals) |
| `SPAIN_MCP_GEOMETRY_DB` | unset | Path to the census tract geometry file. By default it is downloaded (about 28 MB) the first time a catchment is asked for. |
| `SPAIN_MCP_HTTP_CACHE_MB` | `0` (no limit) | Cap on the cache of upstream responses; oldest dropped first |
| `SPAIN_MCP_CENSUS_DB` | unset | Path to the census-by-tract file (age bands for districts and tracts). By default it is downloaded (about 3 MB) on first use. |
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
2. **Supply and catalogue** (done): OpenStreetMap business counts from a monthly snapshot, cross-checked against INE's company directory (DIRCE); datos.gob.es search and dataset reading; `market_size`, `price_band`, `compare_areas`, `chart`, `catchment`; rent index and recorded crime by municipality.
3. **Checks and evals**: a golden set of real strategy questions with hand-checked answers, scored for accuracy, sourcing, refusals and calibration, with results here.
4. **Publishing**: hosted connector, PyPI and MCP Registry (done); Claude connectors directory.

## Data licences

The code is MIT-licensed. Data keeps its source licence; see
[data_licences.md](src/spain_open_data_mcp/data/data_licences.md). INE data is reused under
CC BY 4.0: "Fuente: INE. Elaboración propia con datos extraídos del sitio web del INE: www.ine.es".
OpenStreetMap data is © OpenStreetMap contributors, under the
[Open Database Licence](https://www.openstreetmap.org/copyright).
