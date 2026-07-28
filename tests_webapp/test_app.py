"""HTTP layer: routing, CSRF, uploads, and the loopback guarantee."""

from __future__ import annotations

import io
import os
import re
import unittest

from webapp.app import create_app, serve

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(REPO, "tests", "fixtures")
SAMPLE = os.path.join(FIXTURES, "sample.csv")


def read(path):
    with open(path, "rb") as handle:
        return handle.read()


class Base(unittest.TestCase):
    def setUp(self):
        self.app = create_app(":memory:", testing=True)
        self.client = self.app.test_client()

    def token(self, path="/"):
        html = self.client.get(path).get_data(as_text=True)
        match = re.search(r'name="csrf" value="([^"]+)"', html)
        self.assertIsNotNone(match, f"no CSRF token on {path}")
        return match.group(1)

    def upload(self, path=SAMPLE, name="sample.csv"):
        return self.client.post(
            "/imports",
            data={"csrf": self.token("/imports"), "file": (io.BytesIO(read(path)), name)},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

    def commit_latest(self):
        html = self.upload().get_data(as_text=True)
        import_id = re.search(r"/imports/(\d+)/commit", html).group(1)
        return self.client.post(
            f"/imports/{import_id}/commit",
            data={"csrf": self.token(f"/imports/{import_id}")},
            follow_redirects=True,
        )


class TestRoutes(Base):
    def test_pages_render(self):
        for path in ("/", "/imports", "/history"):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_empty_collection_points_at_import(self):
        self.assertIn("Import a CSV", self.client.get("/").get_data(as_text=True))

    def test_missing_import_is_404(self):
        self.assertEqual(self.client.get("/imports/999").status_code, 404)


class TestCsrf(Base):
    def test_mutating_routes_require_a_token(self):
        for path in ("/bulk", "/undo", "/imports"):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, data={}).status_code, 400)

    def test_a_wrong_token_is_refused(self):
        self.assertEqual(
            self.client.post("/bulk", data={"csrf": "nope"}).status_code, 400
        )


class TestImportFlow(Base):
    def test_upload_stages_without_touching_the_collection(self):
        body = self.upload().get_data(as_text=True)
        self.assertIn("unchanged so far", body)
        self.assertIn("Import a CSV", self.client.get("/").get_data(as_text=True))

    def test_commit_populates_the_collection(self):
        body = self.commit_latest().get_data(as_text=True)
        self.assertIn("$431.57", body)

    def test_duplicate_upload_is_refused(self):
        self.commit_latest()
        self.assertIn("already imported", self.upload().get_data(as_text=True))

    def test_unrecognized_file_is_reported(self):
        data = {"csrf": self.token("/imports"),
                "file": (io.BytesIO(b"a,b,c\n1,2,3\n"), "weird.csv")}
        body = self.client.post("/imports", data=data,
                                content_type="multipart/form-data",
                                follow_redirects=True).get_data(as_text=True)
        self.assertIn("Couldn", body)

    def test_upload_with_no_file_is_handled(self):
        body = self.client.post("/imports", data={"csrf": self.token("/imports")},
                                follow_redirects=True).get_data(as_text=True)
        self.assertIn("Choose a CSV", body)


