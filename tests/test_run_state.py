from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.flayr_core.run_state import (
    ANALYSIS_COMPLETED,
    COMPLETED,
    CREATED,
    FAILED,
    PROCESSING,
    REPORT_GENERATING,
    RunStateError,
    begin_report_generation,
    initialize_run_state,
    read_run_state,
    recover_run_state,
    transition_run_state,
)


class RunStateTests(unittest.TestCase):
    def test_normal_lifecycle_is_explicit_and_history_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            initialize_run_state(run_dir, job_id="job-1")
            transition_run_state(run_dir, PROCESSING)
            transition_run_state(run_dir, ANALYSIS_COMPLETED, artifacts=("analysis.json",))
            transition_run_state(run_dir, REPORT_GENERATING, artifacts=("bd_report.html",))
            final = transition_run_state(run_dir, COMPLETED, artifacts=("_SUCCESS.json",))

            self.assertEqual(final["state"], COMPLETED)
            self.assertEqual(final["job_id"], "job-1")
            self.assertEqual([entry["state"] for entry in final["history"]], [
                CREATED,
                PROCESSING,
                ANALYSIS_COMPLETED,
                REPORT_GENERATING,
                COMPLETED,
            ])
            self.assertIsNotNone(final["completed_at"])

    def test_invalid_transition_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            initialize_run_state(run_dir)
            with self.assertRaises(RunStateError):
                transition_run_state(run_dir, COMPLETED)

    def test_report_generation_state_is_written_before_report_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            initialize_run_state(run_dir, job_id="job-3")
            transition_run_state(run_dir, PROCESSING)
            transition_run_state(run_dir, ANALYSIS_COMPLETED, artifacts=("analysis.json",))

            state = begin_report_generation(run_dir, job_id="job-3", artifacts=("analysis.json",))

            self.assertEqual(state["state"], REPORT_GENERATING)
            self.assertFalse((run_dir / "bd_report.html").exists())

    def test_restart_recovery_records_terminal_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            initialize_run_state(run_dir, job_id="job-2")
            transition_run_state(run_dir, PROCESSING)
            recovered = recover_run_state(
                run_dir,
                FAILED,
                job_id="job-2",
                reason="服务重启时发现任务未完成。",
            )

            self.assertEqual(recovered["state"], FAILED)
            self.assertTrue(recovered["history"][-1]["recovered"])
            self.assertEqual(recovered["history"][-1]["from_state"], PROCESSING)
            self.assertEqual(read_run_state(run_dir)["state"], FAILED)


if __name__ == "__main__":
    unittest.main()
