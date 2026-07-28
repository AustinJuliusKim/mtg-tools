"""Getting everything back out.

The database became the system of record when imports started accumulating
state the CSVs never held — verdicts, sale records, an undo log. That was the
right call, but it created a lock-in the file-based workflow never had: before,
the collection *was* a CSV you could open anywhere.

So this is a v1 feature rather than a nice-to-have. Three levels, cheapest
first:

- **`table_csv`** — one table, plain CSV, openable in anything.
- **`bundle`** — every table plus a manifest, as a zip.
- **`snapshot`** — a byte-for-byte copy of the SQLite file, taken through the
  backup API so it is consistent even if something is mid-write.

Money is written as decimal dollars here, not cents. This is the one place that
is correct: a spreadsheet should show `75.17`, and the integer-cents discipline
exists to protect arithmetic, which an export no longer does.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Tuple

from binders.export import LEDGER_COLUMNS

from .db import format_cents

__all__ = [
    "EXPORTABLE",
    "MONEY_COLUMNS",
    "table_names",
    "table_csv",
    "ledger_csv",
    "bundle",
    "snapshot",
]

#: Tables worth exporting. `schema_version` is machinery, not data.
EXPORTABLE = (
    "holdings",
    "sealed",
    "verdicts",
    "sales",
    "price_history",
    "imports",
    "operations",
)

#: Columns rendered as decimal dollars rather than raw cents.
MONEY_COLUMNS = frozenset({
    "price_cents", "cost_basis_cents", "listed_cents", "sold_cents",
    "fees_cents", "shipping_cents", "net_cents", "realized_gain_cents",
})

#: `operations.inverse` holds a JSON blob that is large and meaningless outside
#: the app. The row is still useful as an audit trail without it.
OMIT = {("operations", "inverse")}


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def table_names(conn: sqlite3.Connection) -> List[str]:
    present = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    return [t for t in EXPORTABLE if t in present]


def _render(table: str, column: str, value) -> str:
    if value is None:
        return ""
    if column in MONEY_COLUMNS:
        # Dollars with two places — what a spreadsheet expects.
        return f"{Decimal(int(value)) / 100:.2f}"
    return str(value)


def table_csv(conn: sqlite3.Connection, table: str) -> str:
    """One table as CSV. Raises on an unknown table rather than guessing."""
    if table not in EXPORTABLE:
        raise ValueError(f"{table!r} is not exportable")

    columns = [
        r[1]
        for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
        if (table, r[1]) not in OMIT
    ]
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")

    # Money columns are relabelled so nobody reads dollars as cents later.
    writer.writerow([c[:-6] if c in MONEY_COLUMNS else c for c in columns])
    for row in conn.execute(
        f"SELECT {','.join(columns)} FROM {table} ORDER BY rowid"
    ).fetchall():
        writer.writerow([_render(table, c, row[c]) for c in columns])
    return buffer.getvalue()


def ledger_csv(conn: sqlite3.Connection) -> str:
    """The tax/insurance ledger, in the schema the vault already specifies.

    Same columns as `binders.export.LEDGER_COLUMNS`, so singles, sealed and
    anything sold land in one `mtg_collection_tracker.csv`.
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=list(LEDGER_COLUMNS), lineterminator="\r\n"
    )
    writer.writeheader()

    sold = {
        (r["subject_kind"], r["subject_id"]): r
        for r in conn.execute(
            "SELECT * FROM sales WHERE status = 'sold' ORDER BY sold_at"
        ).fetchall()
    }

    def money(cents) -> str:
        return "" if cents is None else f"{Decimal(int(cents)) / 100:.2f}"

    for row in conn.execute(
        "SELECT h.*, COALESCE(v.verdict,'undecided') AS verdict FROM holdings h "
        "LEFT JOIN verdicts v ON v.subject_kind='holding' AND v.subject_id=h.id "
        "ORDER BY (COALESCE(h.price_cents,0) * h.quantity) DESC"
    ).fetchall():
        sale = sold.get(("holding", row["id"]))
        total = (row["price_cents"] or 0) * row["quantity"]
        writer.writerow({
            "Name": row["title"],
            "Set name": row["set_name"],
            "Set code": row["edition"],
            "Collector number": row["collector_number"],
            "Foil": "foil" if row["foil"] else "",
            "Quantity": row["quantity"],
            "Condition": row["condition"],
            "Language": row["language"],
            "Scryfall ID": row["scryfall_id"],
            "Acquisition Date": "",
            "Source": "singles",
            "Cost Basis": "",
            "Market Value": money(total) if row["price_cents"] is not None else "",
            "Valuation Date": (row["updated_at"] or "")[:10],
            "Sold": money(sale["sold_cents"]) if sale else "",
            "Fees": money((sale["fees_cents"] or 0) + (sale["shipping_cents"] or 0))
            if sale
            else "",
            "Net Proceeds": money(sale["net_cents"]) if sale else "",
            "Realized Gain/Loss": money(sale["realized_gain_cents"]) if sale else "",
            "Insurance Flag": "Y" if total >= 5000 else "",
            "Photo Ref": "",
            "Notes": sale["notes"] if sale else "",
        })

    for row in conn.execute(
        "SELECT * FROM sealed ORDER BY (COALESCE(price_cents,0) * quantity) DESC"
    ).fetchall():
        sale = sold.get(("sealed", row["id"]))
        total = (row["price_cents"] or 0) * row["quantity"]
        writer.writerow({
            "Name": row["product_name"] or row["raw_name"],
            "Set name": row["set_name"],
            "Set code": row["set_code"],
            "Collector number": "",
            "Foil": "",
            "Quantity": row["quantity"],
            "Condition": row["condition"],
            "Language": "en",
            "Scryfall ID": "",
            "Acquisition Date": "",
            "Source": "sealed",
            "Cost Basis": money(
                (row["cost_basis_cents"] or 0) * row["quantity"]
            ) if row["cost_basis_cents"] is not None else "",
            "Market Value": money(total) if row["price_cents"] is not None else "",
            # A sealed price was looked up on a specific day; claiming today's
            # date for it would be false.
            "Valuation Date": row["price_date"] or "",
            "Sold": money(sale["sold_cents"]) if sale else "",
            "Fees": money((sale["fees_cents"] or 0) + (sale["shipping_cents"] or 0))
            if sale
            else "",
            "Net Proceeds": money(sale["net_cents"]) if sale else "",
            "Realized Gain/Loss": money(sale["realized_gain_cents"]) if sale else "",
            "Insurance Flag": "Y" if total >= 5000 else "",
            "Photo Ref": "",
            "Notes": " · ".join(
                p for p in (row["notes"], sale["notes"] if sale else "") if p
            ),
        })

    # Sold-and-gone rows. A fully-sold item is deleted from holdings, so
    # iterating the collection alone would drop it — and with it the realized
    # gain the ledger exists to record. Caught by exporting after a real sale
    # and finding zero rows carrying sale figures.
    still_held = {
        ("holding", r["id"]) for r in conn.execute("SELECT id FROM holdings")
    } | {("sealed", r["id"]) for r in conn.execute("SELECT id FROM sealed")}

    for sale in sold.values():
        if (sale["subject_kind"], sale["subject_id"]) in still_held:
            continue
        writer.writerow({
            "Name": sale["subject_name"] or "(sold)",
            "Set name": "",
            "Set code": sale["subject_set"] or "",
            "Collector number": "",
            "Foil": "",
            "Quantity": sale["quantity"],
            "Condition": "",
            "Language": "",
            "Scryfall ID": "",
            "Acquisition Date": "",
            "Source": f"sold · {sale['channel']}" if sale["channel"] else "sold",
            "Cost Basis": money(sale["cost_basis_cents"]),
            # No longer owned, so it carries no market value or valuation date.
            "Market Value": "",
            "Valuation Date": "",
            "Sold": money(sale["sold_cents"]),
            "Fees": money((sale["fees_cents"] or 0) + (sale["shipping_cents"] or 0)),
            "Net Proceeds": money(sale["net_cents"]),
            "Realized Gain/Loss": money(sale["realized_gain_cents"]),
            "Insurance Flag": "",
            "Photo Ref": "",
            "Notes": sale["notes"],
        })

    return buffer.getvalue()


