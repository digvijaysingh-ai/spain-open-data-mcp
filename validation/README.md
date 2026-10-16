# Forward test of the opportunity score

Each atlas build registers its scores: method version, date and the SHA-256 of `atlas.csv`,
attached to the [atlas release](https://github.com/digvijaysingh-ai/spain-open-data-mcp/releases/tag/atlas).
`scripts/score_forward_test.py` scores a registration against INE's business demography
statistics (CODEM, table 82131): for each category, do the provinces the atlas ranked in its
top fifth see more new businesses per existing business, in the quarters after registration,
than those in its bottom fifth? The same is computed for a ranking by population alone, the
baseline the score has to beat. Results land in `results/`.

The first registration is opportunity-1.1 (2026-10-16). INE publishes each quarter about four
months after it ends, so the first quarter after registration (2027 Q1) is expected in
mid-2027. Until then the honest result is "awaiting data".

## Pipeline check (dry run, 2026-10-16)

`--dry-run` scores the registered atlas against the quarters already published (2026 Q1 and
Q2), which come before registration, so it tests the pipeline and is not evidence. On that
run ([results](results/opportunity-1.1-2026-10-16-dry-run.md)) the opportunity ranking's lift
is close to 1 in most categories (0.89 to 1.30; above 1 with its interval clear of 1 for none,
touching 1 for gyms and greengrocers), and a ranking by population alone does better in 11 of
19 categories. Two limits of this check: provinces are a coarse unit for a score built for
towns and districts, and CODEM's CNAE divisions mix categories (division 47 is all retail,
so pharmacies, bakeries and clothes shops share one outcome). If the forward test shows the
same in 2027, the score's ranking should not be presented as predictive of where businesses
open.
