"""The Flask application.

Local-only by construction. `serve()` binds `127.0.0.1` and never `0.0.0.0` —
an "it's just localhost" app with no auth is still reachable from any page the
browser has open, which is also why every mutating route carries a CSRF token.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import threading
from typing import Any, Dict, Optional

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from . import bulk, importer
from . import operations as ops
from . import repo
from .db import (
    DEFAULT_DB,
    connect,
    format_cents,
    init_db,
    is_memory,
    memory_uri,
    transaction,
)

__all__ = ["create_app", "serve"]

MAX_UPLOAD_MB = 25


def create_app(db_path: Optional[str] = None, *, testing: bool = False) -> Flask:
    app = Flask(__name__)
    app.config.update(
        DATABASE=db_path or DEFAULT_DB,
        SECRET_KEY=os.environ.get("MTG_SECRET_KEY") or secrets.token_hex(32),
        MAX_CONTENT_LENGTH=MAX_UPLOAD_MB * 1024 * 1024,
        TESTING=testing,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_HTTPONLY=True,
    )

    # One connection PER THREAD. A single connection shared across threads
    # raises sqlite3.ProgrammingError on the first request the dev server
    # handles off the main thread — which happens in every real run and never
    # under a test client, so the suite passed while the server 500'd on
    # every page.
    #
    # An in-memory database must become a shared-cache URI for this to work: a
    # plain `:memory:` belongs to the connection that opened it, so each thread
    # would otherwise get its own empty database. Keeping one code path for
    # memory and file is the point — the divergence is what hid the bug.
    if is_memory(app.config["DATABASE"]):
        app.config["DATABASE"] = memory_uri()

    app.config["_LOCAL"] = threading.local()

    # An anchor connection kept for the app's lifetime. For a shared-cache
    # memory database this is load-bearing: the database is destroyed when the
    # last connection to it closes.
    app.config["_ANCHOR"] = connect(app.config["DATABASE"])
    init_db(app.config["_ANCHOR"])

    _register(app)
    return app


def db() -> sqlite3.Connection:
    """This thread's connection, opened on first use."""
    from flask import current_app

    store = current_app.config["_LOCAL"]
    conn = getattr(store, "conn", None)
    if conn is None:
        conn = connect(current_app.config["DATABASE"])
        store.conn = conn
    return conn


# --- CSRF ---------------------------------------------------------------------


def _token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def _check_csrf() -> None:
    sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token", "")
    expected = session.get("csrf", "")
    if not expected or not secrets.compare_digest(sent, expected):
        abort(400, "Stale or missing form token — reload the page and try again.")


