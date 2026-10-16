# Design: opportunity engine and Spain Opportunity Atlas
Status: accepted, 2026-10-16. Reviewed for strategy and for engineering, each with an independent
second reviewer. Decisions are listed with their review codes (D, CM, E, OV) so later changes can
refer to them.

## Context
Milestone 1 (place resolver, demographics, income) is on main, and so is the businesses tool
with its OSM snapshot and DIRCE coverage check (PR #12, published as osm-snapshot-v1). The ADRH 2024 income release is due
2026-10-19 (issue #1). Several MCP servers already wrap INE (mcp-ine, ine-mcp, Pipeworx
ine-es) and one covers INE, BOE, BORME and datos.gob.es with 26 tools (Spanish Public Data
Radar). Raw access is a commodity. The project wins on decisions with honest error bars and on
published proof that they work.

Measured on 2026-10-16: OSM coverage against DIRCE companies is 0.10 (personal services,
Andalucía) to 0.65 (food and drink, Madrid region). Raw OSM counts cannot carry a supply-gap
ranking on their own.

Change against BRIEF.md: the brief puts datos.gob.es in phase 2. This plan moves it after
hosting, because it is the most commoditised piece. The README roadmap is updated to match.
The brief's tourism-datasets example question is answered after that move.

## Vision

### 10x Check
A Spain Opportunity Atlas. Every municipality and district gets an opportunity score for 20
business categories, from demand (residents and visitors), spending power (euros per household
for the category) and calibrated supply (DIRCE regional company count distributed by OSM's local
share). Supply refreshes monthly with OSM; demand refreshes yearly with INE releases; each
release states the vintage of every input. Each method version's scores are frozen and checked
against INE's official business openings as each quarter is published.

### Platonic Ideal
Asked "restaurants in Valladolid?", Claude returns in about 20 seconds a one-page brief: the
yearly market in euros as low, base and high; the three best districts on a map; which chains
operate there; a price band that fits local incomes; editable assumptions; one line on
confidence and why; a verify link on every figure. The README shows how often the score was
right in past years.

Sub-categories such as "Indian restaurant": demand share comes from outside local supply
(the regional OSM cuisine share of the parent category, shown in the assumption table as an
overridable assumption; no scaling by immigration figures, per CM5); the local OSM cuisine
share is used for supply only. Otherwise the share would cancel in market / supply and a gap could
never show. No official source splits by cuisine, so these answers always carry a
low-confidence check.

## Chosen approach (D1): C, opportunity engine
One engine, three consumers: the market_size and compare_areas tools, charts, and the atlas
export (which includes exports/hero_spain.json for djcrafted.com). The golden-set evals are
written before the engine code.

### Review decisions so far
- 1.1 A: engine/ is pure functions (no network, no files). A data-assembly layer fetches
  inputs; tools call the engine per request; the atlas script calls the same functions.
- 1.2 A: geocoding with CartoCiudad (IGN, official, open licence); Nominatim fallback at most
  1 request per second, only when CartoCiudad returns nothing.
- 1.3 B: exact tract shapes. Simplified tract polygons (about 10 m tolerance) are stored in the
  snapshot; shapely and matplotlib become runtime dependencies. Cloud Run memory and cold start
  are re-measured before hosting.
- 1.4 A: backtest spike moves to step 3.
- 2.1 A: snapshot integrity check on open, one automatic re-download, clear error otherwise;
  fixed in PR #12 before merge, with a truncated-file test.
- 2.2 A: charts degrade gracefully; text and data always returned, chart failure is a visible
  check and a log line.
- 2.3 A: every atlas row records each input's status (ok, estimated, missing); release fails if
  over 1% of rows miss an input; gaps listed in release notes; the map shows "no data", never 0.
- 3.1 A: addresses never logged (logs keep municipality and radius only); floor and door
  stripped before geocoding; returned points rounded to about 100 m; privacy policy names
  CartoCiudad.
- 3.2 A: one sanitiser for all upstream free text (control characters, newlines, length);
  brands need 3 or more places; upstream strings quoted as data in text output; malicious-brand
  test. Done in PR #12.
- 4.1 A: market_size overrides are a fixed typed set (spend per household, capture share,
  visitor share, supply override) with bounds; every answer shows an assumption table
  (default, used, source); user values marked and confidence capped at medium.
- 5.1 A: areas module (benchmark_ladder, population_of, sub_units) extracted in its own PR before
  the engine; income and businesses use it; businesses() split into steps.
- 6.1 A: Hypothesis property tests (dev only) for low <= base <= high, monotonicity in supply and
  households, finite scores, and whole-municipality catchment = municipality totals.
- 7.1 A: tract bounding boxes as indexed columns; catchments load only nearby shapes; LRU cache;
  peak-memory test.
- 8.1 A: data-status MCP resource (every source, data date, retrieval, next expected release,
  overdue flag); scheduled jobs open and close a GitHub issue on failure and recovery; one
  structured log line per tool call, never addresses.
- 9.1 A: one release tag per snapshot schema (osm-snapshot-v1 now, in PR #12); old schemas keep
  their last good file with a stale note.
- 10.1 A: engine method version in every output, METHODOLOGY.md with formulas and changelog,
  backtest re-run on each method change.
- 11.1 A: MapLibre page on GitHub Pages, per-category JSON, full state design, mobile list,
  accessible table, credits visible; /plan-design-review before building it.

## Engine specification

Units: euros per year, people, households. Every quantity is a triple (low, base, high).
Ranges propagate by interval arithmetic on monotone formulas (no Monte Carlo): low inputs give
the low output where the formula increases in that input, and the reverse where it decreases.

Inputs per area A (municipality, district, tract or catchment) and category K:
- households(A) = residents(A) / avg household size(A). Residents: census (municipality and
  up) or ADRH (district, tract). Catchments: tract values × share of tract area inside the
  circle (exact geometry).
- spend_hh(K, R, band) = EPF euros per household per year for the ECOICOP 4-digit code(s) of K,
  region R (table 73993). Regional 4-digit cells are noisy (Castilla y León restaurants
  +33% from 2023 to 2025) and some are published as 0 when the sample is too small; a 0 is
  treated as missing. Base value: the national 4-digit amount × the region's 3-digit
  (subgroup) ratio to national, averaged over the last 3 EPF years.
  Income is handled once, through the income band (see Market). No separate income factor.
- visitor_nights(A): hotel overnight stays where A is an INE tourist point (EOH tables
  75197/75198) plus tourist-flat nights shared by beds (table 39363); elsewhere, provincial
  overnight stays (hotels and flats) shared out by A's share of the province's tourist beds.
  Fallback is flagged as an estimate.
- spend_visitor_night(K): foreign visitors from EGATUR, domestic visitors from INE's
  Residents Travel Survey (ETR), weighted by the EOH resident/non-resident split of nights,
  × share of category K in visitor spend. Only for tourism-sensitive categories.

Market:
  market_eur(A, K) = households(A) × spend_hh(K, R, band(A)) + visitor_nights(A) × spend_visitor_night(K)
  Low/high: spend × (1 ± 1.96 × CV), CV from EPF table 74005, capped at ±50%; ±20% on
  visitor terms.
  Income band: ADRH yearly household income / 12, deflated with CPI to the EPF year; the
  band ratio is interpolated linearly between band midpoints of table 74002. That table gives
  total spend only, in four bands of net monthly household income (under 1,000, 1,000-1,999,
  2,000-2,999, 3,000 euros or more), not spend by category, so the same ratio applies to every
  category and is flagged as an estimate. It likely understates how fast discretionary spend
  (restaurants, personal care) rises with income; category-specific elasticities from the
  national spend-quintile table (73828) are a later refinement.
  Visitor-only categories (flag in engine/categories, e.g. guest_house) use the visitor term
  only.

Supply (category-specific shares):
  supply(A, K) = companies(R, CNAE(K)) × OSM_K(A) / OSM_K(R)
  where OSM_K(x) is the count of OSM places of category K in x. If OSM_K(R) = 0, the share is
  A's population share of R. Low = min(base, max(OSM_K(A), base × 0.8)).
  High = base × 1.25 (coverage is already in base; OV2). Where K has no CNAE
  yardstick, supply = OSM_K(A) / national coverage of the proxy sector named in the
  engine/categories column `coverage_proxy`, flagged low confidence.

Opportunity:
  Areas below min_population(K) (column in engine/categories, e.g. 5,000 for gyms) are "too
  small to score" (CM2).
  density(A, K) = market_eur(A, K) / (supply(A, K) + k), with k = 1 pseudo-business.
  score(A, K) = density(A, K) / median base density over peer areas (the median uses base
  values only; the area's low, base and high are each divided by it).
  Peers: same level, same log-population band (2x wide) or the 50 nearest areas by population,
  any region (OV2).
  A score of 1.0 means "as much market per business as similar areas".
  Rankings use the low end of the score, not the base (CM2).
  Mapping completeness(A): OSM places per resident for well-covered categories (pharmacy, bar)
  against peer areas. Low completeness caps confidence and is shown on the map, because a
  badly mapped town otherwise looks under-supplied (CM2).

Atlas categories (20): restaurant (incl. fast food), cafe, bar, bakery, butcher, greengrocer,
supermarket, pharmacy, hairdresser, beauty_salon, dentist, physiotherapy, veterinary, gym,
childcare, private_school, clothes, optician, estate_agent, guest_house. The table mapping each
to its ECOICOP codes, CNAE code and tourism flag is the first file written (engine/categories).

## Catchments
- Radius catchments use exact tract geometry (1.3B, confirmed in CM4).
- Walking-time catchments are in scope (TODO 2, built now). The method is chosen in the
  engineering review: an in-process route search on a street graph limited to the catchment
  area, or a self-hosted router. A routing server needs several GB of RAM, beyond the free
  tier; any option that costs money is put to the owner first.
- Computed per tract with each tract's own region R (EPF, DIRCE), then summed, so circles that
  cross a regional border are handled.
- Area share assumes residents are spread evenly within a tract; catchments where most of the
  circle falls in large, sparsely built tracts get a scope warning.
- Early check: measure snapshot size with simplified tract polygons and the memory of the
  shapely STRtree; update the README download size.

## Geographic edge cases
- Ceuta and Melilla: in DIRCE and EPF; ADRH coverage checked; no OSM sub-areas below
  municipality if tracts are sparse.
- Canary Islands: separate OSM extract (done), CPI and VAT differ (IGIC); price_band uses the
  Canary CPI series.
- Basque Country and Navarra: ADRH income comes from the State tax agency only and is partial
  there, at every level. Areas are marked "income partial" and capped at medium confidence.
  Eustat and Nastat income series are a later TODO.
- Tracts with suppressed ADRH values: use the district value with an estimate check.

## Validation: pre-registered forward test
Replaces the OSM backtest (CM1, 1.4), after the spike below showed it cannot work.
- Each method version's scores are frozen and published (atlas CSV with a SHA-256 and date) the
  moment the method version is released. This is the registered prediction.
- Outcome: INE's business demography statistics (CODEM, operation 426), which come from the
  business register, not OSM: openings ("altas") by province and CNAE division each quarter
  (table 82131, e.g. food and drink, division 56), and opening and closure rates for all 8,133
  municipalities (table 82132, all sectors). The series starts in 2026 Q1.
- Metric: openings per 1,000 existing units in the top vs bottom score quintile of provinces
  (sector level) and municipalities (all sectors), with bootstrap 95% intervals, against a
  population-only baseline. First read after four quarters of data following registration
  (about mid-2027), then every quarter.
- The README states the registration date and shows results only when there are some; until
  then it says "prediction registered, not yet validated".
- OSM appearances between snapshots are reported only as mapping activity, never as openings.

### Spike results (2026-10-16)
- OSM changes do not measure openings. Spain's mapped places grew from 447,398 (2025-01-01) to
  535,657 (2026-10-15), +20% in 21 months, against roughly 1-2% a year real business growth.
- In Valladolid, Sevilla and Zaragoza, 1,869 places appeared. About 80% are new map objects with
  no date and 17% are retagged objects; fewer than 2% carry evidence either way (a former vacant
  unit, or a start date). The planned placebo (places dated before the snapshot) had 4 members.
- EPF (2025 data published) has the 4-digit codes the categories need (11.1.1 restaurants and
  cafés, 13.1.2 hairdressing and beauty, 06.2.1 dental, 09.4.1 sport and recreation, 03.1.1
  clothes, 01.1.1 bread) by region; coefficients of variation only at 3-digit level (e.g. 5.3%
  for restaurants in Castilla y León); table 74002 has total spend by income band only.

## Atlas and hero export
- Published at municipality and district level. Tract scores only on demand, low confidence.
- Release assets: atlas.csv (one row per area × category, with every input and its vintage)
  and per-category JSON for the map page.
- hero_spain.json: municipalities over 20,000 residents (about 400), metric = calibrated
  restaurant supply per 1,000 residents (not raw mapped counts), one insight each, under 300 KB.
- Map page hosted on GitHub Pages from the repo, with ODbL and INE attribution.

## Scope Decisions

| # | Proposal | Effort (human / CC) | Decision | Reasoning |
|---|----------|--------|----------|-----------|
| 1 | Household spending (EPF), market size in euros | ~3 days / ~1-2 h | ACCEPTED | "How big is the market" means euros |
| 2 | Validate the score against later openings (now a pre-registered forward test, see Validation) | ~1 week / ~2-3 h | ACCEPTED | Measured hit rate is the strongest credibility signal |
| 3 | Open Opportunity Atlas: release + public map page; hero JSON as a slice | ~1 week / ~2-3 h | ACCEPTED | One click to see the product; feeds djcrafted |
| 4 | Point or address catchments (radius) | ~1 week / ~2-3 h | ACCEPTED | Business owners think in walking distance |
| 5 | Visitor demand from INE tourism statistics | ~4 days / ~1-2 h | ACCEPTED | Tourist cities are badly underestimated otherwise |
| 6 | Polish bundle: natural names, peer-city benchmarks, chain share, verify links, plain confidence lines | ~3 days / ~1 h | ACCEPTED | Small touches that make answers feel finished |
| - | price_band, chart (existing roadmap, issues #4 and #5) | - | IN PLAN | Part of the original brief |
| - | datos.gob.es, rents, crime (issues #3 and #6) | - | AFTER HOSTING | Commoditised or not needed for the engine |

## Accepted Scope (added to this plan)
- engine/ (pure): categories table, demand, supply calibration, opportunity score, ranges.
- Data assembly: EPF (73993, 74002, 74005), tourism (EOH 75197/75198, tourist flats 39363,
  EGATUR), geocoder (CartoCiudad), tract geometry in the snapshot.
- Tools: market_size, compare_areas, price_band, chart; catchment parameters on demographics,
  income, businesses and market_size.
- Atlas build, release, map page, hero export.
- Registration of frozen scores, quarterly scoring script against CODEM, README results.
- Golden-set evals (30 to 50 questions), written first.
- Polish bundle (5 items).

## Deferred (tracked as GitHub issues)
- datos.gob.es find_datasets and read_dataset (issue #3), after hosting.
- Rental reference index and crime by municipality (issue #6), after hosting.
- Eustat and Nastat income for the Basque Country and Navarra (new issue).
- Tract-level atlas publication (new issue).

## Outside voice decisions
- CM1 A: backtest placebo, net lift reported. Superseded by the pre-registered forward test after the spike.
- CM2 A: completeness flag, minimum population per category, rank by low end.
- CM3 B: keep expansion order (hosting, evals and GIF at step 7).
- CM4 B: keep exact geometry and addresses.
- CM5 A: regional cuisine share only, overridable.
- CM6 A: bundle yearly INE inputs into the snapshot release.
- CM7 B: full evals local; CI free checks (plus monthly model subset per TODO 4).

## Sequencing
1. Merge PR #12, publish the first OSM snapshot (done 2026-10-16).
2. Issue #1: ADRH 2024 rebuild (data due 2026-10-19).
3. Golden-set questions written; backtest spike (2025-01-01 snapshot vs today, openings in 3
   cities); engine/categories table; EPF; engine; market_size; compare_areas.
4. Tourism demand, tract geometry and catchments, polish bundle.
5. Charts, atlas build, hero export, map page.
6. Register the first frozen scores; quarterly scoring against CODEM from then on.
7. Hosting (Cloud Run), registry, directory submission, demo GIF.
8. datos.gob.es, rents, crime.

## Data bundling (CM6)
Yearly INE inputs (ADRH, EPF, DIRCE, tourism, Padrón) are bundled into the monthly snapshot
release with their vintages, so answers make no live INE calls. Live INE remains a fallback and
serves tables that are not bundled. The data-status resource (8.1) reads vintages from the
bundle. EPF tables 73993, 74002 and 74005 are checked for the needed breakdowns before the
engine spec is final. Step 3 runs on ADRH 2023 if the 2024 release slips.

## Evals (CM7 + TODO 4)
Full runs locally with the owner's key. CI runs free tool-level checks on every push, and a
5-question model eval monthly with an Anthropic API key stored as an Actions secret and a
spending cap set by the owner in the Anthropic console (set up during the eval work; the key
and cap are requested at that point).

## Latency budget (target 20 s for a market_size answer, warm cache)
Place resolution < 50 ms; INE inputs from cache < 200 ms (cold: up to 10 s per table, the
answer says so); snapshot queries < 100 ms; geometry for a catchment < 300 ms; chart < 1 s.

## Engineering review decisions (2026-10-16)
- E1.1 A: bundle/ module (store, schema, build_bundle.py) holds OSM, INE and geometry sections;
  release data-bundle-v2; osm-snapshot-v1 stays frozen for old installs.
- E1.2 (superseded by OV1): raw-reply cache seeding does not work; the cache expires entries
  after 7 days (core/http.py:113) and keys include exact filters.
- E1.3 A: engine, geometry, bundle queries and charts run via asyncio.to_thread; one SQLite
  connection per thread; charts use matplotlib's Figure API with the Agg backend, never pyplot;
  test with 10 parallel calls.
- E1.4 A: the atlas workflow deploys per-category JSON with the page through GitHub Pages
  (release downloads have no CORS header, verified); the release keeps the full CSV.
- E1.5 A: walking catchments from per-province pedestrian graphs built monthly in CI and fetched
  on first need; shortest-route search on the streets near the point; fallback circle of
  speed x time / 1.3 labelled "estimate". Spike shard size and route time first.
- E2.1 A: one category registry (top-level categories module) with optional engine fields
  (ecoicop, tourism, visitor_only, min_population, coverage_proxy); a test checks every atlas
  category is complete.
- E2.2 A: Estimate dataclass (low, base, high, unit, note) with interval +, x, / implemented once
  and an ordering check; property tests target it.
- E3.1 A: golden set in two layers: required tool calls + hand-checked facts checked in CI
  against the frozen bundle (no model); answer rubric scored in local and monthly model runs.
- E3.2 A: Playwright smoke test of the map page in CI and after each monthly deploy.
- E3 regression (mandatory): existing income, demographics and businesses tests pass unchanged
  through the areas, bundle and registry refactors; a v1-schema install keeps reading
  osm-snapshot-v1.
- E4.1 A: bundle split into core (OSM + INE) and geometry parts, each fetched on first need;
  `spain-open-data-mcp prefetch` command in the install docs; a slow first download returns a
  "downloading data, try again in a minute" message instead of timing out.
- E4.2 A: shapely, numpy and matplotlib imported lazily; a test asserts importing the server does
  not import them; startup time logged.

## Engineering outside voice decisions
- OV1 A: each INE table bundled once, normalised (all areas and years, with vintage); IneData reads
  the bundle first and goes live only for what is missing; year-pinning in the data-assembly
  layer for the backtest.
- OV2 A: supply high = base x 1.25 (coverage is already in base); engine test that low and high
  respond to coverage alike in every region; peers = same log-population band (2x wide) or the
  50 nearest areas by population.
- OV3 A: categories tagged local or destination; destination categories scored at municipality
  level and up, district views labelled as residents' spending; away-from-home share removed
  from visitor demand so domestic tourists count once.
- OV4 A (amended after the spike): the atlas release requires the current method version's
  scores to be registered (frozen, dated, checksummed); until validation results exist the page
  shows "prediction registered, not yet validated" and no hero export is produced.
- OV5 A: hosted image bakes in core and geometry bundles and walking graphs for the 10 largest
  provinces; fetch-on-need for local installs only; image size, memory and cold start measured at
  step 4.
- OV6 A: tract shapes bundled for the income atlas's own year as well as 2025; build fails under
  99% join rate; unmatched codes fall back to the district with a visible check.
- OV7 A: street-network walking catchments kept (owner decision).
- Not raised: DIRCE counts companies at head office. Chain-dominated categories already have
  no CNAE yardstick, and DIRCE premises by activity are not published in Tempus.

## Method 1.0 as built (2026-10-16)
Decisions made while implementing, against live data:
- Supply without a DIRCE yardstick (supermarkets, pharmacies, opticians, clothes, home, gyms,
  childcare) is the mapped count with a high end of x1.5, flagged as a lower bound. The
  planned coverage proxy (another sector's OSM-vs-DIRCE coverage) was dropped: it turned
  1,857 mapped Madrid pharmacies into 15,298 (about 2,900 exist; OSM maps about 18,300 of
  Spain's roughly 22,000 pharmacies) and 35 Valladolid gyms into 427.
- The score's reference is the area's region (autonomous community): market per business in
  the area over market per business in the region. Log-population peer bands (OV2) need
  national per-municipality inputs and come with the atlas, which computes them anyway.
- In rankings, areas whose map looks thin (completeness under 0.5) are ranked after all
  others and labelled, instead of sinking the confidence of the whole ranking.
- Regional EPF ratios are computed year by year over the codes both the region and Spain
  publish, so one unpublished code no longer discards a region's year.
- Categories sharing spending codes (restaurant, cafe, bar, fast food on 11.1.1; hairdresser
  and beauty on 13.1.2) split the pool by national share of mapped places.

## Implementation Tasks
- [x] **T1 (P1, human ~2h / CC ~10min)** osm-snapshot: integrity check on open, one re-download, truncated-file test (2.1). Done in PR #12.
- [x] **T2 (P1, human ~2h / CC ~15min)** businesses: sanitise upstream text, brands need 3+ places, malicious-brand test (3.2). Done in PR #12.
- [x] **T3 (P1, human ~2h / CC ~10min)** osm-snapshot: release tag osm-snapshot-v1 (9.1). Done in PR #12.
- [x] **T4 (P2, human ~4h / CC ~20min)** areas module, split businesses() (5.1).
- [x] **T5 (P2, human ~4h / CC ~30min)** verify EPF 73993/74002/74005 (done; findings in Validation > Spike results). engine/categories table still to do.
- [x] **T6 (P2, human ~4h / CC ~1h)** backtest spike in 3 cities (done; OSM backtest dropped, see Validation).
- [x] **T7 (P2, human ~1w / CC ~3h)** pure engine: ranges, completeness flag, minimum population, method version, property tests (1.1, CM2, 6.1, 10.1).
- [ ] **T8 (P2, human ~3d / CC ~2h)** bundle yearly INE inputs into the snapshot release (CM6).
- [x] **T9 (P2, human ~1d / CC ~30min)** data-status resource, job failure auto-issues, structured call logs (8.1).
