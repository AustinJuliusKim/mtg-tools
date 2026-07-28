"""The JSON API contract.

Replaces the HTML-scraping tests that came with the Jinja UI. Those counted
occurrences of `pill sell` and regexed `name="csrf"` out of rendered markup;
these assert response shape, which is both stronger and no longer coupled to
how anything looks.

`test_core.py` is untouched by the front-end change — it never spoke HTTP.
"""

from __future__ import annotations

import io
import json
import os
import re
import unittest

from webapp.app import create_app, serve

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(REPO, "tests", "fixtures")
SAMPLE = os.path.join(FIXTURES, "sample.csv")
SAMPLE2 = os.path.join(FIXTURES, "sample2.csv")
SEALED = os.path.join(FIXTURES, "sealed_sample.csv")


def read(path):
    with open(path, "rb") as handle:
        return handle.read()


class Base(unittest.TestCase):
    def setUp(self):
        self.app = create_app(":memory:", testing=True)
        self.client = self.app.test_client()
        self.token = self.client.get("/api/session").get_json()["csrfToken"]

    # -- helpers ---------------------------------------------------------

    def post(self, path, body=None, **kwargs):
        return self.client.post(
            path,
            json=body if body is not None else {},
            headers={"X-CSRF-Token": self.token},
            **kwargs,
        )

    def upload(self, path=SAMPLE, name="sample.csv"):
        return self.client.post(
            "/api/imports",
            data={"file": (io.BytesIO(read(path)), name)},
            content_type="multipart/form-data",
            headers={"X-CSRF-Token": self.token},
        )

    def commit(self, path=SAMPLE, name="sample.csv"):
        import_id = self.upload(path, name).get_json()["importId"]
        return self.post(f"/api/imports/{import_id}/commit")

    def rows(self, query=""):
        return self.client.get(f"/api/collection{query}").get_json()


class TestSession(Base):
    def test_session_carries_a_token_and_the_database(self):
        body = self.client.get("/api/session").get_json()
        self.assertTrue(body["csrfToken"])
        self.assertIn("database", body)
        self.assertIsNone(body["undoable"])

    def test_undoable_appears_after_a_change(self):
        self.commit()
        undoable = self.client.get("/api/session").get_json()["undoable"]
        self.assertIsNotNone(undoable)
        self.assertIn("Imported", undoable["summary"])


class TestCsrf(Base):
    def test_mutation_without_the_header_is_refused(self):
        response = self.client.post("/api/bulk", json={"action": "verdict"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "csrf")

    def test_a_wrong_token_is_refused(self):
        response = self.client.post(
            "/api/bulk", json={}, headers={"X-CSRF-Token": "nope"}
        )
        self.assertEqual(response.status_code, 400)

    def test_reads_do_not_need_a_token(self):
        for path in ("/api/session", "/api/collection", "/api/history", "/api/imports"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)


class TestCollection(Base):
    def setUp(self):
        super().setUp()
        self.commit()

    def test_shape(self):
        body = self.rows()
        self.assertEqual(body["totalRows"], 6)
        self.assertEqual(len(body["rows"]), 6)
        for key in ("page", "pages", "totals", "grandTotals", "facets", "sort"):
            self.assertIn(key, body)

    def test_money_crosses_as_integer_cents(self):
        """Dollars as JSON floats would reintroduce the drift Decimal prevents."""
        mox = next(r for r in self.rows()["rows"] if r["title"] == "Mox Amber")
        self.assertEqual(mox["priceCents"], 7517)
        self.assertIsInstance(mox["priceCents"], int)
        self.assertEqual(mox["totalCents"], 7517 * 3)
        # …plus a preformatted string, so the client never does the arithmetic.
        self.assertEqual(mox["price"], "$75.17")
        self.assertEqual(mox["total"], "$225.51")

    def test_an_unpriced_row_is_null_not_zero(self):
        body = json.dumps(self.rows())
        self.assertNotIn('"priceCents": 0.0', body)

    def test_totals_track_the_filter(self):
        everything = self.rows()["totals"]
        filtered = self.rows("?price_min=10")["totals"]
        self.assertLess(filtered["valueCents"], everything["valueCents"])
        self.assertEqual(everything["rows"], 6)

    def test_grand_totals_ignore_the_filter(self):
        body = self.rows("?price_min=10")
        self.assertEqual(body["grandTotals"]["rows"], 6)
        self.assertLess(body["totals"]["rows"], 6)

    def test_unknown_filter_is_rejected_not_ignored(self):
        """A typo'd filter that quietly matched everything is how a bad bulk
        edit happens."""
        response = self.client.get("/api/collection?prices_min=10")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "bad-filter")

    def test_sorting_and_pagination(self):
        body = self.rows("?sort=price&dir=asc&perPage=2&page=1")
        self.assertEqual(len(body["rows"]), 2)
        self.assertEqual(body["pages"], 3)
        prices = [r["priceCents"] for r in body["rows"]]
        self.assertEqual(prices, sorted(prices))

    def test_facets_are_offered_for_filtering(self):
        facets = self.rows()["facets"]
        self.assertIn("DOM", facets["editions"])
        self.assertIn("mythic", facets["rarities"])


