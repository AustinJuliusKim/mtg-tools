# mtg-tools

Python utilities for ManaBox binder CSV exports — parse, merge, filter, tier
and export a Magic collection.

Built for the MTG Sell/Reinvest project tracked in
`ObsidianVault/30-projects/Paternity Leave Project Plan.md`, whose tier and
multi-copy tables this package regenerates from live data.

Stdlib only. No install, no virtualenv, works on the system Python 3.9.

```bash
cd ~/personal/mtg-tools
python3 -m binders summary ~/Desktop/Binders.csv ~/Desktop/Binders2.csv
```

## Why merging matters

Each binder is scanned separately, so a card owned in thirteen copies across
two binders looks like an unremarkable ×3 and ×10 until the exports are
collapsed onto one identity. Every command merges by default (`--no-merge` to
opt out).

```
$ python3 -m binders dupes ~/Desktop/Binders.csv ~/Desktop/Binders2.csv --min-qty 4
Card                    Set    Rarity    Qty  Each    Total    Source
----------------------  -----  --------  ---  ------  -------  -----------------
Mox Amber               DOM    mythic    x13  $75.17  $977.21  Binders2
Holistic Wisdom (foil)  ODY    rare      x4   $46.96  $187.84  Binders2
Doubling Season (foil)  FDN    mythic    x4   $36.04  $144.16  Binders2
...
```

## Commands

| Command | What it does |
|---|---|
| `summary` | Counts, total value, breakdowns by rarity, source and set |
| `tiers` | Price bands with cash/credit buylist estimates |
| `dupes` | Cards held in multiple copies, richest stack first |
| `high-value` | Cards worth a condition check before shipping |
| `top` | Highest-value positions |
| `filter` | Query by price, rarity, set, name, language, foil |
| `diff` | Compare two scans: added, removed, quantity and price changes |
| `merge` | Combine exports into one deduplicated CSV |
| `buylist` | Vendor submission list with per-card estimates |
| `ledger` | The tracking ledger with tax and insurance columns |
| `validate` | Flag rows that need a human look |

Add `--markdown` to `summary`, `tiers`, `dupes` or `top` to get a pipe table
that pastes straight into the vault.

```bash
# Regenerate the vault's CK Buylist Estimates table
python3 -m binders tiers ~/Desktop/Binders*.csv --markdown

# What did the manual prune actually remove?
python3 -m binders diff ~/Desktop/Binders.csv.bak ~/Desktop/Binders.csv

# Everything over $20, foils only
python3 -m binders filter ~/Desktop/Binders*.csv --price-min 20 --foil

# Build the submission list
python3 -m binders buylist ~/Desktop/Binders*.csv -o buylist.csv --min-price 1
```

## Library

```python
from binders import load_many, merge, price_tiers, multi_copies, where

collection = merge(load_many("Binders.csv", "Binders2.csv"))

collection.total_value          # Decimal('9735.73')
collection.total_quantity       # 543

price_tiers(collection)["prime"].cash        # Decimal('4248.37')
multi_copies(collection, min_qty=4)          # the Mox Amber ×13 stack
where(collection, price_min=20, foil=True)   # filtered Collection
```

`Collection` chains, and every function also takes a plain list of `Card`:

```python
Collection.load_many("Binders.csv", "Binders2.csv").merged().where(price_min=20).top(10)
```

Filters combine as keyword criteria (AND) or as composable predicates:

```python
from binders.filters import any_of, is_rarity, negate, price_between

where(cards, price_min=5, price_max=20, rarity_in=["rare", "mythic"])
where(cards, any_of(is_rarity("mythic"), price_between(100, None)))
where(cards, negate(is_rarity("common")), language="en")
```

An unknown criterion raises rather than silently matching everything — a
typo'd filter that quietly returns the whole collection is how a bad buylist
gets submitted.

## Things this handles that bit during development

- **Two header dialects.** Current exports use `Title,Edition,Foil,...`; the
  older ones on disk use `Name,Set code,...` in a different order with
  word-valued foils (`normal`/`foil`). Both parse, and a header with no
  recognizable name column raises `UnknownSchema` instead of yielding rows with
  blank titles.
- **Money is `Decimal`.** The current exports sum to exactly `$9,737.83`. The
  same sum in float is `9737.829999999999927…` — it happens to print as
  `9737.83` at this size, but the error is real, grows with the collection, and
  lands on tier boundaries where a cent decides which band a card falls in.
  Nothing here touches float.
- **Collector numbers are strings.** `140★`, `35s`, `KLD-112`, `bs308` all
  appear in real data.
- **Prices drift between scans.** Five cards were scanned in both binders a week
  apart at different prices. Merging keeps the most recent, since that is the
  current market value. This matters at boundaries — Black Market Connections
  went $20.23 → $19.70, moving it from the prime band into mid.
- **Non-English cards.** `validate` flags them; vendors price them differently.
- **Round-trip fidelity.** `load()` → `save()` reproduces a current export
  byte-for-byte, so files stay re-importable into ManaBox.

## Tests

```bash
python3 run_tests.py          # preferred — adds the skip guard
python3 run_tests.py -v
python3 -m unittest discover -s tests -t .   # plain runner, no guard
```

CI runs on every pull request and on pushes to `main`, across Python 3.9, 3.11
and 3.13. There is no install step in the workflow on purpose: the package is
stdlib-only, and a bare interpreter is what keeps that honest.

`run_tests.py` fails the run if a test skips for any reason other than "the
ManaBox exports aren't on disk". Off the dev machine 24 tests skip for exactly
that reason — the whole vault oracle plus the real-export round-trip — so
plain `unittest` would report a cheerful `OK (skipped=24)` and hide a genuine
gap. All such skips route through `tests.support.require_exports`; anything
else is a build failure.

The tradeoff is deliberate: the exports are the actual collection with real
valuations, so they stay out of git. CI covers the other 86 tests — parsing,
both header dialects, merging, filters, tiering, exporters and the CLI.

`tests/test_vault_oracle.py` checks the code against the hand-built tables in
the vault, which were computed independently from the `.bak` exports — 752
cards, `$9,831`, and tier counts of 129/199/424. Those tests skip when the
exports aren't on disk.

Exact figures are asserted exactly; the cash/credit estimates are asserted
within a dollar, because the published table rounds by hand (`$7,100.85 × 0.60`
is `$4,260.51`, published as `~$4,260`).

## Ledger

`ledger` writes the schema from `ObsidianVault/30-projects/Financial Freedom
Profile.md`. Market Value and Valuation Date are filled in; Cost Basis and the
sale columns are deliberately blank — the plan there is a batch-level
good-faith reconstruction from TCGPlayer/eBay order history, which this file
cannot guess.
