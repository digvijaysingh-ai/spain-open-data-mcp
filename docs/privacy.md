# Privacy policy

Spain Open Data (spain-open-data-mcp) is an open-source MCP server that answers questions about
Spanish places from public statistics. This policy covers the hosted server and the atlas site.
If you run the server yourself, nothing reaches us at all.

Last updated: 2026-10-16.

## What the server receives

Your AI assistant sends the server tool calls: a place name, a business category, and sometimes
a street address (for the `catchment` tool) or a search term (for `find_datasets`). The server
does not receive your conversation, your name, your account or any identifier of you.

## What it keeps

- **Logs:** one line per tool call with the tool's name, whether it worked, and how long it
  took. Tool arguments (places, addresses, search terms) are never logged. On an unexpected
  error, the error type and the location in the code are logged, not the error message.
- **Platform logs:** the hosting platform (Google Cloud Run, EU region) records each HTTP
  request's time, size, status, IP address and user agent. Google keeps these for 30 days.
- **Rate limiting:** to share capacity fairly, each server instance counts recent requests per
  IP address in memory. The count is not written anywhere and disappears when the instance
  stops (usually within minutes of inactivity).
- **Cache:** answers from public data services (INE, datos.gob.es) are
  cached to avoid repeat requests. Addresses are never cached. Catalogue searches are cached
  for a day, so a search term stays in that cache, unlinked to anyone, until it expires or is
  pushed out.

There are no accounts, cookies, analytics or advertising, and no data is sold or shared.

## What it sends to other services

To answer, the server fetches public data. Some requests carry part of what you asked:

| Service | When | What is sent |
|---|---|---|
| INE (Instituto Nacional de Estadística) | most tools | Table and area codes, never addresses |
| CartoCiudad (Instituto Geográfico Nacional) | `catchment` with an address | The street, number and municipality. Floor and door are removed first |
| Nominatim (OpenStreetMap Foundation) | `catchment`, only if CartoCiudad finds no match | The same street, number and municipality |
| datos.gob.es and dataset publishers | `find_datasets`, `read_dataset` | The search term, or the dataset's own download link |

Coordinates derived from an address are rounded to about 100 metres before they are used. Each
service has its own privacy policy:
[INE](https://www.ine.es/dyngs/AYU/index.htm?cid=126),
[CartoCiudad / IGN](https://www.ign.es/web/ign/portal/politica-privacidad),
[OpenStreetMap Foundation](https://osmfoundation.org/wiki/Privacy_Policy),
[datos.gob.es](https://datos.gob.es/es/aviso-legal).

## Data about other people

The server returns aggregate statistics only. It does not return data about identifiable
people: business counts come without names or phone numbers, and when it reads a public
dataset it withholds columns that look like personal data (names with surnames, ID numbers,
emails, phone numbers, dates of birth, bank accounts, number plates, home addresses).

## Your rights

Because the server keeps nothing that identifies you, there is normally nothing to access or
delete. If you think it holds data about you, or have a question about this policy, open an
issue at https://github.com/digvijaysingh-ai/spain-open-data-mcp/issues (do not include personal
data in it). You can also complain to the Spanish data protection authority
([AEPD](https://www.aepd.es)).

## Changes

Changes to this policy are made in the public repository, so every version and its date are
visible in the file's history.
