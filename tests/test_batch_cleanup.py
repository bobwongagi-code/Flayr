"""Batch failure cleanup using synthetic workers, never model/video jobs."""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from scripts import batch_analyze as batch


class BatchCleanupTests(unittest.TestCase):
    @staticmethod
    def job(name="sample"):
        return {"name": name, "creator": "fixture", "benchmark": "fixture"}

    def test_launch_status_write_failure_still_owns_and_stops_child(self):
        for persistent, broken_stderr in ((False, False), (True, False), (True, True)):
            with (
                self.subTest(persistent=persistent, broken_stderr=broken_stderr),
                tempfile.TemporaryDirectory() as temp,
            ):
                root, states = Path(temp), []
                initial_error = OSError("running record failed")
                process = mock.Mock()
                process.poll.return_value = None
                log = mock.mock_open()
                errors = mock.Mock() if broken_stderr else io.StringIO()
                if broken_stderr:
                    errors.write.side_effect = OSError("diagnostic log failed")

                def persist(
                    _path,
                    status,
                    states=states,
                    initial_error=initial_error,
                    persistent=persistent,
                ):
                    states.append(status["jobs"]["sample"]["state"])
                    if len(states) == 2:
                        raise initial_error
                    if persistent and len(states) > 2:
                        raise OSError("interrupt record failed")

                with (
                    mock.patch.object(
                        batch, "_completion_gate", return_value=(False, "new")
                    ),
                    mock.patch.object(batch, "open", log, create=True),
                    mock.patch.object(batch, "write_status", side_effect=persist),
                    mock.patch.object(
                        batch.subprocess, "Popen", return_value=process
                    ) as spawn,
                    mock.patch.object(batch, "_stop_process") as stop,
                    redirect_stderr(errors),
                    self.assertRaises(OSError) as caught,
                ):
                    batch._run_jobs([self.job()], [], root, root / "status.json", 1)
                self.assertIs(caught.exception, initial_error)
                self.assertEqual(states, ["pending", "running", "interrupted"])
                spawn.assert_called_once()
                stop.assert_called_once_with(process)
                log().close.assert_called_once()

    def test_interrupt_attempts_every_cleanup_even_if_persist_stop_or_close_fails(self):
        for cleanup_failure in (None, "stop", "close"):
            with (
                self.subTest(cleanup_failure=cleanup_failure),
                tempfile.TemporaryDirectory() as temp,
            ):
                root, states = Path(temp), []
                interruption = KeyboardInterrupt("operator stopped")
                processes = [mock.Mock(), mock.Mock()]
                logs = [mock.Mock(), mock.Mock()]
                for process in processes:
                    process.poll.return_value = None
                if cleanup_failure == "close":
                    logs[0].close.side_effect = OSError("log close failed")
                errors = io.StringIO()

                def persist(_path, status, states=states):
                    states.append(
                        tuple(job["state"] for job in status["jobs"].values())
                    )
                    if len(states) == 4:
                        raise OSError("interrupt record failed")

                with (
                    mock.patch.object(
                        batch, "_completion_gate", return_value=(False, "new")
                    ),
                    mock.patch.object(batch, "open", side_effect=logs, create=True),
                    mock.patch.object(batch, "write_status", side_effect=persist),
                    mock.patch.object(batch.subprocess, "Popen", side_effect=processes),
                    mock.patch.object(batch.time, "sleep", side_effect=interruption),
                    mock.patch.object(
                        batch,
                        "_stop_process",
                        side_effect=[
                            (
                                RuntimeError("stop failed")
                                if cleanup_failure == "stop"
                                else None
                            ),
                            None,
                        ],
                    ) as stop,
                    redirect_stderr(errors),
                    self.assertRaises(KeyboardInterrupt) as caught,
                ):
                    batch._run_jobs(
                        [self.job("one"), self.job("two")],
                        [],
                        root,
                        root / "status.json",
                        2,
                    )
                self.assertIs(caught.exception, interruption)
                self.assertEqual(states[-1], ("interrupted", "interrupted"))
                self.assertEqual(stop.call_args_list, [mock.call(p) for p in processes])
                for log in logs:
                    log.close.assert_called_once()
                self.assertIn("interrupt record failed", errors.getvalue())
                if cleanup_failure:
                    self.assertIn(f"{cleanup_failure} failed", errors.getvalue())

    def test_initial_status_failure_does_not_start_a_worker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with (
                mock.patch.object(
                    batch, "_completion_gate", return_value=(False, "new")
                ),
                mock.patch.object(
                    batch, "write_status", side_effect=OSError("initial record failed")
                ),
                mock.patch.object(batch.subprocess, "Popen") as spawn,
                mock.patch.object(batch, "_stop_process") as stop,
                self.assertRaisesRegex(OSError, "initial record failed"),
            ):
                batch._run_jobs([self.job()], [], root, root / "status.json", 1)
            spawn.assert_not_called()
            stop.assert_not_called()

    def test_later_launch_or_completion_failure_cleans_remaining_workers(self):
        for failure_at in ("second_launch", "completion"):
            with (
                self.subTest(failure_at=failure_at),
                tempfile.TemporaryDirectory() as temp,
            ):
                root = Path(temp)
                error = OSError(failure_at)
                processes, logs = [mock.Mock(), mock.Mock()], [mock.Mock(), mock.Mock()]
                processes[0].poll.return_value = (
                    0 if failure_at == "completion" else None
                )
                processes[0].returncode = 0
                processes[1].poll.return_value = None
                launches = [
                    processes[0],
                    error if failure_at == "second_launch" else processes[1],
                ]
                writes = [None, None, None, error, OSError("interrupt record failed")]
                errors = io.StringIO()
                with (
                    mock.patch.object(
                        batch,
                        "_completion_gate",
                        side_effect=[(False, "new"), (False, "new"), (True, "")],
                    ),
                    mock.patch.object(batch, "open", side_effect=logs, create=True),
                    mock.patch.object(batch, "write_status", side_effect=writes),
                    mock.patch.object(batch.subprocess, "Popen", side_effect=launches),
                    mock.patch.object(batch.time, "sleep"),
                    mock.patch.object(batch, "_stop_process") as stop,
                    redirect_stderr(errors),
                    self.assertRaises(OSError) as caught,
                ):
                    batch._run_jobs(
                        [self.job("one"), self.job("two")],
                        [],
                        root,
                        root / "status.json",
                        2,
                    )
                self.assertIs(caught.exception, error)
                stop.assert_called_once_with(
                    processes[0 if failure_at == "second_launch" else 1]
                )
                for log in logs:
                    self.assertTrue(log.close.called)
                if failure_at == "completion":
                    self.assertIn("interrupt record failed", errors.getvalue())

    def test_completed_children_are_not_stopped_on_normal_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            root, states = Path(temp), []
            process = mock.Mock(returncode=0)
            process.poll.return_value = 0
            log = mock.mock_open()
            with (
                mock.patch.object(
                    batch, "_completion_gate", side_effect=[(False, "new"), (True, "")]
                ),
                mock.patch.object(batch, "open", log, create=True),
                mock.patch.object(
                    batch,
                    "write_status",
                    side_effect=lambda _p, s: states.append(
                        s["jobs"]["sample"]["state"]
                    ),
                ),
                mock.patch.object(batch.subprocess, "Popen", return_value=process),
                mock.patch.object(batch.time, "sleep"),
                mock.patch.object(batch, "_stop_process") as stop,
            ):
                self.assertEqual(
                    batch._run_jobs([self.job()], [], root, root / "status.json", 1), 0
                )
            self.assertEqual(states, ["pending", "running", "done", "done"])
            stop.assert_not_called()
            log().close.assert_called_once()

    def test_real_local_child_is_reaped_after_status_write_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root, children = Path(temp), []
            real_popen, real_stop = subprocess.Popen, batch._stop_process

            def spawn(*args, **options):
                process = real_popen(*args, **options)
                children.append(process)
                return process

            try:
                with (
                    mock.patch.object(
                        batch, "_completion_gate", return_value=(False, "new")
                    ),
                    mock.patch.object(
                        batch,
                        "build_command",
                        return_value=[
                            sys.executable,
                            "-c",
                            "import time; time.sleep(60)",
                        ],
                    ),
                    mock.patch.object(
                        batch,
                        "write_status",
                        side_effect=[None, OSError("running record failed"), None],
                    ),
                    mock.patch.object(batch.subprocess, "Popen", side_effect=spawn),
                    self.assertRaisesRegex(OSError, "running record failed"),
                ):
                    batch._run_jobs([self.job()], [], root, root / "status.json", 1)
                self.assertEqual(len(children), 1)
                self.assertIsNotNone(
                    children[0].poll(), "runner left a live child behind"
                )
                self.assertIsNotNone(children[0].wait(timeout=1))
            finally:
                for process in children:
                    if process.poll() is None:
                        real_stop(process, grace_seconds=1)

    def test_restart_refuses_live_or_unidentified_previous_workers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            status = root / "status.json"
            for state, worker in (
                ("starting", None), ("running", None),
                ("running", batch._lock_identity()),
                ("interrupted", batch._lock_identity()),
            ):
                batch.write_status(status, {"jobs": {"sample": {"state": state, "worker": worker}}})
                before = status.read_bytes()
                work = mock.Mock()
                with self.assertRaisesRegex(RuntimeError, "拒绝重新派发"):
                    batch._run_owned_batch(root / "runner.lock", status, work)
                work.assert_not_called()
                self.assertEqual(status.read_bytes(), before)

    @unittest.skipUnless(os.name == "posix", "POSIX descriptor inheritance")
    def test_worker_retains_runner_guard_after_parent_closes_descriptor(self):
        with tempfile.TemporaryDirectory() as temp:
            lock = Path(temp) / "runner.lock"
            status = lock.with_name("status.json")
            with batch.exclusive_file_lock(lock.with_suffix(".guard")) as fd:
                worker = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(60)"], pass_fds=(fd,),
                )
            try:
                with self.assertRaisesRegex(OSError, "Another process holds the lock"):
                    batch._run_owned_batch(lock, status, lambda *_: self.fail("dispatched a second runner"))
                self.assertFalse(status.exists())
            finally:
                worker.terminate()
                worker.wait(timeout=5)
            self.assertEqual(batch._run_owned_batch(lock, status, lambda *_: 0), 0)

    @unittest.skipUnless(os.name == "posix", "real POSIX SIGTERM and process group")
    def test_sigterm_reaps_worker_before_releasing_runner(self):
        script = """
import sys
from pathlib import Path
from unittest.mock import patch
from scripts import batch_analyze as batch
root = Path(sys.argv[1])
status = root / "status.json"
job = {"name": "sample", "creator": "fixture", "benchmark": "fixture"}
with patch.object(batch, "_completion_gate", return_value=(False, "new")), \
     patch.object(batch, "build_command", return_value=[sys.executable, "-c",
         "import time; time.sleep(120)"]):
    raise SystemExit(batch._run_owned_batch(root / "runner.lock", status,
        lambda fd, stopping: batch._run_jobs([job], [], root, status, 1,
            inherited_lock_fd=fd, stop_requested=stopping)))
"""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            status = root / "status.json"
            worker_pid = None
            with subprocess.Popen(
                [sys.executable, "-B", "-c", script, str(root)],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            ) as runner:
                try:
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline and runner.poll() is None:
                        if status.exists():
                            job = json.loads(status.read_text())["jobs"]["sample"]
                            if job["state"] == "running":
                                worker_pid = job["worker"]["pid"]
                                break
                        time.sleep(0.02)
                    self.assertIsNotNone(worker_pid)
                    runner.send_signal(signal.SIGTERM)
                    _, error = runner.communicate(timeout=14)
                    self.assertEqual(runner.returncode, 128 + signal.SIGTERM, error)
                    self.assertFalse(batch._pid_alive(worker_pid))
                    self.assertEqual(json.loads(status.read_text())["jobs"]["sample"]["state"], "interrupted")
                    self.assertEqual(batch._run_owned_batch(root / "runner.lock", status, lambda *_: 0), 0)
                finally:
                    if runner.poll() is None:
                        runner.kill()
                    if worker_pid is not None and batch._pid_alive(worker_pid):
                        os.killpg(worker_pid, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