def manifest(conn: sqlite3.Connection) -> str:
    counts = {
        table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        for table in table_names(conn)
    }
    totals = conn.execute(
        "SELECT COALESCE(SUM(quantity),0) AS qty, "
        "COALESCE(SUM(COALESCE(price_cents,0)*quantity),0) AS cents FROM holdings"
    ).fetchone()

    return json.dumps(
        {
            "exportedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": "mtg-tools",
            "rowCounts": counts,
            "singles": {
                "quantity": totals["qty"],
                "valueCents": totals["cents"],
                "value": format_cents(totals["cents"]),
            },
            "notes": [
                "Money columns are decimal dollars; the database stores integer cents.",
                "collection.sqlite is the complete database — the CSVs are a "
                "convenience view of it.",
                "operations.inverse is omitted from the CSV; it is app-internal.",
            ],
        },
        indent=2,
    )


def bundle(conn: sqlite3.Connection, db_path: Optional[str] = None) -> Tuple[str, bytes]:
    """Everything, as a zip. Returns (filename, bytes)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for table in table_names(conn):
            archive.writestr(f"csv/{table}.csv", table_csv(conn, table))
        archive.writestr("mtg_collection_tracker.csv", ledger_csv(conn))
        archive.writestr("manifest.json", manifest(conn))
        if db_path and os.path.isfile(db_path):
            archive.writestr("collection.sqlite", snapshot(conn))
    return f"mtg-collection-{_stamp()}.zip", buffer.getvalue()


def snapshot(conn: sqlite3.Connection) -> bytes:
    """A consistent copy of the database.

    Uses SQLite's backup API rather than reading the file: with WAL enabled the
    file on disk is not the whole story, and a plain copy taken mid-write can
    land somewhere no transaction ever was.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "collection.sqlite")
        destination = sqlite3.connect(target)
        try:
            conn.backup(destination)
        finally:
            destination.close()
        with open(target, "rb") as handle:
            return handle.read()