class TestBulkOverHttp(Base):
    def setUp(self):
        super().setUp()
        self.commit_latest()

    def ids(self):
        html = self.client.get("/").get_data(as_text=True)
        return re.findall(r'name="ids" value="(\d+)"', html)

    def test_bulk_verdict_then_undo(self):
        self.client.post("/bulk", data={
            "csrf": self.token("/"), "action": "verdict", "value": "sell",
            "ids": self.ids()[:3], "back": "/"}, follow_redirects=True)
        self.assertEqual(self.client.get("/").get_data(as_text=True).count("pill sell"), 3)

        self.client.post("/undo", data={"csrf": self.token("/"), "back": "/"},
                         follow_redirects=True)
        self.assertEqual(self.client.get("/").get_data(as_text=True).count("pill sell"), 0)

    def test_select_all_matching_honours_the_filter(self):
        self.client.post("/bulk", data={
            "csrf": self.token("/?price_min=10"), "action": "verdict", "value": "keep",
            "select_all": "1", "price_min": "10", "back": "/"}, follow_redirects=True)
        html = self.client.get("/?verdict=keep").get_data(as_text=True)
        self.assertEqual(html.count("pill keep"), 3)

    def test_a_bad_bulk_request_reports_instead_of_crashing(self):
        body = self.client.post("/bulk", data={
            "csrf": self.token("/"), "action": "verdict", "value": "sell",
            "back": "/"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Nothing was selected", body)

    def test_undo_button_appears_only_when_there_is_something_to_undo(self):
        fresh = create_app(":memory:", testing=True).test_client()
        self.assertNotIn("Undo:", fresh.get("/").get_data(as_text=True))
        self.assertIn("Undo:", self.client.get("/").get_data(as_text=True))


class TestLoopbackOnly(unittest.TestCase):
    def test_serve_refuses_a_routable_bind(self):
        """No auth means loopback only. This is the part people skip."""
        for host in ("0.0.0.0", "192.168.1.10", ""):
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    serve(host=host)

    def test_loopback_is_the_default(self):
        import inspect

        self.assertEqual(
            inspect.signature(serve).parameters["host"].default, "127.0.0.1"
        )


if __name__ == "__main__":
    unittest.main()


class TestThreading(unittest.TestCase):
    """The bug that shipped: one connection shared across threads.

    Flask's dev server handles requests on worker threads, so a connection
    opened on the main thread raises `sqlite3.ProgrammingError` on the very
    first request. Every one of the 53 tests passed anyway, because a test
    client runs in the calling thread — the suite and the server exercised
    different code paths.

    These tests close that gap: they drive the app from other threads, and
    against a real file database rather than only an in-memory one.
    """

    def test_requests_from_other_threads_succeed(self):
        import threading

        app = create_app(":memory:", testing=True)
        results = []

        def hit():
            results.append(app.test_client().get("/").status_code)

        threads = [threading.Thread(target=hit) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(results, [200] * 4, "a worker thread could not read the db")

    def test_a_write_from_one_thread_is_visible_to_another(self):
        """Per-thread connections must still share one database.

        A plain `:memory:` would give each thread its own empty one, so this
        also guards the shared-cache URI that makes memory behave like a file.
        """
        import threading

        app = create_app(":memory:", testing=True)
        client = app.test_client()
        html = client.get("/imports").get_data(as_text=True)
        token = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
        client.post(
            "/imports",
            data={"csrf": token, "file": (io.BytesIO(read(SAMPLE)), "sample.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        seen = []

        def look():
            seen.append("sample.csv" in app.test_client().get("/imports").get_data(as_text=True))

        thread = threading.Thread(target=look)
        thread.start()
        thread.join()
        self.assertEqual(seen, [True], "another thread saw a different database")

    def test_a_file_backed_database_works_end_to_end(self):
        """The suite ran only against memory; the server runs against a file."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "collection.db")
            app = create_app(path, testing=True)
            client = app.test_client()

            html = client.get("/imports").get_data(as_text=True)
            token = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
            body = client.post(
                "/imports",
                data={"csrf": token, "file": (io.BytesIO(read(SAMPLE)), "sample.csv")},
                content_type="multipart/form-data",
                follow_redirects=True,
            ).get_data(as_text=True)
            import_id = re.search(r"/imports/(\d+)/commit", body).group(1)

            token = re.search(
                r'name="csrf" value="([^"]+)"',
                client.get(f"/imports/{import_id}").get_data(as_text=True),
            ).group(1)
            committed = client.post(
                f"/imports/{import_id}/commit",
                data={"csrf": token},
                follow_redirects=True,
            ).get_data(as_text=True)

            self.assertIn("$431.57", committed)
            self.assertTrue(os.path.exists(path))

            # A brand-new app over the same file must see the same data — the
            # actual point of persisting.
            again = create_app(path, testing=True).test_client()
            self.assertIn("$431.57", again.get("/").get_data(as_text=True))

    def test_serve_binds_loopback_with_threading(self):
        """`serve` must not quietly run single-threaded to dodge the issue."""
        import inspect

        source = inspect.getsource(serve)
        self.assertIn("127.0.0.1", source)
        self.assertNotIn("threaded=False", source)
