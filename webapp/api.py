"""The JSON API.

Everything the front end can do goes through here. The domain modules —
`repo`, `bulk`, `importer`, `operations` — are unchanged; the Jinja templates
were only ever one presentation of them, and this is another.

Two rules carried over from the server-rendered version, because neither is a
view concern:

**Money crosses as integer cents.** JSON numbers are IEEE doubles. Cents are
exact integers far inside 2^53; dollars-as-float are not. Every row carries
`priceCents` plus a preformatted display string, so the client never does
money arithmetic.

**Bulk selection is resolved server-side.** The client sends explicit ids, or
`{"selectAll": true}` plus filters — never a materialized id list standing in
for "everything matching". A filter that changed since the page rendered
therefore cannot silently widen an edit.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, Optional

from flask import Blueprint, current_app, jsonify, request

from . import bulk, importer
from . import operations as ops
from . import repo
from .db import format_cents, transaction

__all__ = ["api", "ApiError"]

api = Blueprint("api", __name__, url_prefix="/api")


class ApiError(Exception):
    """A failure with a status code and a message meant for a person."""

    def __init__(self, message: str, status: int = 400, code: str = "error"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


@api.errorhandler(ApiError)
def _handle(exc: ApiError):
    return jsonify({"error": exc.message, "code": exc.code}), exc.status


def db() -> sqlite3.Connection:
    from .app import db as _db

    return _db()


def _payload() -> Dict[str, Any]:
    return request.get_json(silent=True) or {}


#: Query parameters that are not filters. Anything else must be a known filter.
NON_FILTER_PARAMS = frozenset({"sort", "dir", "page", "perPage"})


def _filters(source, *, strict: bool = True) -> Dict[str, Any]:
    """Pull filters out of a query string or a JSON body.

    Strict on purpose. Picking out only the *known* keys would silently drop a
    typo — `prices_min=10` would return the whole collection while looking
    filtered, which is exactly how a bulk edit hits rows nobody meant to touch.
    """
    if strict:
        unknown = [
            key
            for key in source.keys()
            if key not in repo.FILTERS and key not in NON_FILTER_PARAMS
        ]
        if unknown:
            known = ", ".join(sorted(repo.FILTERS))
            raise ApiError(
                f"Unknown filter(s): {', '.join(sorted(unknown))}. Available: {known}",
                400,
                "bad-filter",
            )

    out: Dict[str, Any] = {}
    for key in repo.FILTERS:
        value = source.get(key)
        if value not in (None, "", []):
            out[key] = value
    return out


# --- session -----------------------------------------------------------------


@api.get("/session")
def session_info():
    from .app import csrf_token

    undoable = ops.latest_undoable(db())
    return jsonify({
        "csrfToken": csrf_token(),
        "database": current_app.config["DATABASE"],
        "undoable": _operation(undoable) if undoable else None,
    })


# --- collection ---------------------------------------------------------------


def _holding(row) -> dict:
    price = row["price_cents"]
    total = price * row["quantity"] if price is not None else None
    return {
        "id": row["id"],
        "title": row["title"],
        "edition": row["edition"],
        "setName": row["set_name"],
        "collectorNumber": row["collector_number"],
        "rarity": row["rarity"],
        "foil": bool(row["foil"]),
        "quantity": row["quantity"],
        "priceCents": price,
        "totalCents": total,
        "price": format_cents(price),
        "total": format_cents(total),
        "condition": row["condition"],
        "language": row["language"],
        "verdict": row["verdict"],
    }


@api.get("/collection")
def collection():
    filters = _filters(request.args)
    try:
        page = repo.query_holdings(
            db(),
            filters,
            sort=request.args.get("sort", repo.DEFAULT_SORT),
            direction=request.args.get("dir", "desc"),
            page=int(request.args.get("page", 1) or 1),
            per_page=int(request.args.get("perPage", 50) or 50),
        )
    except ValueError as exc:
        raise ApiError(str(exc), 400, "bad-filter")

    scoped = repo.totals(db(), filters)
    grand = repo.totals(db(), {})

    return jsonify({
        "rows": [_holding(r) for r in page.rows],
        "page": page.page,
        "perPage": page.per_page,
        "pages": page.pages,
        "totalRows": page.total_rows,
        "sort": page.sort,
        "direction": page.direction,
        "totals": _totals(scoped),
        "grandTotals": _totals(grand),
        "facets": {
            "editions": repo.distinct_values(db(), "edition"),
            "rarities": repo.distinct_values(db(), "rarity"),
            "conditions": repo.distinct_values(db(), "condition"),
        },
    })


def _totals(t: dict) -> dict:
    return {
        "rows": t["rows"],
        "quantity": t["quantity"],
        "valueCents": t["value_cents"],
        "value": format_cents(t["value_cents"]),
        "unpriced": t["unpriced"],
    }


@api.get("/collection/insights")
def insights():
    """Chart data for the current slice.

    Same filters as `/collection`, so the charts always describe exactly what
    the table below them shows — a chart that silently ignored the filter would
    be worse than no chart.
    """
    filters = _filters(request.args)
    tiers = repo.tier_breakdown(db(), filters)

    return jsonify({
        "concentration": repo.concentration(db(), filters),
        "tiers": [
            {
                **t,
                "market": format_cents(t["marketCents"]),
                "cash": format_cents(t["cashCents"]),
                "credit": format_cents(t["creditCents"]),
            }
            for t in tiers
        ],
        "sets": [
            {**s, "value": format_cents(s["cents"])}
            for s in repo.top_sets(db(), filters)
        ],
        "rarity": [
            {**r, "value": format_cents(r["cents"])}
            for r in repo.rarity_split(db(), filters)
        ],
        "totals": _totals(repo.totals(db(), filters)),
    })


# --- bulk ---------------------------------------------------------------------


@api.get("/bulk/actions")
def bulk_actions():
    return jsonify([
        {
            "key": key,
            "label": spec["label"],
            "needsValue": spec["needs_value"],
            "destructive": bool(spec.get("destructive")),
        }
        for key, spec in bulk.ACTIONS.items()
    ])


@api.post("/bulk/preview")
def bulk_preview():
    """What the confirmation dialog shows: the real count and a real sample."""
    body = _payload()
    try:
        target = bulk.resolve_selection(
            db(),
            ids=body.get("ids"),
            filters=_filters(body.get("filters") or {}),
            select_all=bool(body.get("selectAll")),
        )
    except (bulk.BulkError, ValueError) as exc:
        raise ApiError(str(exc), 400, "bad-selection")

    preview = bulk.preview(db(), target)
    return jsonify({
        "count": preview["count"],
        "quantity": preview["quantity"],
        "valueCents": preview["value_cents"],
        "value": format_cents(preview["value_cents"]),
        "more": preview["more"],
        "sample": [
            {
                "title": r["title"],
                "edition": r["edition"],
                "quantity": r["quantity"],
                "price": format_cents(r["price_cents"]),
            }
            for r in preview["sample"]
        ],
    })


@api.post("/bulk")
def bulk_apply():
    body = _payload()
    try:
        # Resolved here, from ids or filters — never from a count the client
        # sent. This is the guarantee that a stale filter cannot widen an edit.
        target = bulk.resolve_selection(
            db(),
            ids=body.get("ids"),
            filters=_filters(body.get("filters") or {}),
            select_all=bool(body.get("selectAll")),
        )
        with transaction(db()):
            result = bulk.apply_action(
                db(), body.get("action", ""), target, body.get("value")
            )
    except (bulk.BulkError, ValueError) as exc:
        raise ApiError(str(exc), 400, "bulk-failed")

    return jsonify({"affected": result["affected"], "summary": result["summary"]})


# --- imports ------------------------------------------------------------------


def _import(row) -> dict:
    return {
        "id": row["id"],
        "filename": row["filename"],
        "kind": row["kind"],
        "dialect": row["dialect"],
        "rowCount": row["row_count"],
        "status": row["status"],
        "createdAt": row["created_at"],
        "committedAt": row["committed_at"],
    }


@api.get("/imports")
def imports_list():
    rows = db().execute("SELECT * FROM imports ORDER BY id DESC LIMIT 50").fetchall()
    return jsonify([_import(r) for r in rows])


@api.post("/imports")
def imports_upload():
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        raise ApiError("Choose a CSV first.", 400, "no-file")

    data = upload.read()
    try:
        with transaction(db()):
            import_id, kind = importer.stage_import(
                db(), os.path.basename(upload.filename), data
            )
    except importer.DetectionError as exc:
        raise ApiError(str(exc), 400, "unrecognized")
    except FileExistsError as exc:
        raise ApiError(str(exc), 409, "duplicate")

    return jsonify({"importId": import_id, "kind": kind}), 201


@api.get("/imports/<int:import_id>")
def imports_detail(import_id: int):
    record = db().execute("SELECT * FROM imports WHERE id = ?", (import_id,)).fetchone()
    if record is None:
        raise ApiError(f"No import {import_id}.", 404, "not-found")

    grouped = importer.issues_for(db(), import_id)
    return jsonify({
        "record": _import(record),
        "blocking": importer.blocking_count(db(), import_id),
        "blockingCodes": sorted(importer.BLOCKING),
        "issues": [
            {
                "code": code,
                "blocking": code in importer.BLOCKING,
                "rows": [
                    {
                        "id": item["id"],
                        "lineNo": item["line_no"],
                        "name": item["parsed"].get("title")
                        or item["parsed"].get("raw_name")
                        or "",
                        "candidates": item["parsed"].get("candidates") or [],
                        "state": item["state"],
                    }
                    for item in items
                ],
            }
            for code, items in grouped.items()
        ],
    })


@api.post("/imports/<int:import_id>/rows/<int:row_id>")
def imports_resolve_row(import_id: int, row_id: int):
    import json as _json

    body = _payload()
    row = db().execute(
        "SELECT * FROM staged_rows WHERE id = ? AND import_id = ?", (row_id, import_id)
    ).fetchone()
    if row is None:
        raise ApiError("No such staged row.", 404, "not-found")

    with transaction(db()):
        if body.get("skip"):
            db().execute(
                "UPDATE staged_rows SET state = 'skipped' WHERE id = ?", (row_id,)
            )
        else:
            resolution = _json.loads(row["resolution"] or "{}")
            for key in ("set_code", "identity", "mtgjson_uuid", "product_name"):
                if body.get(key):
                    resolution[key] = body[key]
            db().execute(
                "UPDATE staged_rows SET resolution = ?, state = 'resolved' WHERE id = ?",
                (_json.dumps(resolution), row_id),
            )

    return jsonify({"blocking": importer.blocking_count(db(), import_id)})


@api.post("/imports/<int:import_id>/commit")
def imports_commit(import_id: int):
    try:
        with transaction(db()):
            result = importer.commit_import(db(), import_id)
    except LookupError as exc:
        raise ApiError(str(exc), 404, "not-found")
    except ValueError as exc:
        raise ApiError(str(exc), 409, "blocked")
    return jsonify(result)


@api.post("/imports/<int:import_id>/discard")
def imports_discard(import_id: int):
    with transaction(db()):
        importer.discard_import(db(), import_id)
    return jsonify({"discarded": import_id})


# --- history ------------------------------------------------------------------


def _operation(op) -> dict:
    return {
        "id": op.id,
        "kind": op.kind,
        "summary": op.summary,
        "affected": op.affected,
        "createdAt": op.created_at,
        "revertedAt": op.reverted_at,
        "reverted": op.reverted,
    }


@api.get("/history")
def history():
    return jsonify([_operation(o) for o in ops.recent(db(), 50)])


@api.post("/undo")
def undo():
    try:
        with transaction(db()):
            operation = ops.undo(db())
    except LookupError as exc:
        raise ApiError(str(exc), 409, "nothing-to-undo")
    return jsonify(_operation(operation))
