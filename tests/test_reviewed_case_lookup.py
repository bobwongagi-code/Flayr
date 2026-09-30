from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import test_human_review as human_review_fixtures

from scripts import lookup_reviewed_cases
from scripts.flayr_core import human_review as hr


class ReviewedCaseLookupTests(unittest.TestCase):
    def _fresh_run(self) -> tuple[object, object, Path]:
        fixture = human_review_fixtures.HumanReviewTests()
        temp, run_dir = fixture.fresh_run()
        self.addCleanup(temp.cleanup)
        return fixture, temp, run_dir

    def test_final_cases_separate_model_and_human_outcomes(self) -> None:
        _, _, run_dir = self._fresh_run()
        hr.init_review(run_dir)
        hr.update_stage(run_dir, "S1", "confirmed")
        hr.update_stage(
            run_dir,
            "S2",
            "corrected",
            relation="benchmark_better",
            gap="medium",
            note="人工复核说明",
        )
        hr.update_stage(run_dir, "S3", "not_applicable", note="双方没有信任证据")
        hr.update_stage(run_dir, "S4", "insufficient_evidence", note="效果证据不足")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = lookup_reviewed_cases.main(["--run-dir", str(run_dir)])
        output = stdout.getvalue()

        self.assertEqual(status, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertIn(f"Source run: {run_dir.resolve()}", output)
        self.assertIn("Review mode: draft_visible", output)
        self.assertIn(
            "Interpretation: nonblind; does not establish model accuracy", output
        )
        self.assertIn("Model judgment: creator_better / small", output)
        self.assertIn("Model reason: 达人开场更直接。", output)
        self.assertIn("Human decision: confirmed", output)
        self.assertIn(
            "Human relation / gap: creator_better / small (confirmed original)", output
        )
        self.assertIn("Human note: none", output)
        self.assertIn("Human relation / gap: not_applicable", output)
        self.assertIn("Human relation / gap: unknown", output)
        self.assertIn("Human note: 双方没有信任证据", output)
        self.assertIn("Human note: 效果证据不足", output)
        self.assertIn("Evidence IDs: C1, B1", output)
        self.assertNotIn("Stage: S5", output)
        self.assertNotIn("Stage: S6", output)

    def test_repeat_paths_stage_and_query_are_explicit_literal_filters(self) -> None:
        _, _, first = self._fresh_run()
        _, _, second = self._fresh_run()
        for run_dir in (first, second):
            hr.init_review(run_dir)
            hr.update_stage(
                run_dir,
                "S2",
                "corrected",
                relation="benchmark_better",
                gap="medium",
                note="Literal query marker",
            )
        arguments = [
            "--run-dir",
            str(first),
            "--run-dir",
            str(second),
            "--stage",
            "S2",
            "--query",
            "Literal query marker",
        ]
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = lookup_reviewed_cases.main(arguments)
        output = stdout.getvalue()

        self.assertEqual(status, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertIn(str(first.resolve()), output)
        self.assertIn(str(second.resolve()), output)
        self.assertEqual(output.count("Stage: S2"), 2)
        self.assertNotIn("Stage: S1", output)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = lookup_reviewed_cases.main(
                [
                    "--run-dir",
                    str(first),
                    "--stage",
                    "S2",
                    "--query",
                    "literal query marker",
                ]
            )
        self.assertEqual(status, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(stdout.getvalue(), "No final human-reviewed cases matched.\n")

    def test_invalid_runs_fail_with_source_path_and_no_partial_stdout(self) -> None:
        fixture, temp, stale_run = self._fresh_run()
        hr.init_review(stale_run)
        analysis = hr.load_analysis(stale_run)
        analysis["one_line_summary"] = "Changed after review"
        fixture._replace_run_analysis(temp, stale_run, analysis)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = lookup_reviewed_cases.main(["--run-dir", str(stale_run)])
        self.assertEqual(status, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn(str(stale_run.resolve()), stderr.getvalue())
        self.assertIn("旧人工审核已失效", stderr.getvalue())

        _, _, malformed_run = self._fresh_run()
        hr.init_review(malformed_run)
        (malformed_run / hr.SIDECAR_NAME).write_text("{", encoding="utf-8")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = lookup_reviewed_cases.main(["--run-dir", str(malformed_run)])
        self.assertEqual(status, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn(str(malformed_run.resolve()), stderr.getvalue())
        self.assertIn("无法读取 JSON", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
