"""A corrupt per-run record must not poison unrelated jobs during recovery."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import web_app as web
from scripts.flayr_core.run_state import (
    COMPLETED,
    DEGRADED,
    FAILED,
    PROCESSING,
    read_run_state,
    recover_run_state,
    reset_run_state,
    transition_run_state,
)


class WebRecoveryIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        environment = mock.patch.dict(web.os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = web.JobStore(self.root / "store")
        self.addCleanup(self.store._executor.shutdown, wait=True, cancel_futures=True)
        self.bad = self.make_job()
        self.good = self.make_job()

    def make_job(self) -> str:
        files = {}
        for role in ("benchmark_video", "creator_video"):
            path = self.root / f"{len(self.store.jobs)}-{role}.mp4"
            path.write_bytes(b"synthetic input only")
            files[role] = {"path": path, "filename": path.name}
        with mock.patch.object(self.store._executor, "submit"):
            result = self.store.create({}, files, owner_id="owner-a")
        job = self.store.jobs[result["id"]]
        run = Path(job["run_dir"])
        recover_run_state(
            run, DEGRADED, job_id=job["id"], reason="Synthetic terminal draft"
        )
        (run / "analysis.json").write_text(
            json.dumps({"analysis_run_state": "degraded"}), encoding="utf-8"
        )
        (run / "degraded_manifest.json").write_text(
            json.dumps(
                {
                    "analysis_run_state": "degraded",
                    "degraded_flags": [],
                    "reason": "Synthetic draft",
                }
            ),
            encoding="utf-8",
        )
        for name in ("bd_report.html", "creator_report.html"):
            (run / name).write_text(
                "<html><body>Synthetic report</body></html>", encoding="utf-8"
            )
        job["status"] = "degraded"
        return job["id"]

    def run_dir(self, job_id: str) -> Path:
        return Path(self.store.jobs[job_id]["run_dir"])

    def restart(self) -> web.JobStore:
        self.store._persist_locked()
        with mock.patch.object(web.subprocess, "Popen") as launch:
            recovered = web.JobStore(self.store.root)
        self.addCleanup(recovered._executor.shutdown, wait=True, cancel_futures=True)
        launch.assert_not_called()
        return recovered

    def assert_isolated(self, recovered: web.JobStore) -> None:
        self.assertEqual(recovered.jobs[self.bad]["status"], "failed")
        self.assertEqual(recovered.jobs[self.good]["status"], "degraded")
        public = recovered.public(recovered.jobs[self.good])
        self.assertTrue(public["creator_report_url"])
        self.assertEqual(read_run_state(self.run_dir(self.good))["state"], DEGRADED)
        saved = json.loads(recovered.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved[self.bad]["status"], "failed")
        self.assertEqual(saved[self.good]["status"], "degraded")

    def test_corrupt_lifecycle_is_isolated_without_rewriting_source(self) -> None:
        state = self.run_dir(self.bad) / "run_state.json"
        for data in (
            b"\xffinvalid",
            b'{"schema_version":1,"state":[],"history":[]}',
            b'{"schema_version":1,"state":{},"history":[]}',
        ):
            with self.subTest(data=data):
                state.write_bytes(data)
                recovered = self.restart()
                self.assert_isolated(recovered)
                self.assertEqual(state.read_bytes(), data)

    def test_corrupt_manifest_is_rejected_not_used_to_publish(self) -> None:
        run = self.run_dir(self.bad)
        reset_run_state(run, job_id=self.bad)
        recover_run_state(run, COMPLETED, job_id=self.bad)
        self.store.jobs[self.bad]["status"] = "completed"
        manifest = run / "_SUCCESS.json"
        for data in (
            b"\xffinvalid",
            b'{"schema_version":[]}',
            b'{"schema_version":{}}',
        ):
            with self.subTest(data=data):
                manifest.write_bytes(data)
                self.assertEqual(
                    web._publication_status(run, {}, recovery=True), "failed"
                )
                recovered = self.restart()
                self.assert_isolated(recovered)
                self.assertEqual(manifest.read_bytes(), data)

    def test_missing_index_rebuild_survives_one_corrupt_run(self) -> None:
        self.store._persist_locked()
        state = self.run_dir(self.bad) / "run_state.json"
        data = b"\xffinvalid"
        state.write_bytes(data)
        self.store.state_path.unlink()
        self.store.state_backup_path.unlink(missing_ok=True)
        with mock.patch.object(web.subprocess, "Popen") as launch:
            recovered = web.JobStore(self.store.root)
        self.addCleanup(recovered._executor.shutdown, wait=True, cancel_futures=True)
        launch.assert_not_called()
        self.assert_isolated(recovered)
        self.assertEqual(state.read_bytes(), data)
        self.assertTrue(recovered.recovery_warning)

    def test_lifecycle_write_failure_does_not_skip_remaining_jobs(self) -> None:
        run = self.run_dir(self.bad)
        reset_run_state(run, job_id=self.bad)
        transition_run_state(run, PROCESSING)
        self.store.jobs[self.bad]["status"] = "running"
        original = (run / "run_state.json").read_bytes()
        pending = self.make_job()
        reset_run_state(self.run_dir(pending), job_id=pending)
        transition_run_state(self.run_dir(pending), PROCESSING)
        self.store.jobs[pending]["status"] = "running"

        def fail_one(path, *args, **kwargs):
            if path == run:
                raise PermissionError("synthetic lifecycle write denial")
            return recover_run_state(path, *args, **kwargs)

        with mock.patch.object(web, "recover_run_state", side_effect=fail_one):
            recovered = self.restart()
        self.assert_isolated(recovered)
        self.assertEqual((run / "run_state.json").read_bytes(), original)
        self.assertEqual(recovered.jobs[pending]["status"], "failed")
        self.assertEqual(read_run_state(self.run_dir(pending))["state"], FAILED)
        self.assertTrue(recovered.recovery_warning)

    def test_identity_mismatch_fails_only_that_recovery(self) -> None:
        state = self.run_dir(self.bad) / "run_state.json"
        data = json.loads(state.read_text(encoding="utf-8"))
        data["job_id"] = "different-job"
        state.write_text(json.dumps(data), encoding="utf-8")
        original = state.read_bytes()
        self.store.jobs[self.bad]["status"] = "running"
        recovered = self.restart()
        self.assert_isolated(recovered)
        self.assertEqual(state.read_bytes(), original)
        self.assertTrue(recovered.recovery_warning)

    def test_report_lookup_with_corrupt_lifecycle_is_not_a_server_error(self) -> None:
        (self.run_dir(self.bad) / "run_state.json").write_bytes(b"\xffinvalid")
        public = self.store.public(self.store.jobs[self.bad])
        self.assertFalse(public["creator_report_url"])
        self.assertFalse(
            web._job_reports_published(
                self.store.jobs[self.bad], self.run_dir(self.bad)
            )
        )

    def test_global_index_write_errors_are_not_suppressed(self) -> None:
        self.store.jobs[self.bad]["status"] = "running"
        self.store._persist_locked()
        with mock.patch.object(
            web, "_atomic_write_bytes", side_effect=OSError("synthetic index denial")
        ), self.assertRaises(OSError):
            web.JobStore(self.store.root)

    def test_programming_errors_in_validator_still_propagate(self) -> None:
        run = self.run_dir(self.bad)
        reset_run_state(run, job_id=self.bad)
        recover_run_state(run, COMPLETED, job_id=self.bad)
        with mock.patch.object(
            web,
            "validate_success_manifest",
            side_effect=RuntimeError("synthetic programming fault"),
        ), self.assertRaises(RuntimeError):
            web._publication_status(run, {}, recovery=True)


if __name__ == "__main__":
    unittest.main()