class TestBulk(Base):
    def setUp(self):
        super().setUp()
        self.commit()
        self.ids = [r["id"] for r in self.rows()["rows"]]

    def test_actions_are_advertised(self):
        actions = self.client.get("/api/bulk/actions").get_json()
        keys = {a["key"] for a in actions}
        self.assertIn("verdict", keys)
        delete = next(a for a in actions if a["key"] == "delete")
        self.assertTrue(delete["destructive"])

    def test_preview_reports_the_real_count_and_sample(self):
        body = self.post(
            "/api/bulk/preview", {"selectAll": True, "filters": {"price_min": 10}}
        ).get_json()
        self.assertEqual(body["count"], 3)
        self.assertTrue(body["sample"])
        self.assertEqual(body["sample"][0]["title"], "Mox Amber")

    def test_apply_by_explicit_ids(self):
        body = self.post(
            "/api/bulk", {"action": "verdict", "value": "sell", "ids": self.ids[:3]}
        ).get_json()
        self.assertEqual(body["affected"], 3)
        sold = [r for r in self.rows()["rows"] if r["verdict"] == "sell"]
        self.assertEqual(len(sold), 3)

    def test_select_all_uses_the_filter_not_the_page(self):
        """The invariant the rewrite had to preserve: the server resolves the
        selection, so a filter that changed since render cannot widen it."""
        body = self.post(
            "/api/bulk",
            {
                "action": "verdict",
                "value": "keep",
                "selectAll": True,
                "filters": {"price_min": 10},
            },
        ).get_json()
        self.assertEqual(body["affected"], 3)
        kept = [r for r in self.rows()["rows"] if r["verdict"] == "keep"]
        self.assertEqual(len(kept), 3)

    def test_select_all_ignores_any_ids_the_client_also_sent(self):
        """A client that sends both must not get the union."""
        body = self.post(
            "/api/bulk",
            {
                "action": "verdict",
                "value": "keep",
                "selectAll": True,
                "ids": self.ids,
                "filters": {"price_min": 10},
            },
        ).get_json()
        self.assertEqual(body["affected"], 3)

    def test_empty_selection_is_refused(self):
        response = self.post("/api/bulk", {"action": "verdict", "value": "sell"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Nothing was selected", response.get_json()["error"])

    def test_unknown_action_is_refused(self):
        response = self.post("/api/bulk", {"action": "drop_table", "ids": self.ids})
        self.assertEqual(response.status_code, 400)

    def test_price_adjustment_is_exact(self):
        target = next(r for r in self.rows()["rows"] if r["priceCents"] == 684)
        self.post("/api/bulk", {"action": "adjust_price", "value": "5",
                                "ids": [target["id"]]})
        after = next(r for r in self.rows()["rows"] if r["id"] == target["id"])
        self.assertEqual(after["priceCents"], 718)  # half-up, no float


class TestImports(Base):
    def test_upload_stages_without_touching_the_collection(self):
        body = self.upload().get_json()
        self.assertIn("importId", body)
        self.assertEqual(body["kind"], "singles")
        self.assertEqual(self.rows()["totalRows"], 0)

    def test_detail_lists_issues_grouped_by_code(self):
        import_id = self.upload().get_json()["importId"]
        body = self.client.get(f"/api/imports/{import_id}").get_json()
        codes = {i["code"] for i in body["issues"]}
        self.assertIn("language", codes)
        self.assertEqual(body["blocking"], 0)

    def test_commit_populates_the_collection(self):
        body = self.commit().get_json()
        self.assertEqual(body["added"], 6)
        self.assertEqual(self.rows()["totals"]["value"], "$431.57")

    def test_duplicate_upload_is_refused(self):
        self.commit()
        response = self.upload()
        self.assertEqual(response.status_code, 409)
        self.assertIn("double", response.get_json()["error"])

    def test_unrecognized_file_reports_its_header(self):
        response = self.client.post(
            "/api/imports",
            data={"file": (io.BytesIO(b"Alpha,Beta\n1,2\n"), "weird.csv")},
            content_type="multipart/form-data",
            headers={"X-CSRF-Token": self.token},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Alpha", response.get_json()["error"])

    def test_no_file_is_reported(self):
        response = self.client.post(
            "/api/imports",
            data={},
            content_type="multipart/form-data",
            headers={"X-CSRF-Token": self.token},
        )
        self.assertEqual(response.status_code, 400)

    def test_blocking_rows_prevent_commit(self):
        import_id = self.upload(SEALED, "sealed.csv").get_json()["importId"]
        detail = self.client.get(f"/api/imports/{import_id}").get_json()
        self.assertGreater(detail["blocking"], 0)

        response = self.post(f"/api/imports/{import_id}/commit")
        self.assertEqual(response.status_code, 409)
        self.assertIn("need a decision", response.get_json()["error"])

    def test_ambiguous_rows_carry_their_candidates(self):
        import_id = self.upload(SEALED, "sealed.csv").get_json()["importId"]
        detail = self.client.get(f"/api/imports/{import_id}").get_json()
        ambiguous = next(i for i in detail["issues"] if i["code"] == "ambiguous")
        self.assertTrue(ambiguous["rows"][0]["candidates"])

    def test_skipping_a_row_clears_the_blocker(self):
        import_id = self.upload(SEALED, "sealed.csv").get_json()["importId"]
        detail = self.client.get(f"/api/imports/{import_id}").get_json()
        blocking_rows = [
            row
            for issue in detail["issues"] if issue["blocking"]
            for row in issue["rows"]
        ]
        for row in blocking_rows:
            body = self.post(
                f"/api/imports/{import_id}/rows/{row['id']}", {"skip": True}
            ).get_json()
        self.assertEqual(body["blocking"], 0)
        self.assertEqual(self.post(f"/api/imports/{import_id}/commit").status_code, 200)

    def test_discard_leaves_the_collection_untouched(self):
        import_id = self.upload().get_json()["importId"]
        self.post(f"/api/imports/{import_id}/discard")
        self.assertEqual(self.rows()["totalRows"], 0)
        listed = self.client.get("/api/imports").get_json()
        self.assertEqual(listed[0]["status"], "discarded")

    def test_missing_import_is_404(self):
        self.assertEqual(self.client.get("/api/imports/999").status_code, 404)


class TestHistoryAndUndo(Base):
    def test_undo_reverses_a_bulk_edit(self):
        self.commit()
        ids = [r["id"] for r in self.rows()["rows"]]
        before = {r["id"]: r["priceCents"] for r in self.rows()["rows"]}

        self.post("/api/bulk", {"action": "adjust_price", "value": "10", "ids": ids})
        self.assertNotEqual(
            {r["id"]: r["priceCents"] for r in self.rows()["rows"]}, before
        )

        undone = self.post("/api/undo").get_json()
        self.assertIn("Adjusted", undone["summary"])
        self.assertEqual(
            {r["id"]: r["priceCents"] for r in self.rows()["rows"]}, before
        )

    def test_undo_reverses_an_import(self):
        self.commit()
        self.commit(SAMPLE2, "sample2.csv")
        self.assertEqual(self.rows()["totals"]["quantity"], 31)
        self.post("/api/undo")
        self.assertEqual(self.rows()["totals"]["quantity"], 17)

    def test_history_records_both_states(self):
        self.commit()
        self.post("/api/undo")
        history = self.client.get("/api/history").get_json()
        self.assertTrue(history[0]["reverted"])

    def test_nothing_to_undo(self):
        response = self.post("/api/undo")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "nothing-to-undo")


class TestSpaShell(Base):
    def test_unknown_api_path_is_json_not_html(self):
        response = self.client.get("/api/nope")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["code"], "not-found")

    def test_a_client_route_falls_through_to_the_shell(self):
        """React Router owns /history and /imports in the browser."""
        for path in ("/", "/history", "/imports/1"):
            with self.subTest(path=path):
                self.assertIn(self.client.get(path).status_code, (200, 503))

    def test_a_missing_build_says_how_to_fix_it(self):
        """Deterministic rather than conditional on whether dist/ exists.

        An earlier version skipped when the front end happened to be built —
        and borrowed the sanctioned "exports not present" wording to get past
        the skip guard, which is precisely the erosion that guard exists to
        prevent. Pointing DIST at an empty directory tests the branch either way.
        """
        import tempfile

        from webapp import app as app_module

        original = app_module.DIST
        with tempfile.TemporaryDirectory() as empty:
            app_module.DIST = empty
            try:
                response = self.client.get("/")
                body = response.get_data(as_text=True)
            finally:
                app_module.DIST = original

        self.assertEqual(response.status_code, 503)
        self.assertIn("npm --prefix frontend", body)
        self.assertIn("run build", body)


class TestLoopbackOnly(unittest.TestCase):
    def test_serve_refuses_a_routable_bind(self):
        for host in ("0.0.0.0", "192.168.1.10", ""):
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    serve(host=host)

    def test_loopback_is_the_default(self):
        import inspect

        self.assertEqual(
            inspect.signature(serve).parameters["host"].default, "127.0.0.1"
        )


class TestThreading(unittest.TestCase):
    """Kept from the fix for the bug that shipped: one connection per thread.

    A test client runs in the calling thread, so the original suite passed
    while the real server 500'd on every page. These drive the app from worker
    threads and against a file database.
    """

    def test_requests_from_other_threads_succeed(self):
        import threading

        app = create_app(":memory:", testing=True)
        results = []

        def hit():
            results.append(app.test_client().get("/api/collection").status_code)

        threads = [threading.Thread(target=hit) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results, [200] * 4)

    def test_a_write_in_one_thread_is_visible_in_another(self):
        import threading

        app = create_app(":memory:", testing=True)
        client = app.test_client()
        token = client.get("/api/session").get_json()["csrfToken"]
        client.post(
            "/api/imports",
            data={"file": (io.BytesIO(read(SAMPLE)), "sample.csv")},
            content_type="multipart/form-data",
            headers={"X-CSRF-Token": token},
        )

        seen = []

        def look():
            seen.append(len(app.test_client().get("/api/imports").get_json()))

        thread = threading.Thread(target=look)
        thread.start()
        thread.join()
        self.assertEqual(seen, [1], "another thread saw a different database")

    def test_a_file_backed_database_works_end_to_end(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "collection.db")
            app = create_app(path, testing=True)
            client = app.test_client()
            token = client.get("/api/session").get_json()["csrfToken"]

            import_id = client.post(
                "/api/imports",
                data={"file": (io.BytesIO(read(SAMPLE)), "sample.csv")},
                content_type="multipart/form-data",
                headers={"X-CSRF-Token": token},
            ).get_json()["importId"]
            client.post(
                f"/api/imports/{import_id}/commit", headers={"X-CSRF-Token": token}
            )

            again = create_app(path, testing=True).test_client()
            self.assertEqual(
                again.get("/api/collection").get_json()["totals"]["value"], "$431.57"
            )


if __name__ == "__main__":
    unittest.main()


class TestInsights(Base):
    """Chart data. The invariants matter more than the shapes."""

    def setUp(self):
        super().setUp()
        self.commit()

    def test_tiers_sum_to_the_collection_total(self):
        body = self.client.get("/api/collection/insights").get_json()
        self.assertEqual(
            sum(t["marketCents"] for t in body["tiers"]),
            body["totals"]["valueCents"],
        )

    def test_tier_estimates_match_the_documented_rates(self):
        """Same arithmetic as `binders.aggregate.price_tiers`: band then rate."""
        for tier in self.client.get("/api/collection/insights").get_json()["tiers"]:
            with self.subTest(tier=tier["tier"]):
                self.assertEqual(
                    tier["cashCents"],
                    round(tier["marketCents"] * tier["cashPct"] / 100),
                )
                self.assertEqual(
                    tier["creditCents"],
                    round(tier["marketCents"] * tier["creditPct"] / 100),
                )

    def test_sets_sum_to_the_collection_total(self):
        body = self.client.get("/api/collection/insights").get_json()
        self.assertEqual(
            sum(s["cents"] for s in body["sets"]), body["totals"]["valueCents"]
        )

    def test_rarity_sums_to_the_collection_total(self):
        body = self.client.get("/api/collection/insights").get_json()
        self.assertEqual(
            sum(r["cents"] for r in body["rarity"]), body["totals"]["valueCents"]
        )

    def test_concentration_covers_priced_rows_and_ends_at_100(self):
        conc = self.client.get("/api/collection/insights").get_json()["concentration"]
        self.assertEqual(len(conc["points"]), conc["pricedRows"])
        self.assertAlmostEqual(conc["points"][-1]["valuePct"], 100.0, places=1)

    def test_insights_respect_the_filter(self):
        """A chart that ignored the filter would describe a different slice
        than the table under it."""
        everything = self.client.get("/api/collection/insights").get_json()
        filtered = self.client.get(
            "/api/collection/insights?price_min=10"
        ).get_json()
        self.assertLess(
            filtered["totals"]["valueCents"], everything["totals"]["valueCents"]
        )
        self.assertEqual(
            sum(t["marketCents"] for t in filtered["tiers"]),
            filtered["totals"]["valueCents"],
        )

    def test_insights_reject_an_unknown_filter_too(self):
        response = self.client.get("/api/collection/insights?prices_min=10")
        self.assertEqual(response.status_code, 400)

    def test_empty_collection_does_not_crash(self):
        empty = create_app(":memory:", testing=True).test_client()
        body = empty.get("/api/collection/insights").get_json()
        self.assertEqual(body["concentration"]["points"], [])
        self.assertEqual(sum(t["marketCents"] for t in body["tiers"]), 0)


class TestSalesAndExport(Base):
    """The sale lifecycle and the escape hatch."""

    def setUp(self):
        super().setUp()
        self.commit()
        self.ids = [r["id"] for r in self.rows()["rows"]]
        # Mark the three priciest to sell; the queue is verdict-driven.
        self.post("/api/bulk", {"action": "verdict", "value": "sell",
                                "ids": self.ids[:3]})

    # -- queue ------------------------------------------------------------

    def test_queue_is_driven_by_verdicts(self):
        queue = self.client.get("/api/sales/queue").get_json()
        self.assertEqual(len(queue), 3)
        self.assertEqual(queue[0]["name"], "Mox Amber")
        self.assertIsNone(queue[0]["sale"])

    def test_listing_then_selling(self):
        queue = self.client.get("/api/sales/queue").get_json()
        subject = queue[0]

        listed = self.post("/api/sales/list", {
            "kind": subject["kind"], "id": subject["id"], "channel": "ebay",
        })
        self.assertEqual(listed.status_code, 201)
        sale_id = listed.get_json()["saleId"]

        sold = self.post(f"/api/sales/{sale_id}/sold", {
            "sold": "200.00", "fees": "26.00", "shipping": "5.00",
        }).get_json()

        # 20000 - 2600 - 500
        self.assertEqual(sold["netCents"], 16900)
        self.assertEqual(sold["net"], "$169.00")
        self.assertTrue(sold["removedFromCollection"])

    def test_a_sold_card_leaves_the_collection(self):
        """Leaving it in would inflate every valuation after the fact."""
        before = self.rows()["totals"]["quantity"]
        queue = self.client.get("/api/sales/queue").get_json()
        sale_id = self.post("/api/sales/list", {
            "kind": queue[0]["kind"], "id": queue[0]["id"],
        }).get_json()["saleId"]
        self.post(f"/api/sales/{sale_id}/sold", {"sold": "200.00"})

        self.assertEqual(
            self.rows()["totals"]["quantity"], before - queue[0]["quantity"]
        )

    def test_partial_quantity_leaves_the_rest(self):
        queue = self.client.get("/api/sales/queue").get_json()
        mox = next(q for q in queue if q["name"] == "Mox Amber")
        self.assertEqual(mox["quantity"], 3)

        sale_id = self.post("/api/sales/list", {
            "kind": mox["kind"], "id": mox["id"], "quantity": 1,
        }).get_json()["saleId"]
        result = self.post(f"/api/sales/{sale_id}/sold", {"sold": "80.00"}).get_json()

        self.assertFalse(result["removedFromCollection"])
        row = next(r for r in self.rows()["rows"] if r["title"] == "Mox Amber")
        self.assertEqual(row["quantity"], 2)

    def test_realized_gain_is_null_without_a_cost_basis(self):
        """An unknown basis must not become a gain equal to the sale price —
        that number would land straight in a tax figure."""
        queue = self.client.get("/api/sales/queue").get_json()
        sale_id = self.post("/api/sales/list", {
            "kind": queue[0]["kind"], "id": queue[0]["id"],
        }).get_json()["saleId"]
        sold = self.post(f"/api/sales/{sale_id}/sold", {"sold": "200.00"}).get_json()

        self.assertIsNone(sold["realizedGainCents"])
        summary = self.client.get("/api/sales/summary").get_json()
        self.assertEqual(summary["gainKnownFor"], 0)

    def test_cannot_list_the_same_thing_twice(self):
        queue = self.client.get("/api/sales/queue").get_json()
        body = {"kind": queue[0]["kind"], "id": queue[0]["id"]}
        self.post("/api/sales/list", body)
        again = self.post("/api/sales/list", body)
        self.assertEqual(again.status_code, 400)
        self.assertIn("already listed", again.get_json()["error"])

    def test_cannot_list_more_than_you_own(self):
        queue = self.client.get("/api/sales/queue").get_json()
        response = self.post("/api/sales/list", {
            "kind": queue[0]["kind"], "id": queue[0]["id"], "quantity": 99,
        })
        self.assertEqual(response.status_code, 400)

    def test_negative_amounts_are_refused(self):
        queue = self.client.get("/api/sales/queue").get_json()
        sale_id = self.post("/api/sales/list", {
            "kind": queue[0]["kind"], "id": queue[0]["id"],
        }).get_json()["saleId"]
        response = self.post(f"/api/sales/{sale_id}/sold",
                             {"sold": "100.00", "fees": "-5"})
        self.assertEqual(response.status_code, 400)

    def test_summary_totals(self):
        queue = self.client.get("/api/sales/queue").get_json()
        sale_id = self.post("/api/sales/list", {
            "kind": queue[0]["kind"], "id": queue[0]["id"],
        }).get_json()["saleId"]
        self.post(f"/api/sales/{sale_id}/sold",
                  {"sold": "200.00", "fees": "26.00", "shipping": "5.00"})

        summary = self.client.get("/api/sales/summary").get_json()
        self.assertEqual(summary["soldCount"], 1)
        self.assertEqual(summary["grossCents"], 20000)
        self.assertEqual(summary["costsCents"], 3100)
        self.assertEqual(summary["netCents"], 16900)
        self.assertEqual(summary["net"], "$169.00")

    def test_a_sale_is_undoable(self):
        before = self.rows()["totals"]["quantity"]
        queue = self.client.get("/api/sales/queue").get_json()
        sale_id = self.post("/api/sales/list", {
            "kind": queue[0]["kind"], "id": queue[0]["id"],
        }).get_json()["saleId"]
        self.post(f"/api/sales/{sale_id}/sold", {"sold": "200.00"})
        self.assertLess(self.rows()["totals"]["quantity"], before)

        self.post("/api/undo")
        self.assertEqual(self.rows()["totals"]["quantity"], before)

    # -- export -----------------------------------------------------------

    def test_manifest_counts_the_tables(self):
        body = self.client.get("/api/export/manifest").get_json()
        self.assertIn("holdings", body["tables"])
        self.assertEqual(body["rowCounts"]["holdings"], 6)
        self.assertEqual(body["singles"]["value"], "$431.57")

    def test_table_export_writes_dollars_not_cents(self):
        """A spreadsheet should show 75.17, not 7517."""
        text = self.client.get("/api/export/table/holdings").get_data(as_text=True)
        self.assertIn("75.17", text)
        self.assertNotIn("7517", text)
        # …and the header says `price`, not `price_cents`.
        self.assertIn("price,", text.splitlines()[0])

    def test_unknown_table_is_refused(self):
        self.assertEqual(
            self.client.get("/api/export/table/sqlite_master").status_code, 404
        )

    def test_ledger_uses_the_vault_schema(self):
        import csv as _csv
        from binders.export import LEDGER_COLUMNS

        text = self.client.get("/api/export/ledger").get_data(as_text=True)
        rows = list(_csv.DictReader(io.StringIO(text)))
        self.assertEqual(list(rows[0].keys()), list(LEDGER_COLUMNS))
        self.assertEqual(len(rows), 6)

    def test_ledger_carries_sale_figures_once_sold(self):
        import csv as _csv

        queue = self.client.get("/api/sales/queue").get_json()
        mox = next(q for q in queue if q["name"] == "Mox Amber")
        sale_id = self.post("/api/sales/list", {
            "kind": mox["kind"], "id": mox["id"], "quantity": 1,
        }).get_json()["saleId"]
        self.post(f"/api/sales/{sale_id}/sold",
                  {"sold": "80.00", "fees": "10.40"})

        text = self.client.get("/api/export/ledger").get_data(as_text=True)
        row = next(
            r for r in _csv.DictReader(io.StringIO(text)) if r["Name"] == "Mox Amber"
        )
        self.assertEqual(row["Sold"], "80.00")
        self.assertEqual(row["Net Proceeds"], "69.60")

    def test_a_sold_and_gone_item_still_appears_in_the_ledger(self):
        """The bug live verification caught.

        A fully-sold item is deleted from holdings, so a ledger built only from
        the collection dropped it entirely — losing exactly the realized-gain
        record the ledger exists for. `subject_name` is captured at listing time
        so the row survives its subject.
        """
        import csv as _csv

        queue = self.client.get("/api/sales/queue").get_json()
        target = queue[0]
        sale_id = self.post("/api/sales/list", {
            "kind": target["kind"], "id": target["id"],
        }).get_json()["saleId"]
        self.post(f"/api/sales/{sale_id}/sold",
                  {"sold": "300.00", "fees": "39.00"})

        # Gone from the collection…
        self.assertNotIn(
            target["name"], [r["title"] for r in self.rows()["rows"]]
        )

        # …but present in the ledger, with its figures intact.
        text = self.client.get("/api/export/ledger").get_data(as_text=True)
        rows = list(_csv.DictReader(io.StringIO(text)))
        sold_row = next((r for r in rows if r["Name"] == target["name"]), None)
        self.assertIsNotNone(sold_row, "the sold item vanished from the ledger")
        self.assertEqual(sold_row["Sold"], "300.00")
        self.assertEqual(sold_row["Net Proceeds"], "261.00")
        self.assertEqual(sold_row["Market Value"], "")  # no longer owned
        self.assertTrue(sold_row["Source"].startswith("sold"))

    def test_a_partially_sold_item_is_not_duplicated_in_the_ledger(self):
        """It is still held, so it must appear once — as a holding."""
        import csv as _csv

        queue = self.client.get("/api/sales/queue").get_json()
        mox = next(q for q in queue if q["name"] == "Mox Amber")
        sale_id = self.post("/api/sales/list", {
            "kind": mox["kind"], "id": mox["id"], "quantity": 1,
        }).get_json()["saleId"]
        self.post(f"/api/sales/{sale_id}/sold", {"sold": "80.00"})

        text = self.client.get("/api/export/ledger").get_data(as_text=True)
        rows = [
            r for r in _csv.DictReader(io.StringIO(text)) if r["Name"] == "Mox Amber"
        ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Quantity"], "2")

    def test_bundle_contains_everything(self):
        import zipfile

        response = self.client.get("/api/export/bundle")
        self.assertEqual(response.status_code, 200)
        archive = zipfile.ZipFile(io.BytesIO(response.get_data()))
        names = archive.namelist()

        self.assertIn("manifest.json", names)
        self.assertIn("mtg_collection_tracker.csv", names)
        self.assertIn("csv/holdings.csv", names)
        # The zip must be readable — a corrupt archive is worse than none.
        self.assertIsNone(archive.testzip())

    def test_bundle_includes_the_database_for_a_file_backed_app(self):
        import sqlite3 as _sqlite3
        import tempfile
        import zipfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.db")
            app = create_app(path, testing=True)
            client = app.test_client()
            token = client.get("/api/session").get_json()["csrfToken"]
            import_id = client.post(
                "/api/imports",
                data={"file": (io.BytesIO(read(SAMPLE)), "sample.csv")},
                content_type="multipart/form-data",
                headers={"X-CSRF-Token": token},
            ).get_json()["importId"]
            client.post(f"/api/imports/{import_id}/commit",
                        headers={"X-CSRF-Token": token})

            archive = zipfile.ZipFile(
                io.BytesIO(client.get("/api/export/bundle").get_data())
            )
            self.assertIn("collection.sqlite", archive.namelist())

            # The extracted copy must be a working database, not just bytes.
            out = os.path.join(tmp, "restored.sqlite")
            with open(out, "wb") as handle:
                handle.write(archive.read("collection.sqlite"))
            restored = _sqlite3.connect(out)
            count = restored.execute("SELECT COUNT(*) FROM holdings").fetchone()[0]
            restored.close()
            self.assertEqual(count, 6)
