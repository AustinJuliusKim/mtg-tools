"""Queries over the stored collection.

Filter names mirror `binders.filters.CRITERIA` so the web UI, the CLI and the
dashboards all describe a slice the same way. The implementation is SQL rather
than the Python predicates, because filtering 543 rows in the database and
paginating is the whole point of storing them.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = ["FILTERS", "SORTS", "Page", "query_holdings", "totals", "distinct_values"]

#: keyword -> (SQL fragment, value transform). Names match binders.filters.
#: Fragments are written table-qualified up front. An earlier version qualified
#: them afterwards with string replacement, which silently corrupts any fragment
#: where a column name appears as a substring of another token.
FILTERS = {
    "price_min": ("h.price_cents >= ?", lambda v: int(round(float(v) * 100))),
    "price_max": ("h.price_cents <= ?", lambda v: int(round(float(v) * 100))),
    "qty_min": ("h.quantity >= ?", int),
    "edition": ("h.edition = ?", str),
    "rarity": ("h.rarity = ?", str),
    "language": ("h.language = ?", str),
    "condition": ("h.condition = ?", str),
    "foil": ("h.foil = ?", lambda v: 1 if v in (True, 1, "1", "true", "on") else 0),
    "title_contains": ("LOWER(h.title) LIKE ?", lambda v: f"%{str(v).lower()}%"),
    "set_contains": ("LOWER(h.set_name) LIKE ?", lambda v: f"%{str(v).lower()}%"),
    "unpriced": ("h.price_cents IS NULL", None),
    "verdict": ("COALESCE(v.verdict, 'undecided') = ?", str),
}

SORTS = {
    "title": "h.title COLLATE NOCASE",
    "edition": "h.edition COLLATE NOCASE",
    "rarity": "h.rarity",
    "quantity": "h.quantity",
    "price": "h.price_cents",
    "total": "(COALESCE(h.price_cents, 0) * h.quantity)",
    "updated": "h.updated_at",
}

DEFAULT_SORT = "total"


class Page:
    """One screen of results, plus what the whole filtered set looks like.

    `matching_ids` is the full filtered set, not just this page. Bulk actions
    need it so "select all N matching this filter" acts on exactly that set —
    the distinction between it and "the 50 rows you can see" is the difference
    between a correct bulk edit and a destructive one.
    """

    __slots__ = ("rows", "total_rows", "page", "per_page", "sort", "direction")

    def __init__(self, rows, total_rows, page, per_page, sort, direction):
        self.rows = rows
        self.total_rows = total_rows
        self.page = page
        self.per_page = per_page
        self.sort = sort
        self.direction = direction

    @property
    def pages(self) -> int:
        return max(1, -(-self.total_rows // self.per_page))

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def start_index(self) -> int:
        return 0 if not self.total_rows else (self.page - 1) * self.per_page + 1

    @property
    def end_index(self) -> int:
        return min(self.total_rows, self.page * self.per_page)


def _where(filters: Dict[str, Any]) -> Tuple[str, List[Any]]:
    clauses, params = [], []
    for key, value in (filters or {}).items():
        if value in (None, ""):
            continue
        if key not in FILTERS:
            raise ValueError(f"unknown filter {key!r}")
        fragment, transform = FILTERS[key]
        clauses.append(fragment)
        if transform is not None:
            params.append(transform(value))
    return (" AND ".join(clauses) if clauses else "1=1"), params


_BASE = """
FROM holdings h
LEFT JOIN verdicts v ON v.subject_kind = 'holding' AND v.subject_id = h.id
WHERE {where}
"""


def query_holdings(
    conn: sqlite3.Connection,
    filters: Optional[Dict[str, Any]] = None,
    *,
    sort: str = DEFAULT_SORT,
    direction: str = "desc",
    page: int = 1,
    per_page: int = 50,
) -> Page:
    where, params = _where(filters)
    order = SORTS.get(sort, SORTS[DEFAULT_SORT])
    direction = "ASC" if str(direction).lower() == "asc" else "DESC"
    page = max(1, int(page))
    per_page = max(1, min(500, int(per_page)))

    body = _BASE.format(where=where)
    total = conn.execute(f"SELECT COUNT(*) AS n {body}", params).fetchone()["n"]
    rows = conn.execute(
        f"SELECT h.*, COALESCE(v.verdict, 'undecided') AS verdict "
        f"{body} ORDER BY {order} {direction}, h.id ASC LIMIT ? OFFSET ?",
        params + [per_page, (page - 1) * per_page],
    ).fetchall()

    return Page(rows, total, page, per_page, sort, direction.lower())


def matching_ids(
    conn: sqlite3.Connection, filters: Optional[Dict[str, Any]] = None
) -> List[int]:
    """Every holding id matching the filter — the whole set, unpaginated."""
    where, params = _where(filters)
    body = _BASE.format(where=where)
    return [r["id"] for r in conn.execute(f"SELECT h.id {body}", params).fetchall()]


def totals(conn: sqlite3.Connection, filters: Optional[Dict[str, Any]] = None) -> dict:
    where, params = _where(filters)
    body = _BASE.format(where=where)
    row = conn.execute(
        f"SELECT COUNT(*) AS rows, "
        f"COALESCE(SUM(h.quantity), 0) AS quantity, "
        f"COALESCE(SUM(COALESCE(h.price_cents, 0) * h.quantity), 0) AS value_cents, "
        f"SUM(CASE WHEN h.price_cents IS NULL THEN h.quantity ELSE 0 END) AS unpriced "
        f"{body}",
        params,
    ).fetchone()
    return {
        "rows": row["rows"],
        "quantity": row["quantity"],
        "value_cents": row["value_cents"],
        "unpriced": row["unpriced"] or 0,
    }


def distinct_values(conn: sqlite3.Connection, column: str) -> List[str]:
    if column not in {"edition", "rarity", "language", "condition", "set_name"}:
        raise ValueError(f"cannot enumerate {column!r}")
    rows = conn.execute(
        f"SELECT DISTINCT {column} AS v FROM holdings "
        f"WHERE {column} != '' ORDER BY {column} COLLATE NOCASE"
    ).fetchall()
    return [r["v"] for r in rows]
