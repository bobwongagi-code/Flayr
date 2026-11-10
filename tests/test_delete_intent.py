"""Manual deletion keeps durable ownership while releasing the admission lock."""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from scripts import web_app as web


class DeleteIntentTests(unittest.TestCase):
    def setUp(self):
        environment = mock.patch.dict(web.os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = web.JobStore(Path(temporary.name).resolve())
        self.addCleanup(self.store._executor.shutdown, wait=True, cancel_futures=True)
        self.path = self.job("job-first")

    def job(self, job_id):
        path = self.store.jobs_root / job_id
        path.mkdir()
        (path / "source.bin").write_bytes(b"synthetic owned source")
        record = {
            "id": job_id,
            "owner_id": "owner-a",
            "workspace_id": "local",
            "status": "queued",
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "run_dir": str(path / "run"),
            "log_path": str(path / "worker.log"),
            "benchmark_path": str(path / "source.bin"),
            "creator_path": str(path / "source.bin"),
        }
        self.store.jobs[job_id] = record
        self.store._write_job_metadata(path, record)
        self.store._persist_locked()
        return path

    def delete(self, job_id="job-first"):
        return self.store.delete(job_id, owner_id="owner-a", workspace_id="local")

    def saved(self):
        return json.loads(self.store.state_path.read_text(encoding="utf-8"))

    def test_cleanup_and_compensation_failure_keeps_ownership_after_restart(self):
        persist = self.store._persist_locked
        calls = 0

        def fail_compensation():
            nonlocal calls
            calls += 1
            if calls >= 2:
                raise OSError("synthetic index denial")
            persist()

        with mock.patch.object(
            self.store, "_persist_locked", side_effect=fail_compensation
        ), mock.patch.object(
            web.shutil, "rmtree", side_effect=PermissionError("synthetic locked file")
        ), self.assertRaises(
            PermissionError
        ):
            self.delete()
        self.assertTrue(self.saved()["job-first"]["deletion_incomplete"])
        self.assertTrue((self.path / "source.bin").is_file())
        restarted = web.JobStore(self.store.root)
        self.addCleanup(restarted._executor.shutdown, wait=True, cancel_futures=True)
        self.assertEqual(restarted.jobs["job-first"]["owner_id"], "owner-a")
        self.assertTrue(restarted.jobs["job-first"]["deletion_incomplete"])
        self.assertFalse(
            restarted.delete("job-first", owner_id="other", workspace_id="local")
        )
        self.assertTrue(
            restarted.delete("job-first", owner_id="owner-a", workspace_id="local")
        )
        self.assertFalse(self.path.exists())

    def test_durable_intent_precedes_stop_and_revokes_worker_writeback(self):
        process = mock.Mock()
        process.poll.return_value = 0
        self.store.jobs["job-first"]["status"] = "running"
        self.store._running_processes["job-first"] = process

        def stop(*args, **kwargs):
            self.assertTrue(self.saved()["job-first"]["deletion_incomplete"])
            self.assertEqual(self.store.jobs["job-first"]["status"], "failed")
            with mock.patch.object(web.subprocess, "Popen") as spawn:
                self.store._run_job("job-first")
            spawn.assert_not_called()
            self.store._update_running_job("job-first", progress=99)
            self.store._finish("job-first", 0)
            self.assertEqual(self.store.jobs["job-first"]["phase"], "删除未完成")

        with mock.patch.object(web, "stop_process_group", side_effect=stop):
            self.assertTrue(self.delete())
        self.assertNotIn("job-first", self.saved())
        self.assertNotIn("job-first", self.store._running_processes)

    def test_duplicate_delete_is_not_reentered_and_other_jobs_remain_available(self):
        other = self.job("job-other")
        entered, release = threading.Event(), threading.Event()
        remove = web.shutil.rmtree
        results, errors = [], []

        def cleanup(path, *args, **kwargs):
            if path == self.path:
                entered.set()
                if not release.wait(timeout=5):
                    raise RuntimeError("test cleanup release timed out")
            return remove(path, *args, **kwargs)

        def delete_first():
            try:
                results.append(self.delete())
            except (OSError, RuntimeError, AssertionError) as exc:
                errors.append(type(exc).__name__)

        worker = threading.Thread(target=delete_first)
        with mock.patch.object(web.shutil, "rmtree", side_effect=cleanup):
            try:
                worker.start()
                self.assertTrue(entered.wait(timeout=3))
                self.assertTrue(self.saved()["job-first"]["deletion_incomplete"])
                self.assertFalse(self.delete())
                self.assertFalse(
                    self.store.delete(
                        "job-first", owner_id="other", workspace_id="local"
                    )
                )
                self.assertTrue(self.delete("job-other"))
                self.assertFalse(other.exists())
            finally:
                release.set()
                worker.join(timeout=6)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [True])
        self.assertFalse(self.path.exists())

    def test_failed_initial_commit_restores_original_and_releases_claim(self):
        original = dict(self.store.jobs["job-first"])
        with mock.patch.object(
            self.store, "_persist_locked", side_effect=OSError("synthetic write denial")
        ), mock.patch.object(web.shutil, "rmtree") as remove, self.assertRaises(
            OSError
        ):
            self.delete()
        remove.assert_not_called()
        self.assertEqual(self.store.jobs["job-first"], original)
        self.assertTrue(self.delete())
        self.assertFalse(self.path.exists())

    def test_final_commit_failure_is_not_success_and_can_be_retried(self):
        persist = self.store._persist_locked
        calls = 0

        def fail_final():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic final write denial")
            persist()

        with mock.patch.object(
            self.store, "_persist_locked", side_effect=fail_final
        ), self.assertRaises(OSError):
            self.delete()
        self.assertFalse(self.path.exists())
        self.assertTrue(self.saved()["job-first"]["deletion_incomplete"])
        self.assertTrue(self.delete())
        self.assertEqual(self.saved(), {})
        self.assertFalse(self.path.exists())

    def test_stop_failure_keeps_handle_and_durable_intent_when_compensation_fails(self):
        process = mock.Mock()
        process.poll.return_value = None
        self.store._running_processes["job-first"] = process
        persist = self.store._persist_locked
        calls = 0

        def save():
            nonlocal calls
            calls += 1
            if calls > 1:
                raise OSError("synthetic compensation denial")
            persist()

        with mock.patch.object(
            self.store, "_persist_locked", side_effect=save
        ), mock.patch.object(
            web,
            "stop_process_group",
            side_effect=RuntimeError("synthetic stop failure"),
        ), self.assertRaises(
            RuntimeError
        ):
            self.delete()
        self.assertTrue(self.saved()["job-first"]["deletion_incomplete"])
        self.assertIs(self.store._running_processes["job-first"], process)
        self.assertTrue((self.path / "source.bin").is_file())
        process.poll.return_value = 0
        with mock.patch.object(web, "stop_process_group"):
            self.assertTrue(self.delete())

    def test_successful_delete_has_two_index_commits_no_metadata_recreation(self):
        with mock.patch.object(
            self.store, "_persist_locked", wraps=self.store._persist_locked
        ) as persist:
            self.assertTrue(self.delete())
        self.assertEqual(persist.call_count, 2)
        self.assertEqual(self.saved(), {})
        self.assertFalse(self.path.exists())

    def test_cleanup_interruption_releases_claim_and_keeps_retryable_intent(self):
        for error in (KeyboardInterrupt("synthetic interrupt"), SystemExit(23)):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(
                    web.shutil, "rmtree", side_effect=error
                ), self.assertRaises(type(error)) as caught:
                    self.delete()
                self.assertIs(caught.exception, error)
                self.assertTrue(self.saved()["job-first"]["deletion_incomplete"])
                self.assertEqual(self.store._deleting_jobs, set())
                self.assertTrue((self.path / "source.bin").is_file())
        self.assertTrue(self.delete())


if __name__ == "__main__":
    unittest.main()