def _register(app: Flask) -> None:
    @app.before_request
    def guard():
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            _check_csrf()

    @app.context_processor
    def inject():
        return {
            "csrf_token": _token(),
            "money": format_cents,
            "undoable": ops.latest_undoable(db()),
        }

    # --- collection -----------------------------------------------------------

    @app.route("/")
    def collection():
        filters = _filters_from(request.args)
        page = repo.query_holdings(
            db(),
            filters,
            sort=request.args.get("sort", repo.DEFAULT_SORT),
            direction=request.args.get("dir", "desc"),
            page=int(request.args.get("page", 1) or 1),
            per_page=int(request.args.get("per_page", 50) or 50),
        )
        return render_template(
            "collection.html",
            page=page,
            filters=filters,
            totals=repo.totals(db(), filters),
            grand=repo.totals(db(), {}),
            editions=repo.distinct_values(db(), "edition"),
            rarities=repo.distinct_values(db(), "rarity"),
            actions=bulk.ACTIONS,
            query=request.args.to_dict(flat=True),
        )

    @app.post("/bulk")
    def bulk_apply():
        filters = _filters_from(request.form)
        select_all = request.form.get("select_all") == "1"
        ids = request.form.getlist("ids")
        action = request.form.get("action", "")
        value = request.form.get("value")

        try:
            target = bulk.resolve_selection(
                db(), ids=ids, filters=filters, select_all=select_all
            )
            with transaction(db()):
                result = bulk.apply_action(db(), action, target, value)
        except (bulk.BulkError, ValueError) as exc:
            flash(str(exc), "error")
        else:
            flash(
                f"{result['summary']} on {result['affected']} row(s). "
                f"Undo is available.",
                "ok",
            )
        return redirect(request.form.get("back") or url_for("collection"))

    # --- imports --------------------------------------------------------------

    @app.route("/imports")
    def imports():
        rows = db().execute(
            "SELECT * FROM imports ORDER BY id DESC LIMIT 50"
        ).fetchall()
        return render_template("imports.html", imports=rows)

    @app.post("/imports")
    def upload():
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            flash("Choose a CSV first.", "error")
            return redirect(url_for("imports"))

        data = upload.read()
        try:
            with transaction(db()):
                import_id, kind = importer.stage_import(
                    db(), os.path.basename(upload.filename), data
                )
        except importer.DetectionError as exc:
            flash(str(exc), "error")
            return redirect(url_for("imports"))
        except FileExistsError as exc:
            flash(str(exc), "error")
            return redirect(url_for("imports"))

        flash(f"Staged {kind} import — nothing has changed yet.", "ok")
        return redirect(url_for("review", import_id=import_id))

    @app.route("/imports/<int:import_id>")
    def review(import_id: int):
        record = db().execute(
            "SELECT * FROM imports WHERE id = ?", (import_id,)
        ).fetchone()
        if record is None:
            abort(404)
        return render_template(
            "review.html",
            record=record,
            grouped=importer.issues_for(db(), import_id),
            blocking=importer.blocking_count(db(), import_id),
            rows=db().execute(
                "SELECT * FROM staged_rows WHERE import_id = ? ORDER BY line_no LIMIT 200",
                (import_id,),
            ).fetchall(),
            blocking_codes=importer.BLOCKING,
        )

    @app.post("/imports/<int:import_id>/rows/<int:row_id>")
    def resolve_row(import_id: int, row_id: int):
        import json as _json

        action = request.form.get("do", "resolve")
        row = db().execute(
            "SELECT * FROM staged_rows WHERE id = ? AND import_id = ?",
            (row_id, import_id),
        ).fetchone()
        if row is None:
            abort(404)

        with transaction(db()):
            if action == "skip":
                db().execute(
                    "UPDATE staged_rows SET state = 'skipped' WHERE id = ?", (row_id,)
                )
            else:
                resolution = _json.loads(row["resolution"] or "{}")
                for key in ("set_code", "identity", "mtgjson_uuid", "product_name"):
                    if request.form.get(key):
                        resolution[key] = request.form[key]
                db().execute(
                    "UPDATE staged_rows SET resolution = ?, state = 'resolved' WHERE id = ?",
                    (_json.dumps(resolution), row_id),
                )
        return redirect(url_for("review", import_id=import_id))

    @app.post("/imports/<int:import_id>/commit")
    def commit(import_id: int):
        try:
            with transaction(db()):
                result = importer.commit_import(db(), import_id)
        except (ValueError, LookupError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("review", import_id=import_id))
        flash(
            f"Committed — {result['added']} new, {result['updated']} updated. "
            f"Undo is available.",
            "ok",
        )
        return redirect(url_for("collection"))

    @app.post("/imports/<int:import_id>/discard")
    def discard(import_id: int):
        with transaction(db()):
            importer.discard_import(db(), import_id)
        flash("Discarded. Your collection was never touched.", "ok")
        return redirect(url_for("imports"))

    # --- history --------------------------------------------------------------

    @app.route("/history")
    def history():
        return render_template("history.html", operations=ops.recent(db(), 50))

    @app.post("/undo")
    def undo():
        try:
            with transaction(db()):
                operation = ops.undo(db())
        except LookupError as exc:
            flash(str(exc), "error")
        else:
            flash(f"Undone: {operation.summary}", "ok")
        return redirect(request.form.get("back") or url_for("collection"))

    @app.errorhandler(413)
    def too_big(_):
        return (
            render_template("error.html", message=f"That file is over {MAX_UPLOAD_MB} MB."),
            413,
        )


def _filters_from(source) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in repo.FILTERS:
        value = source.get(key)
        if value not in (None, ""):
            out[key] = value
    return out


def serve(
    db_path: Optional[str] = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    debug: bool = False,
) -> None:
    """Run the local server.

    `host` defaults to loopback and the CLI does not expose a flag to change it.
    Binding 0.0.0.0 would put an unauthenticated view of the collection on every
    network the machine joins.
    """
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(
            f"refusing to bind {host!r}: this app has no authentication and is "
            f"meant for loopback only"
        )
    app = create_app(db_path)
    print(f"Collection manager on http://{host}:{port}  (database: {app.config['DATABASE']})")
    app.run(host=host, port=port, debug=debug)
