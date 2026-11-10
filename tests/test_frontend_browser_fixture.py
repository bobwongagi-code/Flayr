from __future__ import annotations

import http.client
import json
import threading
import unittest
from unittest import mock

from tests.frontend_browser_fixture import JOBS, FixtureServer


class FrontendBrowserFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = FixtureServer()
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())

    def request(self, path: str, method: str = "GET") -> tuple[int, bytes]:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_only_synthetic_read_only_routes_and_allowlisted_frontend(self) -> None:
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        status, data = self.request("/api/jobs")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["jobs"], JOBS)
        self.assertIn(b'type="module" src="app.js"', self.request("/")[1])
        self.assertIn(b"loadReportAudience", self.request("/app.js")[1])
        for path in (
            "/../.env",
            "/.env",
            "/HANDOFF.MD",
            "/runs/private.json",
            "/reports/unknown/creator.html",
        ):
            self.assertEqual(self.request(path)[0], 404, path)
        for method in ("POST", "PUT", "DELETE"):
            self.assertEqual(self.request("/api/jobs", method)[0], 405)

    def test_delays_rejection_and_head_body_match_browser_scenarios(self) -> None:
        self.server.report_delay = 0.01
        with mock.patch("tests.frontend_browser_fixture.time.sleep") as sleep:
            self.assertEqual(
                self.request("/reports/slow-head/creator.html", "HEAD"), (200, b"")
            )
            self.assertIn(
                b"SYNTHETIC", self.request("/reports/slow-frame/creator.html")[1]
            )
            self.assertEqual(sleep.call_count, 2)
            self.request("/reports/slow-head/creator.html")
            self.request("/reports/slow-frame/creator.html", "HEAD")
            self.assertEqual(sleep.call_count, 2)
        self.assertEqual(self.request("/reports/rejected/creator.html", "HEAD")[0], 503)
        self.assertEqual(self.request("/reports/ready/internal.html")[0], 200)
        self.assertEqual(self.request("/api/jobs/missing")[0], 404)


if __name__ == "__main__":
    unittest.main()
