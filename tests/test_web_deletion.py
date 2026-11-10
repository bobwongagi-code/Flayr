"""Deleting a job must not lose ownership of its worker or report false success."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import web_app as web


class WebDeletionTests(unittest.TestCase):
    def setUp(self):
        environment = mock.patch.dict(web.os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.store = web.JobStore(self.root / "store")
        self.addCleanup(self.store._executor.shutdown, wait=True, cancel_futures=True)
        files = {}
        for role in ("benchmark_video", "creator_video"):
            path = self.root / f"{role}.mp4"
            path.write_bytes(b"synthetic input")
            files[role] = {"path": path, "filename": path.name}
        with mock.patch.object(self.store._executor, "submit"):
            public = self.store.create({}, files, owner_id="owner-a")
        self.job_id = public["id"]
        self.job = dict(self.store.jobs[self.job_id])
        self.job_root = Path(self.job["run_dir"]).parent

    def delete(self):
        return self.store.delete(self.job_id, owner_id="owner-a", workspace_id="local")

    def test_failed_index_removal_keeps_job_and_inputs(self):
        with mock.patch.object(
            self.store, "_persist_locked", side_effect=OSError("index failed")
        ):
            with self.assertRaises(OSError):
                self.delete()
        self.assertIn(self.job_id, self.store.jobs)
        self.assertTrue(Path(self.job["creator_path"]).is_file())

    def test_stop_failure_or_live_worker_is_retryable_without_removing_inputs(self):
        for failure in ("raises", "alive"):
            with self.subTest(failure=failure):
                process = mock.Mock()
                process.poll.return_value = None
                self.store._running_processes[self.job_id] = process
                with mock.patch.object(
                    web,
                    "stop_process_group",
                    side_effect=OSError("cannot stop") if failure == "raises" else None,
                ):
                    with self.assertRaises((OSError, RuntimeError)):
                        self.delete()
                self.assertIn(self.job_id, self.store.jobs)
                self.assertTrue(Path(self.job["creator_path"]).is_file())
                self.assertIs(self.store._running_processes[self.job_id], process)
                process.poll.return_value = 0
        with mock.patch.object(web, "stop_process_group"):
            self.assertTrue(self.delete())
        self.assertNotIn(self.job_id, self.store.jobs)
        self.assertFalse(self.job_root.exists())

    def test_filesystem_cleanup_failure_is_not_success_and_can_be_retried(self):
        with mock.patch.object(
            web.shutil, "rmtree", side_effect=PermissionError("locked file")
        ):
            with self.assertRaises(PermissionError):
                self.delete()
        saved = json.loads(self.store.state_path.read_text(encoding="utf-8"))
        self.assertIn(self.job_id, saved)
        self.assertTrue(self.job_root.exists())
        self.assertTrue(self.delete())
        self.assertFalse(self.job_root.exists())

    def test_delete_during_preparation_does_not_recreate_or_launch_job(self):
        def command(_job):
            self.assertTrue(self.delete())
            return ["synthetic-worker"]

        with mock.patch.object(
            self.store, "_command", side_effect=command
        ), mock.patch.object(web.subprocess, "Popen") as spawn:
            self.store._run_job(self.job_id)
        self.assertEqual(spawn.call_count, 0)
        self.assertNotIn(self.job_id, self.store.jobs)
        self.assertFalse(self.job_root.exists())

    def test_delete_after_log_open_cannot_launch_a_late_worker(self):
        process = mock.Mock()
        process.poll.return_value = 0
        real_open = Path.open
        log = mock.MagicMock()

        def enter():
            self.assertTrue(self.delete())
            return mock.Mock()

        log.__enter__.side_effect = enter

        def open_path(path, *args, **kwargs):
            if path == Path(self.job["log_path"]) and args == ("ab",):
                return log
            return real_open(path, *args, **kwargs)

        with mock.patch.object(
            self.store, "_command", return_value=["synthetic-worker"]
        ), mock.patch.object(Path, "open", open_path), mock.patch.object(
            web.subprocess, "Popen", return_value=process
        ) as spawn:
            self.store._run_job(self.job_id)
        self.assertEqual(spawn.call_count, 0)
        self.assertFalse(self.job_root.exists())

    def test_deleted_queued_callback_and_other_owner_cannot_start_or_delete(self):
        self.assertFalse(
            self.store.delete(self.job_id, owner_id="other", workspace_id="local")
        )
        self.assertTrue(self.delete())
        with mock.patch.object(web.subprocess, "Popen") as spawn:
            self.store._run_job(self.job_id)
        self.assertEqual(spawn.call_count, 0)
        self.assertFalse(self.delete())

    def test_index_write_then_error_restores_disk_record(self):
        persist = self.store._persist_locked
        calls = []

        def save():
            persist()
            calls.append(True)
            if len(calls) == 1:
                raise OSError("error after atomic index replacement")

        with mock.patch.object(self.store, "_persist_locked", side_effect=save):
            with self.assertRaises(OSError):
                self.delete()
        self.assertIn(self.job_id, self.store.jobs)
        self.assertIn(
            self.job_id, json.loads(self.store.state_path.read_text(encoding="utf-8"))
        )
        self.assertTrue(Path(self.job["creator_path"]).is_file())

    def test_http_failure_is_explicit_without_exposing_internal_error(self):
        handler = SimpleNamespace(
            path=f"/api/jobs/{self.job_id}",
            server=SimpleNamespace(store=self.store),
            _authorize_request=mock.Mock(return_value=True),
            _workspace_id=mock.Mock(return_value="local"),
            _client_id=mock.Mock(return_value="owner-a"),
            _json=mock.Mock(),
            send_response=mock.Mock(),
            _send_identity_cookie=mock.Mock(),
            end_headers=mock.Mock(),
        )
        with mock.patch.object(
            self.store, "delete", side_effect=OSError("private diagnostic")
        ):
            web.FlayrHandler.do_DELETE(handler)
        handler._json.assert_called_once_with(
            500, {"error": "任务删除未完成，请稍后重试。"}
        )
        handler.send_response.assert_not_called()

    def test_late_finalizer_cannot_overwrite_incomplete_deletion(self):
        for publication in ("completed", "degraded", "failed"):
            with self.subTest(publication=publication):
                self.store._update(self.job_id, status="running")
                ready, release = threading.Event(), threading.Event()
                errors = []

                def validate(*args, **kwargs):
                    ready.set()
                    if not release.wait(timeout=3):
                        raise RuntimeError("test release timed out")
                    return publication

                def finish():
                    try:
                        self.store._finish(self.job_id, 0)
                    except Exception as exc:
                        errors.append(type(exc).__name__)

                worker = threading.Thread(target=finish)
                with mock.patch.object(
                    web, "_publication_status", side_effect=validate
                ):
                    try:
                        worker.start()
                        self.assertTrue(ready.wait(timeout=3))
                        with mock.patch.object(
                            web.shutil, "rmtree", side_effect=OSError("cleanup failed")
                        ):
                            with self.assertRaises(OSError):
                                self.delete()
                    finally:
                        release.set()
                        worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(self.store.jobs[self.job_id]["status"], "failed")
                self.assertEqual(self.store.jobs[self.job_id]["phase"], "删除未完成")

    def test_incomplete_deletion_survives_directory_based_index_rebuild(self):
        with mock.patch.object(
            web.shutil, "rmtree", side_effect=OSError("cleanup failed")
        ):
            with self.assertRaises(OSError):
                self.delete()
        self.store.state_path.unlink(missing_ok=True)
        self.store.state_backup_path.unlink(missing_ok=True)
        with mock.patch.object(web, "_publication_status", return_value="completed"):
            restored = web.JobStore(self.store.root)
        self.addCleanup(restored._executor.shutdown, wait=True, cancel_futures=True)
        recovered = restored.jobs[self.job_id]
        self.assertEqual(recovered["status"], "failed")
        self.assertEqual(recovered["phase"], "删除未完成")
        self.assertEqual(recovered["owner_id"], "owner-a")
        self.assertTrue(Path(self.job["creator_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
