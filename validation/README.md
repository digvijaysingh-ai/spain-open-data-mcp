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
