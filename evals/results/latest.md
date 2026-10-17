# Golden set results: claude-sonnet-5-5, 2026-10-17

38 questions. 95% Wilson intervals.

| Metric | Rate | 95% interval | n |
|---|---|---|---|
| fact accuracy | 100% | 87% to 100% | 26 |
| rubric items met | 96% | 90% to 99% | 84 |
| sourcing items met | 100% | 90% to 100% | 35 |
| caveat items met | 100% | 81% to 100% | 16 |
| refusal items met | 100% | 70% to 100% | 9 |

Facts rescored after a checker fix (a decimal such as 3.6 was read as 36); the answers and rubric grades are from the run.

Missed rubric items:
- `income-08019`: Gives both figures in euros per year.
- `resolve-typo`: Notes that the name was read as Valladolid.
- `market-restaurants-valladolid`: States the method version.
