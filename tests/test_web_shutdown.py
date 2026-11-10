"""Offline service-stop regressions; no video or provider work is executed."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from scripts import web_app as web


class WebShutdownTests(unittest.TestCase):
    def setUp(self):
        environment = mock.patch.dict(web.os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = web.JobStore(self.root)
        self.addCleanup(self.store._executor.shutdown, wait=True, cancel_futures=True)

    def job(self, name="queued"):
        run = self.store.jobs_root / name / "run"
        run.mkdir(parents=True)
        self.store.jobs[name] = {
            "id": name,
            "status": "queued",
            "run_dir": str(run),
            "log_path": str(run.parent / "worker.log"),
        }
        self.store._persist_locked()
        return self.store.jobs[name]

    def uploads(self):
        result = {}
        for role in ("benchmark_video", "creator_video"):
            path = self.root / f"upload-{role}.mp4"
            path.write_bytes(b"synthetic input identity; not real video")
            result[role] = {"path": path, "filename": path.name}
        return result

    def test_stop_failure_still_attempts_other_children_and_allows_shutdown_retry(self):
        for failure in ("raises", "still_alive"):
            with self.subTest(failure=failure):
                first, second = mock.Mock(), mock.Mock()
                first.poll.return_value = None
                second.poll.return_value = 0
                self.store._running_processes = {"one": first, "two": second}
                with (
                    mock.patch.object(
                        web,
                        "stop_process_group",
                        side_effect=[
                            OSError("cannot signal") if failure == "raises" else None,
                            None,
                        ],
                    ) as stop,
                    mock.patch.object(
                        self.store._executor,
                        "shutdown",
                        wraps=self.store._executor.shutdown,
                    ) as pool,
                    self.assertRaisesRegex(RuntimeError, "one"),
                ):
                    self.store.shutdown()
                self.assertEqual(
                    stop.call_args_list,
                    [mock.call(p, grace_seconds=5.0) for p in (first, second)],
                )
                pool.assert_called_once_with(wait=False, cancel_futures=True)
                self.assertIs(self.store._running_processes["one"], first)
                first.poll.return_value = 0
                with (
                    mock.patch.object(web, "stop_process_group"),
                    mock.patch.object(
                        self.store._executor,
                        "shutdown",
                        wraps=self.store._executor.shutdown,
                    ) as pool,
                ):
                    self.store.shutdown()
                pool.assert_called_once_with(wait=True, cancel_futures=True)

    def test_successful_stop_keeps_wait_and_does_not_invalidate_completed_jobs(self):
        completed = {"id": "finished", "status": "completed", "failure_reason": ""}
        self.store.jobs["finished"] = dict(completed)
        process = mock.Mock()
        process.poll.return_value = 0
        self.store._running_processes["worker"] = process
        with (
            mock.patch.object(web, "stop_process_group") as stop,
            mock.patch.object(
                self.store._executor, "shutdown", wraps=self.store._executor.shutdown
            ) as pool,
        ):
            self.store.shutdown()
        stop.assert_called_once_with(process, grace_seconds=5.0)
        pool.assert_called_once_with(wait=True, cancel_futures=True)
        self.assertEqual(self.store.jobs["finished"], completed)
        self.assertTrue(self.store._shutdown_requested)

    def test_closed_store_rejects_uploads_without_creating_a_job(self):
        self.store.shutdown()
        files = self.uploads()
        with (
            mock.patch.object(self.store._executor, "submit") as submit,
            self.assertRaises(web.AdmissionError),
        ):
            self.store.create({"product_name": "fixture"}, files)
        submit.assert_not_called()
        self.assertEqual(self.store.jobs, {})
        self.assertEqual(list(self.store.jobs_root.iterdir()), [])
        self.assertTrue(all(not item["path"].exists() for item in files.values()))
        self.assertIsNotNone(
            self.store.admission_error(owner_id="local", workspace_id="local")
        )

    def test_stop_during_input_adoption_does_not_leave_an_unscheduled_job(self):
        files = self.uploads()
        real_move = web.shutil.move
        moves = []

        def move(source, destination):
            result = real_move(source, destination)
            moves.append(destination)
            if len(moves) == 1:
                self.store.shutdown()
            return result

        with (
            mock.patch.object(web.shutil, "move", side_effect=move),
            mock.patch.object(self.store._executor, "submit") as submit,
            self.assertRaises(web.AdmissionError),
        ):
            self.store.create({"product_name": "fixture"}, files)
        submit.assert_not_called()
        self.assertEqual(self.store.jobs, {})
        self.assertEqual(list(self.store.jobs_root.iterdir()), [])
        self.assertTrue(all(not item["path"].exists() for item in files.values()))

    def test_closed_store_does_not_launch_a_queued_worker(self):
        job = self.job()
        self.store.shutdown()
        process = mock.Mock()
        process.poll.return_value = 0
        with (
            mock.patch.object(
                self.store, "_command", return_value=["synthetic-worker"]
            ) as command,
            mock.patch.object(web.subprocess, "Popen", return_value=process) as spawn,
            mock.patch.object(self.store, "_finish"),
        ):
            self.store._run_job(job["id"])
        self.assertEqual(command.call_count, 0)
        self.assertEqual(spawn.call_count, 0)
        self.assertEqual(self.store.jobs[job["id"]]["status"], "failed")

    def test_shutdown_between_preparation_and_launch_prevents_popen(self):
        job = self.job()
        process = mock.Mock()
        process.poll.return_value = 0

        def prepare(_job):
            self.store.shutdown()
            return ["synthetic-worker"]

        with (
            mock.patch.object(self.store, "_command", side_effect=prepare),
            mock.patch.object(web.subprocess, "Popen", return_value=process) as spawn,
            mock.patch.object(self.store, "_finish"),
            mock.patch.object(web, "stop_process_group"),
        ):
            self.store._run_job(job["id"])
        self.assertEqual(spawn.call_count, 0)
        self.assertEqual(self.store.jobs[job["id"]]["status"], "failed")

    def test_command_preparation_error_is_recorded_without_launch(self):
        job = self.job()
        with (
            mock.patch.object(
                self.store,
                "_command",
                side_effect=RuntimeError("invalid worker configuration"),
            ),
            mock.patch.object(web.subprocess, "Popen") as spawn,
        ):
            self.store._run_job(job["id"])
        spawn.assert_not_called()
        saved = json.loads(self.store.state_path.read_text())[job["id"]]
        self.assertEqual(saved["status"], "failed")
        self.assertIn("invalid worker configuration", saved["failure_reason"])
        self.assertEqual(saved["estimated_remaining_seconds"], 0)

    def test_real_executor_cancellation_closes_queued_status_without_launch(self):
        job = self.job()
        ready = threading.Barrier(3)
        release, cancelled = threading.Event(), threading.Event()
        errors = []

        def blocker():
            ready.wait(timeout=3)
            release.wait(timeout=5)

        def shutdown():
            try:
                self.store.shutdown()
            except (RuntimeError, OSError) as exc:
                errors.append(exc)

        thread = threading.Thread(target=shutdown)
        with mock.patch.object(web.subprocess, "Popen") as spawn:
            try:
                for _ in range(2):
                    self.store._executor.submit(blocker)
                ready.wait(timeout=3)
                future = self.store._executor.submit(self.store._run_job, job["id"])
                future.add_done_callback(lambda _future: cancelled.set())
                thread.start()
                self.assertTrue(
                    cancelled.wait(timeout=3), "queued future not cancelled"
                )
                self.assertTrue(future.cancelled())
                release.set()
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive(), "shutdown did not finish")
                self.assertEqual(errors, [])
                spawn.assert_not_called()
                saved = json.loads(self.store.state_path.read_text())[job["id"]]
                self.assertEqual(saved["status"], "failed")
                self.assertIn("停止", saved["failure_reason"])
            finally:
                release.set()
                if thread.ident is not None:
                    thread.join(timeout=5)

    def test_pending_state_persistence_error_does_not_skip_pool_shutdown(self):
        job = self.job()
        with (
            mock.patch.object(
                self.store, "_persist_locked", side_effect=OSError("disk full")
            ),
            mock.patch.object(
                self.store._executor, "shutdown", wraps=self.store._executor.shutdown
            ) as pool,
            self.assertRaisesRegex(RuntimeError, "disk full"),
        ):
            self.store.shutdown()
        pool.assert_called_once_with(wait=True, cancel_futures=True)
        self.assertTrue(self.store._shutdown_requested)
        self.assertTrue(self.store._shutdown_state_dirty)
        self.assertEqual(
            json.loads(self.store.state_path.read_text())[job["id"]]["status"], "queued"
        )
        self.store.shutdown()
        self.assertFalse(self.store._shutdown_state_dirty)
        self.assertEqual(
            json.loads(self.store.state_path.read_text())[job["id"]]["status"], "failed"
        )

    def test_pool_error_does_not_skip_pending_status_finalization(self):
        job = self.job()
        with (
            mock.patch.object(
                self.store._executor,
                "shutdown",
                side_effect=RuntimeError("pool failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "pool failure"),
        ):
            self.store.shutdown()
        self.assertEqual(
            json.loads(self.store.state_path.read_text())[job["id"]]["status"], "failed"
        )


if __name__ == "__main__":
    unittest.main()
