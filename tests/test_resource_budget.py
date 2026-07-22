import math
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core import resources
from flayr_core.resources import ResourceBudget, ResourceBudgetExceeded, ResourceLimits, encode_file_data_url
from flayr_core.utils import cleanup_stale_temp_entries, run_command


class ResourceBudgetTests(unittest.TestCase):
    def test_limits_reject_nonfinite_values(self) -> None:
        with self.assertRaises(ValueError):
            ResourceLimits(max_total_wall_time=math.nan)
        with self.assertRaises(ValueError):
            ResourceLimits(max_cost_estimate=math.inf)

    def test_source_and_api_limits_are_cumulative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.mp4"
            path.write_bytes(b"x" * 8)
            budget = ResourceBudget(
                ResourceLimits(
                    max_source_bytes=10,
                    max_single_request_bytes=10,
                    max_total_uploaded_bytes=12,
                    max_llm_calls=1,
                    max_cost_estimate=1.0,
                )
            )
            budget.register_source(path, 2.0)
            with self.assertRaises(ResourceBudgetExceeded):
                budget.register_source(path, 2.0)
            budget.reserve_api_call(8)
            with self.assertRaises(ResourceBudgetExceeded):
                budget.reserve_api_call(4)

    def test_api_event_ledger_records_retry_identity_and_cost(self) -> None:
        budget = ResourceBudget(ResourceLimits(max_llm_calls=2, max_cost_estimate=1.0))
        budget.reserve_api_call(
            8,
            request_id="request-1",
            attempt=2,
            retry_reason="incomplete stream",
            estimated_cost=0.25,
        )
        event = budget.snapshot()["used"]["api_events"][0]
        self.assertEqual(event["request_id"], "request-1")
        self.assertEqual(event["attempt"], 2)
        self.assertEqual(event["retry_reason"], "incomplete stream")
        self.assertEqual(event["request_bytes"], 8)
        self.assertEqual(event["estimated_cost"], 0.25)

    def test_stale_temp_cleanup_removes_only_known_old_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stale = root / ".response.flayr-tmp.old"
            fresh = root / ".response.flayr-tmp.fresh"
            unrelated = root / "keep-me"
            stale.mkdir()
            fresh.mkdir()
            unrelated.mkdir()
            old = time.time() - 3600
            os.utime(stale, (old, old))

            removed = cleanup_stale_temp_entries(root, (".response.flayr-tmpX.",), max_age_seconds=60)

            self.assertEqual(removed, 0)
            # A malformed/unrelated prefix must never broaden cleanup.
            self.assertTrue(stale.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(unrelated.exists())

            removed = cleanup_stale_temp_entries(root, (".response.flayr-tmp.",), max_age_seconds=60)
            self.assertEqual(removed, 1)
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(unrelated.exists())

    def test_data_url_is_bounded_and_signature_checked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "frame.jpg"
            image.write_bytes(b"\xff\xd8\xff" + b"x")
            encoded = encode_file_data_url(image, max_bytes=16, expected_kind="image")
            self.assertTrue(encoded.startswith("data:image/jpeg;base64,"))

            unknown = root / "frame.jpg"
            unknown.write_bytes(b"not-an-image")
            with self.assertRaises(ResourceBudgetExceeded):
                encode_file_data_url(unknown, max_bytes=16, expected_kind="image")

            with self.assertRaises(ResourceBudgetExceeded):
                encode_file_data_url(image, max_bytes=2, expected_kind="image")

    def test_command_output_is_streamed_and_capped(self) -> None:
        budget = ResourceBudget(ResourceLimits(max_total_wall_time=5.0))
        token = budget.activate()
        try:
            completed = run_command(
                [sys.executable, "-c", "print('x' * 1000000)"],
                timeout_seconds=5,
                max_output_bytes=128,
                budget=budget,
            )
        finally:
            resources._ACTIVE_BUDGET.reset(token)
        self.assertEqual(completed.returncode, 125)
        self.assertLessEqual(len(completed.stdout.encode("utf-8")), 128)

    def test_command_callback_receives_stream_without_stdout_buffer(self) -> None:
        chunks: list[bytes] = []
        completed = run_command(
            [sys.executable, "-c", "import sys; sys.stdout.write('a'); sys.stdout.flush(); sys.stdout.write('b')"],
            timeout_seconds=5,
            max_output_bytes=128,
            stdout_callback=chunks.append,
            capture_stdout=False,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(b"".join(chunks), b"ab")

    def test_report_and_download_budget_counters_are_hard_limits(self) -> None:
        budget = ResourceBudget(ResourceLimits(max_report_bytes=10, max_download_bytes=4))
        with self.assertRaises(ResourceBudgetExceeded):
            budget.reserve_report(11)
        budget.reserve_download(4)
        with self.assertRaises(ResourceBudgetExceeded):
            budget.reserve_download(1)

    def test_local_artifact_budget_is_hard_and_visible_in_snapshot(self) -> None:
        budget = ResourceBudget(ResourceLimits(max_local_artifact_bytes=4))
        budget.reserve_local_artifact(4)
        self.assertEqual(budget.snapshot()["used"]["local_artifact_bytes"], 4)
        with self.assertRaises(ResourceBudgetExceeded):
            budget.reserve_local_artifact(1)


if __name__ == "__main__":
    unittest.main()
