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
| `dashboard` | Build the self-contained HTML triage GUI |
| `validate` | Flag rows that need a human look |
| `sealed …` | Sealed commander deck tracker (see below) |

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

## The triage GUI

```bash
python3 -m binders dashboard ~/Desktop/Binders*.csv -o dashboard.html --open
```

One self-contained HTML file — no server, no network, no dependencies. Open it
from disk, or add `--fragment` to publish it as a Claude Artifact.

The point is the decision the CLI can't help with: keep or sell, card by card.
Charts sit up top (where the value concentrates, what a buylist pays, value by
binder, top sets), and below them a filterable table where each card gets a
verdict against a live running total for the sell pile. Verdicts persist in
browser storage keyed by card identity, so scanning another binder and
regenerating doesn't wipe decisions already made. Export writes the sell pile in
the same column shape `to_buylist_csv` produces.

Two notes on how the money works:

- **The page never recomputes the collection's figures.** Every aggregate comes
  from the `Decimal` code in `aggregate` and is embedded as a formatted string.
- **Per-card prices cross as integer cents**, because the one thing the page
  does compute is the sell-pile total. It sums a band then applies that band's
  rate once, mirroring `price_tiers` — verified against the real exports:
  marking everything Sell reproduces `binders tiers` to the cent in all three
  bands. A test pins the equivalence.

The generated file embeds the whole inventory, so it's gitignored.

## Sealed commander decks

Singles come from ManaBox. Sealed decks have no scanner and no buylist, so they
get their own tracker with hand-entered prices.

```bash
python3 -m binders sealed template -o sealed.csv   # start a list
python3 -m binders sealed doctor   sealed.csv      # fix what didn't resolve
python3 -m binders sealed summary  sealed.csv
python3 -m binders sealed ledger   sealed.csv -o sealed_ledger.csv
python3 -m binders sealed snapshot sealed.csv      # then diff two snapshots later
```

`sealed.csv` needs only a name and a quantity to start:

```
Name,Set,Quantity,Condition,Price,Price date,Source,Cost basis,Notes
Sneak Attack,,1,sealed,42.00,2026-07-27,tcgplayer,35.00,
Heavenly Inferno,CMD,1,sealed,240.00,2026-07-27,ebay,,the 2011 original
```

### Why prices are entered by hand

Not a shortcut — there is no good automated option:

| Source | Sealed prices | Access |
|---|---|---|
| TCGplayer API | yes | closed to new developers since late 2024 |
| eBay sold comps (Marketplace Insights) | yes | approved partners only |
| eBay Browse API | active listings | free, but **asking** prices, not realized |
| MTGJSON | **none** | free ([open request since 2022](https://github.com/mtgjson/mtgjson/issues/928)) |
| PriceCharting | yes | paid subscription |
| Scraping TCGplayer / CK / Amazon | yes | ToS problems, Cloudflare, brittle |

No free source publishes realized sale prices. Sealed product also moves slowly,
so a quarterly pass over a few dozen decks is tractable in a way it never would
be for 543 singles. Every price carries **its own date and source**, because an
undated valuation is not insurance documentation.

### What the tool does contribute

Product identity, which is the part a person gets wrong. MTGJSON's `SetList.json`
carries sealed product data, so `binders/data/commander_decks.json` vendors all
**220 commander decks** ever printed with their MTGJSON UUID and nine vendor
product IDs (TCGplayer, Card Kingdom, Cardmarket, CardTrader and others). That
file is committed, which is why the package stays offline — `sealed
refresh-catalog` is the only command that touches the network.

Resolution reports what it can't pin rather than guessing:

- **212 of 220 nicknames are unique**, so a bare `Sneak Attack` resolves.
- **8 collide** — Heavenly Inferno, Devour for Power, Evasive Maneuvers and five
  others exist in both an original Commander set and a later Anthology. These
  come back **ambiguous with both candidates listed**, because the printings can
  differ several-fold in price and picking one would silently misvalue a deck.
  Add a set code to pin it.
- **16 Collector's Edition variants** never collapse into their base deck, and
  resolving a base deck notes that the pricier variant exists.
- **25 products keep a set prefix in their nickname** (the set is "Warhammer
  40,000" but the product says "Warhammer 40000"), handled by a suffix-match
  tier so `Forces of the Imperium` still resolves — and still doesn't match the
  Collector's Edition.

Unpriced rows are never counted as `$0.00`: `summary` reports the total as a
floor and says how many decks are missing a price.

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
