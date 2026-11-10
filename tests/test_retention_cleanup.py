"""Retention must retain ownership when cleanup or its index commit fails."""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import web_app as web


class RetentionCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        environment = mock.patch.dict(web.os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = web.JobStore(self.root / "store")
        self.addCleanup(self.store._executor.shutdown, wait=True, cancel_futures=True)

    def job(self, name: str, status: str = "failed", *, expired: bool = True) -> Path:
        job_root = self.store.jobs_root / name
        job_root.mkdir()
        (job_root / "source.bin").write_bytes(b"synthetic owned source")
        created = dt.datetime.now(dt.timezone.utc)
        if expired:
            created -= dt.timedelta(seconds=web.JOB_RETENTION_SECONDS + 60)
        record = {
            "id": name,
            "owner_id": "owner-a",
            "workspace_id": "local",
            "status": status,
            "created_at": created.isoformat(),
            "run_dir": str(job_root / "run"),
        }
        self.store.jobs[name] = record
        self.store._write_job_metadata(job_root, record)
        self.store._persist_locked()
        return job_root

    def saved(self):
        return json.loads(self.store.state_path.read_text(encoding="utf-8"))

    def locked(self, target):
        remove = web.shutil.rmtree

        def cleanup(path, *args, **kwargs):
            if path == target:
                if kwargs.get("ignore_errors"):
                    return None
                raise PermissionError("synthetic retention lock")
            return remove(path, *args, **kwargs)

        return cleanup

    def test_failed_cleanup_is_retained_and_other_expired_jobs_continue(self):
        locked = self.job("job-locked")
        removable = self.job("job-removable", "completed")
        active = self.job("job-active", "running")
        recent = self.job("job-recent", "completed", expired=False)
        with mock.patch.object(web.shutil, "rmtree", side_effect=self.locked(locked)):
            self.store._garbage_collect()
        self.assertTrue(locked.is_dir())
        self.assertFalse(removable.exists())
        self.assertTrue(active.is_dir())
        self.assertTrue(recent.is_dir())
        self.assertTrue(self.saved()["job-locked"]["deletion_incomplete"])
        self.assertEqual(self.store.jobs["job-locked"]["status"], "failed")
        self.assertTrue(self.store.recovery_warning)
        self.assertNotIn("job-removable", self.saved())
        self.store._garbage_collect()
        self.assertNotIn("job-locked", self.saved())
        self.assertFalse(locked.exists())

    def test_index_failure_restores_rows_before_touching_any_files(self):
        path = self.job("job-old")
        error = OSError("synthetic index denial")
        with mock.patch.object(
            self.store, "_persist_locked", side_effect=error
        ), mock.patch.object(web.shutil, "rmtree") as remove, self.assertRaises(
            OSError
        ) as caught:
            self.store._garbage_collect()
        self.assertIs(caught.exception, error)
        self.assertIn("job-old", self.store.jobs)
        self.assertIn("job-old", self.saved())
        self.assertTrue(path.is_dir())
        remove.assert_not_called()

    def test_index_replace_then_error_is_compensated(self):
        path = self.job("job-old")
        persist = self.store._persist_locked
        calls = 0
        error = OSError("synthetic post-replace error")

        def save():
            nonlocal calls
            calls += 1
            persist()
            if calls == 1:
                raise error

        with mock.patch.object(
            self.store, "_persist_locked", side_effect=save
        ), mock.patch.object(web.shutil, "rmtree") as remove, self.assertRaises(
            OSError
        ) as caught:
            self.store._garbage_collect()
        self.assertIs(caught.exception, error)
        self.assertEqual(calls, 2)
        self.assertIn("job-old", self.saved())
        self.assertTrue(path.is_dir())
        remove.assert_not_called()

    def test_successful_batch_uses_two_batched_index_commits(self):
        paths = [self.job(f"job-old-{i}") for i in range(4)]
        with mock.patch.object(
            self.store, "_persist_locked", wraps=self.store._persist_locked
        ) as persist:
            self.store._garbage_collect()
        self.assertEqual(persist.call_count, 2)
        self.assertEqual(self.saved(), {})
        self.assertTrue(all(not path.exists() for path in paths))

    def test_interruption_restores_current_and_unattempted_records(self):
        first, current, remaining = [self.job(f"job-old-{i}") for i in range(3)]
        remove = web.shutil.rmtree
        error = KeyboardInterrupt("synthetic interruption")

        def interrupt(path, *args, **kwargs):
            if path == current:
                (path / "source.bin").unlink()
                raise error
            return remove(path, *args, **kwargs)

        with mock.patch.object(
            web.shutil, "rmtree", side_effect=interrupt
        ), self.assertRaises(KeyboardInterrupt) as caught:
            self.store._garbage_collect()
        self.assertIs(caught.exception, error)
        self.assertFalse(first.exists())
        self.assertTrue(current.is_dir())
        self.assertTrue(remaining.is_dir())
        self.assertTrue(self.saved()[current.name]["deletion_incomplete"])
        self.assertIn(remaining.name, self.saved())
        self.assertNotIn(first.name, self.saved())

    def test_metadata_failure_does_not_lose_retryable_index_record(self):
        path = self.job("job-old")
        with mock.patch.object(
            web.shutil, "rmtree", side_effect=self.locked(path)
        ), mock.patch.object(
            self.store,
            "_write_job_metadata",
            side_effect=OSError("synthetic metadata denial"),
        ):
            self.store._garbage_collect()
        self.assertTrue(self.saved()["job-old"]["deletion_incomplete"])
        self.assertTrue((path / "source.bin").is_file())

    def test_metadata_interruption_still_restores_all_unfinished_records(self):
        first = self.job("job-first")
        second = self.job("job-second")
        error = KeyboardInterrupt("synthetic metadata interruption")
        with mock.patch.object(
            web.shutil, "rmtree", side_effect=self.locked(first)
        ), mock.patch.object(
            self.store, "_write_job_metadata", side_effect=error
        ), self.assertRaises(
            KeyboardInterrupt
        ) as caught:
            self.store._garbage_collect()
        self.assertIs(caught.exception, error)
        self.assertTrue(first.is_dir())
        self.assertTrue(second.is_dir())
        self.assertTrue(self.saved()["job-first"]["deletion_incomplete"])
        self.assertIn("job-second", self.saved())
        self.assertIn("job-second", self.store.jobs)

    def test_cannot_persist_cleanup_failure_is_an_explicit_error(self):
        path = self.job("job-old")
        persist = self.store._persist_locked
        calls = 0

        def deny_restoration():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic final index denial")
            persist()

        with mock.patch.object(
            web.shutil, "rmtree", side_effect=self.locked(path)
        ), mock.patch.object(
            self.store, "_persist_locked", side_effect=deny_restoration
        ), self.assertRaises(
            OSError
        ):
            self.store._garbage_collect()
        self.assertTrue(path.is_dir())
        self.assertIn("job-old", self.store.jobs)
        self.assertTrue(self.saved()["job-old"]["deletion_incomplete"])
        with mock.patch.object(web.shutil, "rmtree", side_effect=self.locked(path)):
            restarted = web.JobStore(self.store.root)
        self.addCleanup(restarted._executor.shutdown, wait=True, cancel_futures=True)
        self.assertTrue(restarted.jobs["job-old"]["deletion_incomplete"])
        self.assertTrue(path.is_dir())

    def test_explicit_delete_metadata_interruption_still_persists_ownership(self):
        path = self.job("job-old")
        error = KeyboardInterrupt("synthetic metadata interruption")
        with mock.patch.object(
            web.shutil, "rmtree", side_effect=self.locked(path)
        ), mock.patch.object(
            self.store, "_write_job_metadata", side_effect=error
        ), self.assertRaises(
            KeyboardInterrupt
        ) as caught:
            self.store.delete("job-old", owner_id="owner-a", workspace_id="local")
        self.assertIs(caught.exception, error)
        self.assertTrue(self.saved()["job-old"]["deletion_incomplete"])
        self.assertTrue(path.is_dir())

    def test_registered_worker_keeps_ownership_despite_terminal_label(self):
        path = self.job("job-old")
        process = mock.Mock()
        process.poll.return_value = None
        self.store._running_processes["job-old"] = process
        self.store._garbage_collect()
        self.assertTrue(path.is_dir())
        self.assertIn("job-old", self.saved())
        self.assertIs(self.store._running_processes["job-old"], process)

    def test_symlink_escape_is_retained_without_deleting_external_directory(self):
        external = self.root / "external"
        external.mkdir()
        marker = external / "keep.txt"
        marker.write_text("outside managed jobs", encoding="utf-8")
        path = self.job("job-link")
        web.shutil.rmtree(path)
        try:
            path.symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest("Directory symlinks are unavailable on this platform")
        self.store._garbage_collect()
        self.assertEqual(marker.read_text(encoding="utf-8"), "outside managed jobs")
        self.assertTrue(self.saved()["job-link"]["deletion_incomplete"])

    def test_symlink_to_another_job_never_deletes_or_rewrites_its_metadata(self):
        alias = self.job("job-alias")
        other = self.job("job-other", "running", expired=False)
        metadata = (other / "job.json").read_bytes()
        web.shutil.rmtree(alias)
        try:
            alias.symlink_to(other, target_is_directory=True)
        except OSError:
            self.skipTest("Directory symlinks are unavailable on this platform")
        self.store._garbage_collect()
        self.assertTrue((other / "source.bin").is_file())
        self.assertEqual((other / "job.json").read_bytes(), metadata)
        self.assertTrue(self.saved()["job-alias"]["deletion_incomplete"])
        self.assertEqual(self.saved()["job-other"]["status"], "running")

    def test_explicit_delete_rejects_a_directory_alias_and_keeps_its_record(self):
        alias = self.job("job-alias")
        other = self.job("job-other", "running", expired=False)
        metadata = (other / "job.json").read_bytes()
        web.shutil.rmtree(alias)
        try:
            alias.symlink_to(other, target_is_directory=True)
        except OSError:
            self.skipTest("Directory symlinks are unavailable on this platform")
        with self.assertRaises(OSError):
            self.store.delete("job-alias", owner_id="owner-a", workspace_id="local")
        self.assertEqual((other / "job.json").read_bytes(), metadata)
        self.assertTrue((other / "source.bin").is_file())
        self.assertTrue(self.saved()["job-alias"]["deletion_incomplete"])


if __name__ == "__main__":
    unittest.main()
