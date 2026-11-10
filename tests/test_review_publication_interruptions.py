from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from tests import test_human_review as fixtures
from scripts.flayr_core import human_review as hr


class ReviewPublicationInterruptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = mock.patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_interrupted_render_removes_pair_without_changing_review_or_sources(
        self,
    ) -> None:
        for renderer, output_name in (
            ("bd_report.write_bd_report", hr.REVIEWED_BD_REPORT_NAME),
            ("creator_report.write_creator_report", hr.REVIEWED_CREATOR_REPORT_NAME),
        ):
            for failure_type in (RuntimeError, KeyboardInterrupt, SystemExit):
                with self.subTest(renderer=renderer, failure=failure_type.__name__):
                    helper = fixtures.HumanReviewTests()
                    temp, run_dir = helper.fresh_run()
                    with temp:
                        run_dir = run_dir.resolve()
                        hr.init_review(run_dir)
                        helper._approve_all(run_dir)
                        outputs = (
                            run_dir / hr.REVIEWED_BD_REPORT_NAME,
                            run_dir / hr.REVIEWED_CREATOR_REPORT_NAME,
                        )
                        hr.write_reviewed_reports(run_dir)
                        self.assertTrue(all(path.is_file() for path in outputs))
                        sources = {
                            path: path.read_bytes()
                            for path in run_dir.rglob("*")
                            if path.is_file() and path not in outputs
                        }
                        self.assertIn(run_dir / "analysis.json", sources)
                        self.assertIn(run_dir / hr.SIDECAR_NAME, sources)
                        failure = failure_type("injected render interruption")

                        def interrupt(
                            root: Path, analysis: dict, **kwargs: object
                        ) -> None:
                            (root / output_name).write_text(
                                "partial report", encoding="utf-8"
                            )
                            raise failure

                        with mock.patch(
                            f"scripts.flayr_core.{renderer}", side_effect=interrupt
                        ):
                            with self.assertRaises(failure_type) as caught:
                                hr.write_reviewed_reports(run_dir)
                        self.assertIs(caught.exception, failure)
                        self.assertFalse(any(path.exists() for path in outputs))
                        self.assertEqual(
                            hr.review_status(hr.load_review(run_dir)), "approved"
                        )
                        self.assertEqual(
                            sources,
                            {
                                path: path.read_bytes()
                                for path in run_dir.rglob("*")
                                if path.is_file() and path not in outputs
                            },
                        )

                        self.assertEqual(hr.write_reviewed_reports(run_dir), outputs)
                        for path in outputs:
                            self.assertIn(
                                '"reviewStatus":"approved"',
                                path.read_text(encoding="utf-8"),
                            )
                        for path, original in sources.items():
                            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
