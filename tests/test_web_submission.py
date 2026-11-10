"""Fault-injected job acceptance; no provider or media processing is allowed."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from scripts import web_app as web


class WebSubmissionTests(unittest.TestCase):
    def setUp(self):
        environment = mock.patch.dict(web.os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = web.JobStore(self.root / "store")
        self.addCleanup(self.store._executor.shutdown, wait=True, cancel_futures=True)

    def uploads(self):
        result = {}
        for role in ("benchmark_video", "creator_video"):
            path = self.root / f"{role}.mp4"
            path.write_bytes(f"synthetic {role}; not real video".encode())
            result[role] = {"path": path, "filename": path.name}
        return result

    def assert_failed_without_losing_inputs(self, job):
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["estimated_remaining_seconds"], 0)
        self.assertIn("未启动", job["failure_reason"])
        for role in ("benchmark", "creator"):
            self.assertEqual(
                Path(job[f"{role}_path"]).read_bytes(),
                f"synthetic {role}_video; not real video".encode(),
            )
        public = self.store.public(job)
        self.assertEqual(public["status"], "failed")
        self.assertEqual(public["report_url"], "")
        self.assertEqual(public["creator_report_url"], "")

    def test_metadata_index_or_submission_failure_leaves_failed_records_not_queued(
        self,
    ):
        for phase in ("metadata", "index", "submit"):
            with self.subTest(phase=phase):
                error, counts = OSError(f"injected {phase} failure"), {
                    "metadata": 0,
                    "index": 0,
                }
                metadata, persist = (
                    self.store._write_job_metadata,
                    self.store._persist_locked,
                )

                def save_metadata(
                    root,
                    job,
                    metadata=metadata,
                    counts=counts,
                    phase=phase,
                    error=error,
                ):
                    metadata(root, job)
                    counts["metadata"] += 1
                    if phase == "metadata" and counts["metadata"] == 1:
                        raise error

                def save_index(
                    persist=persist, counts=counts, phase=phase, error=error
                ):
                    persist()
                    counts["index"] += 1
                    if phase == "index" and counts["index"] == 1:
                        raise error

                with (
                    mock.patch.object(
                        self.store, "_write_job_metadata", side_effect=save_metadata
                    ),
                    mock.patch.object(
                        self.store, "_persist_locked", side_effect=save_index
                    ),
                    mock.patch.object(
                        self.store._executor, "submit", side_effect=error
                    ) as submit,
                    self.assertRaises(OSError) as caught,
                ):
                    self.store.create(
                        {"product_name": phase},
                        self.uploads(),
                        owner_id="operator",
                        enforce_limits=True,
                    )
                self.assertIs(caught.exception, error)
                self.assertEqual(submit.call_count, int(phase == "submit"))
                job = next(
                    j for j in self.store.jobs.values() if j["product_name"] == phase
                )
                self.assert_failed_without_losing_inputs(job)
                job_root = Path(job["run_dir"]).parent
                self.assertEqual(
                    json.loads((job_root / "job.json").read_text())["status"], "failed"
                )
                self.assertEqual(
                    json.loads(self.store.state_path.read_text())[job["id"]]["status"],
                    "failed",
                )
                self.assertEqual(self.store._admission_reservations, {})

    def test_continuous_disk_errors_preserve_original_failure_and_uncertainty(self):
        error = OSError("original metadata failure")
        with (
            mock.patch.object(self.store, "_write_job_metadata", side_effect=error),
            mock.patch.object(
                self.store,
                "_persist_locked",
                side_effect=OSError("index still unavailable"),
            ),
            mock.patch.object(self.store._executor, "submit") as submit,
            self.assertRaises(OSError) as caught,
        ):
            self.store.create(
                {"product_name": "fixture"}, self.uploads(), enforce_limits=True
            )
        self.assertIs(caught.exception, error)
        self.assertEqual(submit.call_count, 0)
        self.assertEqual(self.store._admission_reservations, {})
        self.assertEqual(len(self.store.jobs), 1)
        job = next(iter(self.store.jobs.values()))
        self.assert_failed_without_losing_inputs(job)
        self.assertFalse(self.store.state_path.exists())
        self.assertEqual(len(error.__notes__), 2)

    def test_failed_metadata_recovers_when_index_could_not_be_saved(self):
        with (
            mock.patch.object(
                self.store, "_persist_locked", side_effect=OSError("index unavailable")
            ),
            mock.patch.object(self.store._executor, "submit") as submit,
            self.assertRaises(OSError),
        ):
            self.store.create(
                {"product_name": "fixture"}, self.uploads(), owner_id="operator"
            )
        self.assertEqual(submit.call_count, 0)
        restored = web.JobStore(self.store.root)
        self.addCleanup(restored._executor.shutdown, wait=True, cancel_futures=True)
        job = next(iter(restored.jobs.values()))
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["owner_id"], "operator")
        self.assertIn("未启动", job["failure_reason"])

    def test_enqueued_then_raised_submission_cannot_launch_rejected_job(self):
        real_submit = self.store._executor.submit
        release, futures = threading.Event(), []
        error = RuntimeError("executor failed after enqueue")
        process = mock.Mock()
        process.poll.return_value = 0

        def submit(task, job_id):
            def deferred():
                if not release.wait(timeout=3):
                    raise RuntimeError("test release timed out")
                return task(job_id)

            futures.append(real_submit(deferred))
            raise error

        with (
            mock.patch.object(self.store._executor, "submit", side_effect=submit),
            mock.patch.object(
                self.store, "_command", return_value=["synthetic-worker"]
            ) as command,
            mock.patch.object(self.store, "_finish"),
            mock.patch.object(web.subprocess, "Popen", return_value=process) as spawn,
        ):
            try:
                with self.assertRaises(RuntimeError) as caught:
                    self.store.create({"product_name": "fixture"}, self.uploads())
                self.assertIs(caught.exception, error)
            finally:
                release.set()
                for future in futures:
                    future.result(timeout=3)
            self.assertEqual(command.call_count, 0)
            self.assertEqual(spawn.call_count, 0)
        self.assert_failed_without_losing_inputs(next(iter(self.store.jobs.values())))

    def test_only_queued_state_can_start_a_worker(self):
        for state in ("failed", "completed", "degraded", "running", None):
            with self.subTest(state=state):
                self.store.jobs["existing"] = {
                    "id": "existing",
                    "status": state,
                    "run_dir": str(self.root / "run"),
                    "log_path": str(self.root / "worker.log"),
                }
                process = mock.Mock()
                process.poll.return_value = 0
                with (
                    mock.patch.object(
                        self.store, "_command", return_value=["synthetic-worker"]
                    ) as command,
                    mock.patch.object(self.store, "_finish"),
                    mock.patch.object(
                        web.subprocess, "Popen", return_value=process
                    ) as spawn,
                ):
                    self.store._run_job("existing")
                self.assertEqual(command.call_count, 0)
                self.assertEqual(spawn.call_count, 0)
                self.assertEqual(self.store.jobs["existing"]["status"], state)

    def test_successful_acceptance_claims_job_once_before_preparing_command(self):
        with mock.patch.object(self.store._executor, "submit") as submit:
            public = self.store.create({"product_name": "fixture"}, self.uploads())
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(public["status"], "queued")
        job_id, preparations = public["id"], []

        def command(_job):
            preparations.append(job_id)
            if len(preparations) == 1:
                self.store._run_job(job_id)
            return ["synthetic-worker"]

        process = mock.Mock()
        process.poll.return_value = 0
        with (
            mock.patch.object(self.store, "_command", side_effect=command),
            mock.patch.object(self.store, "_finish"),
            mock.patch.object(web.subprocess, "Popen", return_value=process) as spawn,
        ):
            self.store._run_job(job_id)
        self.assertEqual(preparations, [job_id])
        self.assertEqual(spawn.call_count, 1)


if __name__ == "__main__":
    unittest.main()
