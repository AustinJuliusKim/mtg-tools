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
