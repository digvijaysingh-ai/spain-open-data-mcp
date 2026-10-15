# Data sources and licences

The code of spain-open-data-mcp is MIT-licensed. The data it returns keeps the licence of its source.

| Source | Used for | Licence | Attribution |
|---|---|---|---|
| INE, Censo anual de población (table 65034) | Population by sex and age | CC BY 4.0 | Fuente: INE |
| INE, Cifras oficiales del Padrón (table 29005) | Official municipal population | CC BY 4.0 | Fuente: INE |
| INE, Atlas de Distribución de Renta de los Hogares (ADRH) | Income, inequality, income sources, household profile, down to census tract | CC BY 4.0 | Fuente: INE |
| INE municipality register | Place names and codes | CC BY 4.0 | Fuente: INE |

INE requires the citation "Fuente: INE" or "Elaboración propia con datos extraídos del sitio web del
INE: www.ine.es", and that reuse does not suggest INE endorses it. See the
[INE legal notice](https://www.ine.es/dyngs/AYU/index.htm?cid=125).

## How figures are checked

- INE withholds values for small areas (statistical secrecy). These are reported as suppressed, never as zero.
- INE caps the highest incomes in its income atlas. When several areas share the exact top value, the result says so.
- Where two INE sources measure the same thing (census vs Padrón population), both are shown with the gap.
- Each result states its data year and when it was retrieved, and a confidence level no higher than its weakest input.

## Personal data

This server returns aggregate statistics only. It does not hold or return data about identifiable people.
