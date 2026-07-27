"""Command line interface.

    python3 -m binders summary Binders.csv Binders2.csv
    python3 -m binders tiers Binders.csv Binders2.csv --markdown
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from typing import Iterable, List, Sequence

from .aggregate import (
    cents,
    group_by,
    high_value,
    merge,
    multi_copies,
    price_tiers,
    summarize,
    top_n,
)
from .diff import diff as diff_collections
from .export import (
    multi_copy_table,
    tier_table,
    to_buylist_csv,
    to_ledger_csv,
    to_manabox_csv,
    to_markdown,
    top_table,
)
from .filters import where
from .io import load, load_many, validate
from .model import Card, Collection

__all__ = ["main", "build_parser"]


# --- output helpers ---------------------------------------------------------


def _table(rows: Sequence[Sequence], headers: Sequence[str], *, markdown: bool = False) -> str:
    if markdown:
        return to_markdown(rows, headers)
    body = [[str(cell) for cell in row] for row in rows]
    widths = [len(str(h)) for h in headers]
    for row in body:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = ["  ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    for row in body:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(lines)


def _money(value) -> str:
    return f"${cents(value):,.2f}"


def _load(args) -> Collection:
    cards = load_many(*args.files)
    if getattr(args, "no_merge", False):
        return cards
    return merge(cards)


def _card_rows(cards: Iterable[Card]) -> List[Sequence]:
    return [
        [
            card.display_name,
            card.edition,
            card.rarity,
            f"x{card.quantity}",
            _money(card.market_price),
            _money(card.total_value),
            "|".join(card.sources),
        ]
        for card in cards
    ]


_CARD_HEADERS = ["Card", "Set", "Rarity", "Qty", "Each", "Total", "Source"]


# --- commands ---------------------------------------------------------------


def cmd_summary(args) -> int:
    raw = load_many(*args.files)
    cards = raw if getattr(args, "no_merge", False) else merge(raw)
    s = summarize(cards)

    print(f"Rows           {s.rows}")
    print(f"Distinct cards {s.distinct}")
    print(f"Total quantity {s.quantity}")
    print(f"Total value    {_money(s.total_value)}")
    print(f"Mean / median  {_money(s.mean_price)} / {_money(s.median_price)}")
    print(f"Most expensive {_money(s.max_price)}")
    print(f"Foils          {s.foil_quantity} cards, {_money(s.foil_value)}")

    print("\nBy rarity")
    print(_table(
        [[name, qty, _money(value)] for name, (qty, value) in s.by_rarity.items()],
        ["Rarity", "Qty", "Value"],
        markdown=args.markdown,
    ))

    # Per-binder figures come from the unmerged rows. On merged data a card
    # owned in two binders belongs to both sources, so its (summed) quantity
    # would be counted twice and the columns would not add up to the total.
    per_source = summarize(raw).by_source
    print("\nBy source (as scanned, before merging)")
    print(_table(
        [[name, qty, _money(value)] for name, (qty, value) in per_source.items()],
        ["Source", "Qty", "Value"],
        markdown=args.markdown,
    ))

    top_sets = list(s.by_set.items())[: args.sets]
    if top_sets:
        print(f"\nTop {len(top_sets)} sets by value")
        print(_table(
            [[name, qty, _money(value)] for name, (qty, value) in top_sets],
            ["Set", "Qty", "Value"],
            markdown=args.markdown,
        ))
    return 0


def cmd_tiers(args) -> int:
    cards = _load(args)
    if args.markdown:
        print(tier_table(cards))
        return 0

    rows = []
    totals = [0, Decimal("0"), Decimal("0"), Decimal("0")]
    for row in price_tiers(cards).values():
        rows.append([
            row.label,
            f"{row.quantity} cards",
            _money(row.market_value),
            f"{_money(row.cash)} ({int(row.tier.cash_rate * 100)}%)",
            f"{_money(row.credit)} ({int(row.tier.credit_rate * 100)}%)",
        ])
        totals[0] += row.quantity
        totals[1] += row.market_value
        totals[2] += row.cash
        totals[3] += row.credit
    rows.append([
        "Total",
        f"{totals[0]} cards",
        _money(totals[1]),
        _money(totals[2]),
        _money(totals[3]),
    ])
    print(_table(rows, ["Tier", "Cards", "Market Value", "Cash Est.", "Credit Est."]))
    return 0


def cmd_dupes(args) -> int:
    stacks = multi_copies(_load(args), min_qty=args.min_qty, already_merged=not args.no_merge)
    if not stacks:
        print(f"No cards held in {args.min_qty}+ copies.")
        return 0
    if args.markdown:
        print(multi_copy_table(stacks, min_qty=args.min_qty, limit=args.limit))
        return 0
    print(_table(_card_rows(stacks[: args.limit]), _CARD_HEADERS))
    print(f"\n{len(stacks)} stacks, {_money(Collection(stacks).total_value)} total")
    return 0


def cmd_high_value(args) -> int:
    hits = high_value(_load(args), threshold=args.min)
    print(_table(_card_rows(hits[: args.limit]), _CARD_HEADERS))
    print(f"\n{len(hits)} cards at ${args.min}+, {_money(Collection(hits).total_value)} total")
    return 0


def cmd_top(args) -> int:
    cards = top_n(_load(args), n=args.number)
    if args.markdown:
        print(top_table(cards, n=args.number))
        return 0
    print(_table(_card_rows(cards), _CARD_HEADERS))
    return 0


def cmd_filter(args) -> int:
    criteria = {
        "price_min": args.price_min,
        "price_max": args.price_max,
        "qty_min": args.qty_min,
        "rarity_in": args.rarity.split(",") if args.rarity else None,
        "edition_in": args.edition.split(",") if args.edition else None,
        "title_contains": args.title,
        "set_name_contains": args.set,
        "language": args.language,
        "tier_in": args.tier.split(",") if args.tier else None,
    }
    if args.foil:
        criteria["foil"] = True
    elif args.non_foil:
        criteria["foil"] = False

    cards = where(_load(args), **criteria)

    if args.output:
        to_manabox_csv(cards, args.output, include_sources=args.with_sources)
        print(f"Wrote {len(cards)} rows to {args.output}")
        return 0

    print(_table(_card_rows(cards[: args.limit]), _CARD_HEADERS))
    print(f"\n{len(cards)} rows, {cards.total_quantity} cards, {_money(cards.total_value)}")
    return 0


def cmd_diff(args) -> int:
    result = diff_collections(load(args.old), load(args.new))

    print(f"{args.old} -> {args.new}")
    print(f"  added            {len(result.added):>4} cards  {_money(result.value_added)}")
    print(f"  removed          {len(result.removed):>4} cards  {_money(result.value_removed)}")
    print(f"  quantity changed {len(result.quantity_changed):>4}")
    print(f"  price changed    {len(result.price_changed):>4}")
    print(f"  unchanged        {len(result.unchanged):>4}")
    print(f"  net value        {_money(result.value_delta)}")
    print(f"  net quantity     {result.quantity_delta:+d}")

    if result.added and not args.summary_only:
        print("\nAdded")
        print(_table(_card_rows(result.added[: args.limit]), _CARD_HEADERS))
    if result.removed and not args.summary_only:
        print("\nRemoved")
        print(_table(_card_rows(result.removed[: args.limit]), _CARD_HEADERS))
    if result.quantity_changed and not args.summary_only:
        print("\nQuantity changed")
        print(_table(
            [
                [c.name, f"{c.before.quantity} -> {c.after.quantity}", _money(c.value_delta)]
                for c in result.quantity_changed[: args.limit]
            ],
            ["Card", "Qty", "Value delta"],
        ))
    if result.price_changed and not args.summary_only:
        print("\nPrice changed")
        print(_table(
            [
                [c.name, f"{_money(c.before.market_price)} -> {_money(c.after.market_price)}", _money(c.value_delta)]
                for c in result.price_changed[: args.limit]
            ],
            ["Card", "Price", "Value delta"],
        ))
    return 0


def cmd_merge(args) -> int:
    cards = merge(load_many(*args.files))
    to_manabox_csv(cards, args.output, include_sources=args.with_sources)
    print(
        f"Merged {len(args.files)} files -> {len(cards)} rows, "
        f"{cards.total_quantity} cards, {_money(cards.total_value)} -> {args.output}"
    )
    return 0


def cmd_buylist(args) -> int:
    rows = to_buylist_csv(_load(args), args.output, min_price=args.min_price, already_merged=True)
    print(
        f"Wrote {len(rows)} rows ({rows.total_quantity} cards, "
        f"{_money(rows.total_value)} market) to {args.output}"
    )
    return 0


def cmd_ledger(args) -> int:
    rows = to_ledger_csv(_load(args), args.output, already_merged=True)
    print(f"Wrote {len(rows)} rows to {args.output}")
    print("Cost Basis and the sale columns are intentionally blank — fill from order history.")
    return 0


def cmd_validate(args) -> int:
    issues = []
    for path in args.files:
        issues.extend(validate(load(path)))
    if not issues:
        print("No issues found.")
        return 0

    by_code = group_issues(issues)
    for code, group in by_code.items():
        print(f"\n{code} ({len(group)})")
        for issue in group[: args.limit]:
            print(f"  {issue.message}")
        if len(group) > args.limit:
            print(f"  ... and {len(group) - args.limit} more")

    errors = [i for i in issues if i.level == "error"]
    noun = "issue" if len(issues) == 1 else "issues"
    print(f"\n{len(issues)} {noun}, {len(errors)} of them errors")
    return 1 if errors else 0


def group_issues(issues):
    from collections import OrderedDict

    out = OrderedDict()
    for issue in issues:
        out.setdefault(issue.code, []).append(issue)
    return out


# --- parser -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="binders", description="Utilities for ManaBox binder CSV exports."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def with_files(p, *, merging: bool = True):
        p.add_argument("files", nargs="+", help="ManaBox CSV export(s)")
        if merging:
            p.add_argument(
                "--no-merge",
                action="store_true",
                help="keep rows per-file instead of collapsing duplicate cards",
            )
        return p

    def with_markdown(p):
        p.add_argument("--markdown", action="store_true", help="render as a vault-ready pipe table")
        return p

    p = with_markdown(with_files(sub.add_parser("summary", help="counts, value and breakdowns")))
    p.add_argument("--sets", type=int, default=10, help="how many top sets to show")
    p.set_defaults(func=cmd_summary)

    p = with_markdown(with_files(sub.add_parser("tiers", help="price bands with buylist estimates")))
    p.set_defaults(func=cmd_tiers)

    p = with_markdown(with_files(sub.add_parser("dupes", help="cards held in multiple copies")))
    p.add_argument("--min-qty", type=int, default=2)
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_dupes)

    p = with_files(sub.add_parser("high-value", help="cards worth a condition check"))
    p.add_argument("--min", default="10", help="price threshold (default 10)")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_high_value)

    p = with_markdown(with_files(sub.add_parser("top", help="highest-value positions")))
    p.add_argument("-n", "--number", type=int, default=20)
    p.set_defaults(func=cmd_top)

    p = with_files(sub.add_parser("filter", help="query by price, rarity, set, name"))
    p.add_argument("--price-min")
    p.add_argument("--price-max")
    p.add_argument("--qty-min", type=int)
    p.add_argument("--rarity", help="comma separated: rare,mythic")
    p.add_argument("--edition", help="comma separated set codes: CLB,CMM")
    p.add_argument("--tier", help="comma separated: prime,mid,bulk")
    p.add_argument("--title", help="substring match, accent and case insensitive")
    p.add_argument("--set", help="substring match on full set name")
    p.add_argument("--language")
    p.add_argument("--foil", action="store_true")
    p.add_argument("--non-foil", action="store_true")
    p.add_argument("-o", "--output", help="write matches to a ManaBox CSV instead of printing")
    p.add_argument("--with-sources", action="store_true", help="add a Source column on output")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_filter)

    p = sub.add_parser("diff", help="compare two scans")
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("--summary-only", action="store_true")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("merge", help="combine exports into one deduplicated CSV")
    p.add_argument("files", nargs="+")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--with-sources", action="store_true")
    p.set_defaults(func=cmd_merge)

    p = with_files(sub.add_parser("buylist", help="write a vendor submission list"))
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--min-price", default="1", help="drop cards below this price (default 1)")
    p.set_defaults(func=cmd_buylist)

    p = with_files(sub.add_parser("ledger", help="write the tracking ledger CSV"))
    p.add_argument("-o", "--output", required=True)
    p.set_defaults(func=cmd_ledger)

    p = sub.add_parser("validate", help="flag rows that need a human look")
    p.add_argument("files", nargs="+")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_validate)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"error: {exc.filename}: no such file", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
