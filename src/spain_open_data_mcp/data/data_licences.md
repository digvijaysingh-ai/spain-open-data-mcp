# Data sources and licences

The code of spain-open-data-mcp is MIT-licensed. The data it returns keeps the licence of its source.

| Source | Used for | Licence | Attribution |
|---|---|---|---|
| INE, Censo anual de población (table 65034) | Population by sex and age | CC BY 4.0 | Fuente: INE |
| INE, Censo anual de población, bulk file by census tract (table 65034) | Age bands and sex for districts and tracts | CC BY 4.0 | Fuente: INE |
| INE, Cifras oficiales del Padrón (table 29005) | Official municipal population | CC BY 4.0 | Fuente: INE |
| INE, Atlas de Distribución de Renta de los Hogares (ADRH) | Income, inequality, income sources, household profile, down to census tract | CC BY 4.0 | Fuente: INE |
| INE municipality register | Place names and codes | CC BY 4.0 | Fuente: INE |
| INE, Directorio Central de Empresas (DIRCE, table 73020) | Companies by region and activity, to check OpenStreetMap coverage | CC BY 4.0 | Fuente: INE |
| INE, census tract boundaries (seccionado) | Placing businesses in tracts, area in km² | CC BY 4.0 | Fuente: INE |
| OpenStreetMap, via Geofabrik extracts | Business counts and brands | ODbL 1.0 | © OpenStreetMap contributors |
| OpenStreetMap place names (neighbourhoods, quarters, suburbs, villages, sub-municipal boundaries) | Naming INE districts and tracts; finding a district from a neighbourhood name | ODbL 1.0 | © OpenStreetMap contributors |
| INE, Índice de Precios de Vivienda en Alquiler (IPVA, tables 59056, 59058, 59060, 59061) | Rent index by region, province, municipality and district | CC BY 4.0 | Fuente: INE |
| Ministerio del Interior, Balance de Criminalidad (Portal Estadístico de Criminalidad) | Recorded crime by municipality (over 20,000 residents), province and region | Public sector information, reused under Ley 37/2007: cite the source, do not distort the data, state the date | Fuente: Ministerio del Interior, Balance de Criminalidad |
| datos.gob.es catalogue and the datasets it lists | find_datasets, read_dataset | Each dataset's own licence (often CC BY 4.0), shown with every result | The publisher named in the result, and datos.gob.es |

INE requires the citation "Fuente: INE" or "Elaboración propia con datos extraídos del sitio web del
INE: www.ine.es", and that reuse does not suggest INE endorses it. See the
[INE legal notice](https://www.ine.es/dyngs/AYU/index.htm?cid=125).

OpenStreetMap data is © OpenStreetMap contributors and available under the
[Open Database Licence](https://www.openstreetmap.org/copyright). The monthly snapshot this
server builds is a derived database under the same licence; it is published with each
[snapshot release](https://github.com/digvijaysingh-ai/spain-open-data-mcp/releases/tag/osm-snapshot-v1).
The neighbourhood names file (`area_names.json.gz`, built by `scripts/build_area_names.py`) is
also a derived database under the ODbL, shipped inside the package.

## How figures are checked

- INE withholds values for small areas (statistical secrecy). These are reported as suppressed, never as zero.
- INE caps the highest incomes in its income atlas. When several areas share the exact top value, the result says so.
- Where two INE sources measure the same thing (census vs Padrón population), both are shown with the gap.
- OpenStreetMap counts are lower bounds. Where DIRCE counts the same activity, each result compares the two for the region and lowers its confidence when the map looks incomplete.
- Each result states its data year and when it was retrieved, and a confidence level no higher than its weakest input.

## Personal data

This server returns aggregate statistics only. It does not hold or return data about identifiable people.
The OpenStreetMap snapshot keeps each place's tags, brand and location, never its name, phone number or
address, and the tools return counts and brands rather than lists of premises.

read_dataset shows rows of public datasets as their publishers published them. Columns that look
like personal data (surnames, ID numbers, emails, phone numbers, dates of birth, bank accounts,
number plates, home addresses) are withheld by name or by their values, and the result lists
what was withheld.
