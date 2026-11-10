"""Bad optional analysis metadata must not hide other tasks from the API."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from scripts import web_app as web


class WebProjectionIsolationTests(unittest.TestCase):
    def setUp(self):
        environment = mock.patch.dict(web.os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = web.JobStore(Path(temporary.name).resolve())
        self.addCleanup(self.store._executor.shutdown, wait=True, cancel_futures=True)

    def job(self, job_id, analysis, owner="owner-a"):
        run = self.store.jobs_root / job_id / "run"
        run.mkdir(parents=True)
        (run / "analysis.json").write_bytes(analysis)
        job = {
            "id": job_id,
            "owner_id": owner,
            "workspace_id": "local",
            "status": "failed",
            "run_dir": str(run),
            "created_at": web.utc_now(),
        }
        self.store.jobs[job_id] = job
        return job

    def test_invalid_optional_metadata_falls_back_without_mutating_source_or_state(
        self,
    ):
        for index, payload in enumerate(
            (b"\xffinvalid encoding", b'{"analysis_scope":', b"[]", b"null")
        ):
            with self.subTest(payload=payload):
                job = self.job(f"job-bad-{index}", payload)
                before = dict(job)
                public = self.store.public(job)
                self.assertFalse(public["strategy_level"])
                self.assertEqual(public["status"], "failed")
                self.assertEqual(public["report_url"], "")
                self.assertEqual(public["creator_report_url"], "")
                self.assertEqual(job, before)
                self.assertEqual(
                    (Path(job["run_dir"]) / "analysis.json").read_bytes(), payload
                )

    def test_healthy_strategy_scope_survives_and_does_not_grant_publication(self):
        job = self.job("job-good", b'{"analysis_scope":{"level":"strategy"}}')
        public = self.store.public(job)
        self.assertTrue(public["strategy_level"])
        self.assertEqual(public["status"], "failed")
        self.assertEqual(public["report_url"], "")
        self.assertEqual(public["creator_report_url"], "")

    def test_http_list_and_details_survive_one_corrupt_job_and_keep_owner_scope(self):
        self.job("job-bad", b"\xffinvalid encoding")
        self.job("job-good", b'{"analysis_scope":{"level":"strategy"}}')
        self.job("job-other", b"\xffinvalid private metadata", owner="owner-b")
        server = web.FlayrServer(("127.0.0.1", 0), self.store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        cookie = web._signed_client_cookie("owner-a", server.client_cookie_secret)
        opener = build_opener(ProxyHandler({}))

        def get(path):
            request = Request(
                base + path, headers={"Cookie": f"{web.CLIENT_COOKIE_NAME}={cookie}"}
            )
            return opener.open(request, timeout=3)

        try:
            with get("/api/jobs") as response:
                self.assertEqual(response.status, 200)
                jobs = {job["id"]: job for job in json.load(response)["jobs"]}
            self.assertEqual(set(jobs), {"job-bad", "job-good"})
            self.assertFalse(jobs["job-bad"]["strategy_level"])
            self.assertTrue(jobs["job-good"]["strategy_level"])
            for path in (
                "/api/jobs/job-bad",
                "/api/workspaces/local/jobs/job-bad",
            ):
                with get(path) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.load(response)["id"], "job-bad")
            with self.assertRaises(HTTPError) as caught:
                get("/api/jobs/job-other")
            self.assertEqual(caught.exception.code, 404)
            self.assertNotIn("run_dir", jobs["job-bad"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
