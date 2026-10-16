"""MCP prompts: ready-made workflows that chain the tools for common questions.

Each prompt returns instructions for the assistant: which tools to call, in what order, and
how to present the answer (ranges, assumptions, a closing 'Sources and caveats' block).
"""

from __future__ import annotations

ANSWER_RULES = """
How to answer:
- Give ranges (low to high) wherever the tools return them, not only the base value.
- State the key assumptions and which ones the user could change.
- Never state a conclusion more strongly than the lowest confidence among the results used.
- End with a short "Sources and caveats" block built from the tools' sources and checks:
  dataset, data year, and the caveats that matter for the decision.
- Do not identify or profile individuals; this server holds aggregate data only.
"""


def size_a_market(business: str, place: str) -> str:
    return f"""Size the market for {business} in {place}, Spain.

Steps:
1. resolve_place("{place}") to confirm the place and its INE code.
2. market_size(place, category="{business}"): yearly spending pool in euros, competing
   businesses, market per business and the opportunity score against the region.
3. demographics(place) and income(place): who lives there and what they earn, to explain the
   market figure (households, age, income per household).
4. businesses(place, category="{business}"): mapped competitors, chains, and how the place
   compares with similar-sized municipalities.
5. If the user gives a price, price_band(place, reference_price, category) to suggest a local
   range.

Present: the market in euros (range), the number of competitors, market per business, how
that compares with the region and with similar towns, and what would change the picture.
{ANSWER_RULES}"""


def pick_a_location(business: str, area: str, near: str | None = None) -> str:
    near_step = (
        f'\n4. catchment(near="{near}", walk_minutes=10, category="{business}") for the specific '
        "address the user has in mind, and compare it with the ranking."
        if near
        else ""
    )
    return f"""Recommend where to open {business} in {area}, Spain.

Steps:
1. resolve_place("{area}"). A municipality: compare its districts. A province or region:
   compare its municipalities.
2. compare_areas(within=area, category="{business}"): areas ranked by the cautious (low) end
   of the opportunity score. Note areas ranked last for thin mapping.
3. For the top three: income(place) and demographics(place) to describe who lives there, and
   businesses(place, category) for the competitors already there.{near_step}

Present: a short list of recommended areas with the reason for each (market per business,
competitors, residents and income), the main risk for each, and what to check on the ground
(rents, footfall, local regulation) that public data cannot show.
{ANSWER_RULES}"""


def explore_public_datasets(topic: str, place: str | None = None) -> str:
    where = f" for {place}" if place else ""
    return f"""Find and summarise open public datasets about {topic}{where} in Spain.

Steps:
1. find_datasets(query="{topic}"{', place="' + place + '"' if place else ""}): datasets from the
   national catalogue (datos.gob.es), with publisher, licence, formats and last update.
2. For the two or three most relevant, read_dataset(dataset_id) to see the columns and a
   sample of rows (personal data columns are withheld).
3. Say what each dataset can and cannot answer, how fresh it is, and under which licence it
   may be reused.
{ANSWER_RULES}"""
