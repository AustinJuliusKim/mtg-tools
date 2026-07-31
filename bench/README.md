# CSV ingestion benchmark: Python (server) vs JS (Node + real browser)

Tests the hypothesis "Python is better at parsing/ingesting large CSVs than
TypeScript" against this app's *actual* workload: ManaBox CSV → typed rows
(exact money, foil/finish, identity) → merge duplicates → summary totals.

```bash
bash bench/run_all.sh     # generates data, runs every lane, prints the table
```

## Lanes

| Lane | What it is |
|---|---|
| `python-app` | The repo's real code: `binders.io.parse_row` → `aggregate.merge` → `summarize`, on the repo venv |
| `python-fast` | Same semantics, integer cents + dicts instead of Decimal + dataclasses — isolates the Decimal/dataclass cost |
| `python-stage` | Python-only context: the full production staging path (`webapp.importer.stage_import` into `:memory:` SQLite, incl. 3× `json.dumps`/row) |
| `node` | Faithful JS port (`js/parse.mjs`): PapaParse + integer cents, Node per `.nvmrc` |
| `browser` | The same `parse.mjs` in headless Chromium (Playwright's), timed in-page with `performance.now()` |

## Fairness rules

- JS uses a real CSV parser — ~25% of rows have quoted commas, so `split(',')`
  would be wrong and unfairly fast.
- JS money is integer cents, never IEEE floats (the repo's own money rule).
  Datasets are two-decimal, so cents are exact and lanes can agree bit-for-bit.
- Every lane emits an oracle — `(rows, quantity, valueCents)` before and after
  merge — and `assemble.py` **fails** on any cross-lane mismatch: a fast lane
  that parsed a different reality is a bug, not a result.
- Same protocol everywhere: 2 warmups, 7 timed in-process runs, per-stage
  medians. The browser lane also reports its cold first run.

## Datasets

Synthetic (deterministic, `generate.py`): `synth-20k`, `synth-100k` singles in
the legacy ManaBox dialect with word-dialect Foil and ~30% duplicate
identities; `sealed-20k` in the sealed template dialect. If the real Desktop
exports exist (`~/Desktop/Binders*.csv`), a `real-922` pass runs too.
`bench/data/` is gitignored — real collection data never enters the repo.

Results from this machine live in [RESULTS.md](RESULTS.md).
