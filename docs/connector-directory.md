# Claude connectors directory: submission pack

Everything the directory form asks for, in one place. Submit once the hosted server is
deployed (see [deploy.md](deploy.md)) and its URL is filled in below.

## Listing

- **Name:** Spain Open Data
- **Server URL:** `https://spain-open-data-mcp-165703905656.europe-west1.run.app/mcp` (Streamable HTTP, stateless)
- **Authentication:** none. All data is public; there are no accounts.
- **Short description (one line):** Market sizing, prices, rents, crime and locations in Spain
  from INE and other public data, each figure sourced and dated.
- **Long description:** Answers business questions about any place in Spain, from regions down
  to census tracts, using official public data: INE (population, households, income, household
  spending, consumer prices, rents, tourism, company counts), OpenStreetMap (businesses and
  chains), the Interior ministry (recorded crime) and the national open-data catalogue
  (datos.gob.es). It sizes markets in euros with ranges, ranks areas by market per competing business (an opportunity score, not yet
  validated as a forecast) for a
  business, profiles the walking catchment of an address, and suggests local price ranges.
  Every result lists its sources, data year and licence, the checks it ran (suppressed values,
  small samples, stale data, estimates), and a confidence level.
- **Categories:** Data and analytics; Business research.
- **Languages:** answers in the user's language; place names in Spanish and co-official forms
  (Alacant/Alicante, Donostia) and English (Seville, Catalonia).
- **Privacy policy:** https://github.com/digvijaysingh-ai/spain-open-data-mcp/blob/main/docs/privacy.md
- **Support:** https://github.com/digvijaysingh-ai/spain-open-data-mcp/issues
- **Documentation:** https://github.com/digvijaysingh-ai/spain-open-data-mcp#readme
- **Licence:** MIT (code); data keeps its source licence, listed in the `data-licences` resource.

## Tools

All tools are read-only (`readOnlyHint: true`, `destructiveHint: false`), have a title, and
return structured output with a schema.

| Tool | Title | What it does | Data |
|---|---|---|---|
| `resolve_place` | Resolve a Spanish place | INE code and level for a place or neighbourhood name | local |
| `demographics` | Population and households | Population by sex and age, trend, household profile | INE |
| `income` | Household income | Income per person and household, inequality, benchmarks, down to census tract | INE |
| `businesses` | Businesses and competitors | Businesses of a kind per 10,000 residents and per km², chains, coverage check | OpenStreetMap snapshot, INE |
| `market_size` | Market size in euros | Yearly spending pool for a category, with a range, competitors and an opportunity score | INE, OpenStreetMap |
| `compare_areas` | Compare areas for a business | Areas ranked by market per competing business, cautious end first | INE, OpenStreetMap |
| `catchment` | Catchment around an address | Residents, income, age and businesses within a radius or walking time | INE, CartoCiudad, OpenStreetMap |
| `price_band` | Price range for a place | Price range from local purchasing power and inflation (labelled an estimate) | INE |
| `chart` | Chart | PNG chart of a result, with sources on the image | INE, OpenStreetMap |
| `rents` | Rents | Change in rents (INE index) against province, region and Spain | INE |
| `crime` | Crime | Recorded offences by type per 1,000 residents, with benchmarks | Interior ministry (bundled) |
| `find_datasets` | Find open datasets | Search the national open-data catalogue | datos.gob.es |
| `read_dataset` | Read an open dataset | Columns and sample rows of a catalogue dataset, personal-data columns withheld | datos.gob.es, publishers |

Prompts: `size_a_market`, `pick_a_location`, `explore_public_datasets`.
Resources: `spain-open-data://status`, `spain-open-data://data-licences`.

## Example prompts for reviewers

1. "I want to open a specialty coffee shop in Valencia. Which districts look most promising,
   and how big is the market?"
2. "What is the yearly market for gyms in Valladolid, and how many competitors are there?"
3. "Profile a 10-minute walk around Plaza Mayor, Salamanca: residents, income, and bakeries."
4. "How have rents and recorded crime changed in Málaga compared with the rest of Andalucía?"
5. "Find open datasets about tourist apartments in the Canary Islands and show me what one
   contains."

Expected behaviour: each answer gives ranges and assumptions, and ends with sources and
caveats. A place under 20,000 residents gets province-level crime figures, with a note.
Questions about specific people are declined; the server holds aggregate data only.

## Safety and data handling

- No user data is stored; tool arguments are never logged (see the privacy policy).
- Addresses go only to Spain's official geocoder (CartoCiudad), with floor and door removed.
  Nominatim is a fallback when CartoCiudad finds nothing.
- `read_dataset` fetches only download links listed in the catalogue, never a URL from the
  model, and refuses private network addresses on every redirect.
- Host header checks (DNS rebinding protection) and per-address rate limits, with a higher
  limit for Claude's shared egress range (160.79.104.0/21).

## Before submitting

- [x] Deploy and fill in the server URL above and in `server.json` (`remotes`).
- [x] Add the URL as a custom connector in Claude and run real prompts (pharmacy, barber, Irish bar in Valladolid, 2026-10-17; gym chain in Madrid, 2026-10-18).
- [x] Demo for the README (a screenshot of a real conversation, docs/img/gym-madrid.png).
- [x] Tag a release so PyPI and the MCP Registry listing match the hosted version.
