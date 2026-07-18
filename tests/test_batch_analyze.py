from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.batch_analyze import _run_jobs, acquire_lock, validate_spec


class BatchAnalyzeValidationTests(unittest.TestCase):
    def _job(self, name: str = "sample") -> dict[str, object]:
        return {"name": name, "creator": "/tmp/creator.mp4", "benchmark": "/tmp/benchmark.mp4"}

    def test_rejects_duplicate_names_and_output_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            with self.assertRaisesRegex(ValueError, "重复 job name"):
                validate_spec({"jobs": [self._job(), self._job()]}, runs_dir, 1)

            first = self._job("first")
            second = self._job("second")
            first["output_dir"] = str(runs_dir / "same")
            second["output_dir"] = str(runs_dir / "same")
            with self.assertRaisesRegex(ValueError, "同一 output_dir"):
                validate_spec({"jobs": [first, second]}, runs_dir, 1)

    def test_rejects_unsafe_names_and_runner_owned_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            with self.assertRaisesRegex(ValueError, "name 非法"):
                validate_spec({"jobs": [self._job("../escape")]}, runs_dir, 1)
            with self.assertRaisesRegex(ValueError, "不得覆盖 runner 参数"):
                validate_spec(
                    {"common_args": ["--output-dir=/tmp/override"], "jobs": [self._job()]},
                    runs_dir,
                    1,
                )

    def test_rejects_non_positive_concurrency(self) -> None:
        with self.assertRaisesRegex(ValueError, "concurrency"):
            validate_spec({"jobs": [self._job()]}, Path("/tmp/runs"), 0)

    def test_lock_creation_is_atomic_and_rejects_live_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "runner.lock"
            acquire_lock(lock_path)
            self.assertEqual(lock_path.read_text(encoding="utf-8"), str(os.getpid()))
            with self.assertRaisesRegex(RuntimeError, "已有 runner"):
                acquire_lock(lock_path)

    def test_launch_failure_closes_log_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = root / "_batch" / "status.json"
            status_path.parent.mkdir()
            opened = mock.mock_open()
            with mock.patch("builtins.open", opened), mock.patch(
                "scripts.batch_analyze.subprocess.Popen", side_effect=OSError("launch failed")
            ):
                with self.assertRaisesRegex(OSError, "launch failed"):
                    _run_jobs([self._job()], [], root, status_path, 1)
            opened().close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
