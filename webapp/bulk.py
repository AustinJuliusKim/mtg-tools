"""Bulk edits over a selection.

Two things make bulk editing safe rather than terrifying:

**One transaction, one undo entry.** A 400-row price adjustment either happens
completely or not at all, and reverses as a single step.

**The selection is resolved server-side.** The client sends either an explicit
list of ids or "everything matching this filter" — never a count. A filter that
changed between rendering the page and pressing the button therefore cannot
silently widen what gets edited; the server re-runs the filter and reports the
number it is actually about to touch.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import operations as ops
from .db import now, to_cents
from .repo import matching_ids

__all__ = ["ACTIONS", "BulkError", "resolve_selection", "preview", "apply_action"]


class BulkError(ValueError):
    """A bulk request that should not be executed."""


#: Hard ceiling. Not a performance limit — a tripwire. Nothing in this
#: collection legitimately needs a single action over ten thousand rows, so a
#: request that large is far likelier to be a bug than an intent.
MAX_ROWS = 10_000


def resolve_selection(
    conn: sqlite3.Connection,
    *,
    ids: Optional[Sequence[int]] = None,
    filters: Optional[Dict[str, Any]] = None,
    select_all: bool = False,
) -> List[int]:
    """Turn a request into the exact list of holding ids it will affect."""
    if select_all:
        found = matching_ids(conn, filters or {})
    else:
        found = [int(i) for i in (ids or [])]
        if found:
            placeholders = ",".join("?" for _ in found)
            found = [
                r["id"]
                for r in conn.execute(
                    f"SELECT id FROM holdings WHERE id IN ({placeholders})", found
                ).fetchall()
            ]

    if not found:
        raise BulkError("Nothing was selected.")
    if len(found) > MAX_ROWS:
        raise BulkError(
            f"{len(found):,} rows is more than this is meant to touch at once "
            f"({MAX_ROWS:,} max). Narrow the filter."
        )
    return found


# --- the actions --------------------------------------------------------------


def _set_verdict(conn, ids, value, stamp):
    if value not in ("keep", "sell", "undecided"):
        raise BulkError(f"{value!r} is not a verdict")
    for holding_id in ids:
        if value == "undecided":
            conn.execute(
                "DELETE FROM verdicts WHERE subject_kind='holding' AND subject_id=?",
                (holding_id,),
            )
        else:
            conn.execute(
                "INSERT INTO verdicts (subject_kind, subject_id, verdict, decided_at) "
                "VALUES ('holding', ?, ?, ?) "
                "ON CONFLICT (subject_kind, subject_id) "
                "DO UPDATE SET verdict = excluded.verdict, decided_at = excluded.decided_at",
                (holding_id, value, stamp),
            )
    return f"Set verdict to {value}"


def _set_column(column: str, coerce: Callable[[Any], Any], label: str):
    def action(conn, ids, value, stamp):
        coerced = coerce(value)
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"UPDATE holdings SET {column} = ?, version = version + 1, updated_at = ? "
            f"WHERE id IN ({placeholders})",
            [coerced, stamp] + list(ids),
        )
        return f"{label} {value}"

    return action


def _adjust_price(conn, ids, value, stamp):
    """Move every selected price by a percentage, in exact integer cents.

    Rounded half-up per row rather than computed in float — the same discipline
    the rest of the stack uses, so a 5% bump on 400 cards doesn't drift.
    """
    try:
        pct = Decimal(str(value))
    except Exception:
        raise BulkError(f"{value!r} is not a percentage")
    if pct <= -100:
        raise BulkError("That would make every price zero or negative.")

    factor = (Decimal(100) + pct) / Decimal(100)
    rows = conn.execute(
        f"SELECT id, price_cents FROM holdings "
        f"WHERE id IN ({','.join('?' for _ in ids)}) AND price_cents IS NOT NULL",
        list(ids),
    ).fetchall()
    for row in rows:
        updated = int(
            (Decimal(row["price_cents"]) * factor).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        conn.execute(
            "UPDATE holdings SET price_cents = ?, version = version + 1, updated_at = ? "
            "WHERE id = ?",
            (max(0, updated), stamp, row["id"]),
        )
    return f"Adjusted prices by {pct:+}%"


def _delete(conn, ids, value, stamp):
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"DELETE FROM verdicts WHERE subject_kind='holding' AND subject_id IN ({placeholders})",
        list(ids),
    )
    conn.execute(f"DELETE FROM holdings WHERE id IN ({placeholders})", list(ids))
    return "Deleted"


def _price_cents(value):
    cents = to_cents(value)
    if cents is None:
        raise BulkError("Enter a price.")
    if cents < 0:
        raise BulkError("A price cannot be negative.")
    return cents


ACTIONS: Dict[str, dict] = {
    "verdict": {
        "label": "Set verdict",
        "run": _set_verdict,
        "needs_value": True,
        "touches": ("verdicts",),
    },
    "condition": {
        "label": "Set condition",
        "run": _set_column("condition", str, "Set condition to"),
        "needs_value": True,
        "touches": ("holdings",),
    },
    "language": {
        "label": "Set language",
        "run": _set_column("language", str, "Set language to"),
        "needs_value": True,
        "touches": ("holdings",),
    },
    "price": {
        "label": "Set price",
        "run": _set_column("price_cents", _price_cents, "Set price to"),
        "needs_value": True,
        "touches": ("holdings",),
    },
    "adjust_price": {
        "label": "Adjust price by %",
        "run": _adjust_price,
        "needs_value": True,
        "touches": ("holdings",),
    },
    "delete": {
        "label": "Delete",
        "run": _delete,
        "needs_value": False,
        "destructive": True,
        "touches": ("holdings", "verdicts"),
    },
}


# --- preview and apply ---------------------------------------------------------


def preview(conn: sqlite3.Connection, ids: Sequence[int], limit: int = 5) -> dict:
    """What the user is shown before confirming: the count and a real sample."""
    placeholders = ",".join("?" for _ in ids[:limit])
    sample = conn.execute(
        f"SELECT title, edition, quantity, price_cents FROM holdings "
        f"WHERE id IN ({placeholders}) ORDER BY (COALESCE(price_cents,0)*quantity) DESC",
        list(ids[:limit]),
    ).fetchall()
    totals = conn.execute(
        f"SELECT COALESCE(SUM(quantity),0) AS qty, "
        f"COALESCE(SUM(COALESCE(price_cents,0)*quantity),0) AS value "
        f"FROM holdings WHERE id IN ({','.join('?' for _ in ids)})",
        list(ids),
    ).fetchone()
    return {
        "count": len(ids),
        "quantity": totals["qty"],
        "value_cents": totals["value"],
        "sample": [dict(r) for r in sample],
        "more": max(0, len(ids) - limit),
    }


def apply_action(
    conn: sqlite3.Connection, action: str, ids: Sequence[int], value: Any = None
) -> dict:
    """Run one bulk action. Caller supplies the transaction."""
    spec = ACTIONS.get(action)
    if spec is None:
        raise BulkError(f"{action!r} is not a bulk action")
    if spec["needs_value"] and (value is None or value == ""):
        raise BulkError(f"{spec['label']} needs a value.")

    ids = list(ids)
    stamp = now()

    # Snapshot before mutating — the same ordering mistake that made the first
    # version of import-commit unreversible.
    before = {
        table: ops.snapshot_rows(
            conn,
            table,
            ids if table == "holdings" else [("holding", i) for i in ids],
        )
        for table in spec["touches"]
    }

    summary = spec["run"](conn, ids, value, stamp)

    ops.record(
        conn,
        f"bulk_{action}",
        f"{summary} on {len(ids)} row(s)",
        before=before,
        # A verdict set where none existed is an insert; recording the keys lets
        # undo delete them rather than leave an orphan behind.
        created={"verdicts": _new_verdict_keys(conn, ids, before)}
        if "verdicts" in spec["touches"]
        else None,
        affected=len(ids),
    )
    return {"affected": len(ids), "summary": summary}


def _new_verdict_keys(conn, ids, before) -> List[list]:
    had = {row["subject_id"] for row in before.get("verdicts", [])}
    now_has = {
        r["subject_id"]
        for r in conn.execute(
            f"SELECT subject_id FROM verdicts WHERE subject_kind='holding' "
            f"AND subject_id IN ({','.join('?' for _ in ids)})",
            list(ids),
        ).fetchall()
    }
    return [["holding", i] for i in sorted(now_has - had)]
