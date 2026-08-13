"""Regression tests for run-directory and release-operation contracts."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import flayr  # noqa: E402
from scripts.flayr_core.resources import ResourceBudget
from scripts.flayr_core.run_state import COMPLETED, FAILED, initialize_run_state, read_run_state


class OperationsContractTests(unittest.TestCase):
    def test_asr_failure_can_only_publish_degraded_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            initialize_run_state(run_dir, job_id="job-1")
            analysis = {
                "analysis_run_state": "completed",
                "stage_analysis": [],
                "improvements": [],
            }
            issues = flayr._transcription_issues(
                {
                    "benchmark": {"transcription_status": "completed"},
                    "creator": {"transcription_status": "failed"},
                }
            )
            self.assertEqual(issues, ["creator: online Fun-ASR transcription_status=failed"])
            flayr._mark_analysis_degraded(run_dir, analysis, issues)
            self.assertEqual(analysis["analysis_run_state"], "degraded")
            manifest = json.loads((run_dir / "degraded_manifest.json").read_text(encoding="utf-8"))
            self.assertIn("creator: online Fun-ASR transcription_status=failed", manifest["reason"])

    def test_nonempty_explicit_output_dir_requires_explicit_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "run"
            output_dir.mkdir()
            (output_dir / "analysis.json").write_text("stale", encoding="utf-8")
            args = SimpleNamespace(output_dir=output_dir, reuse_preprocessing=False, mode="improve")
            with self.assertRaisesRegex(SystemExit, "已存在且非空"):
                flayr.create_run_dir(args)

    def test_web_run_state_does_not_block_new_explicit_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "run"
            output_dir.mkdir()
            (output_dir / "run_state.json").write_text('{"state":"CREATED"}', encoding="utf-8")
            args = SimpleNamespace(output_dir=output_dir, reuse_preprocessing=False, mode="improve")
            self.assertEqual(flayr.create_run_dir(args), output_dir.resolve())

    def test_reuse_removes_known_stale_top_level_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "run"
            output_dir.mkdir()
            stale_outputs = (
                "analysis.json",
                "analysis_replay_context.json",
                "comparison_provider_artifact_ref.json",
                "comparison_provider_meta.json",
                "product_foundation_provider_meta.json",
                "provider_product_foundation.json",
                "stage1_provider_creator_A.json",
                "stage2_provider_S1_S2.json",
                "report.html",
            )
            for name in stale_outputs:
                (output_dir / name).write_text("stale", encoding="utf-8")
            (output_dir / "benchmark").mkdir()
            args = SimpleNamespace(output_dir=output_dir, reuse_preprocessing=True, mode="improve")
            self.assertEqual(flayr.create_run_dir(args), output_dir.resolve())
            for name in stale_outputs:
                self.assertFalse((output_dir / name).exists())
            self.assertTrue((output_dir / "benchmark").is_dir())

    def test_in_place_stage_resume_preserves_only_matching_provider_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "run"
            output_dir.mkdir()
            stage1 = output_dir / "stage1_provider_creator_A.json"
            stage2 = output_dir / "stage2_provider_S1_S2.json"
            analysis = output_dir / "analysis.json"
            for path in (stage1, stage2, analysis):
                path.write_text("stale", encoding="utf-8")
            args = SimpleNamespace(
                output_dir=output_dir,
                reuse_preprocessing=True,
                mode="improve",
                stage1_replay_from=None,
                stage1_resume_from=output_dir,
                stage2_replay_from=None,
                stage2_resume_from=None,
            )

            self.assertEqual(flayr.create_run_dir(args), output_dir.resolve())
            self.assertTrue(stage1.exists())
            self.assertFalse(stage2.exists())
            self.assertFalse(analysis.exists())

    def test_in_place_stage_resume_does_not_preserve_provider_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "run"
            output_dir.mkdir()
            outside = root / "outside.json"
            outside.write_text("external", encoding="utf-8")
            linked = output_dir / "stage1_provider_creator_A.json"
            linked.symlink_to(outside)
            args = SimpleNamespace(
                output_dir=output_dir,
                reuse_preprocessing=True,
                mode="improve",
                stage1_replay_from=None,
                stage1_resume_from=output_dir,
                stage2_replay_from=None,
                stage2_resume_from=None,
            )

            self.assertEqual(flayr.create_run_dir(args), output_dir.resolve())
            self.assertFalse(linked.exists())
            self.assertEqual(outside.read_text(encoding="utf-8"), "external")

    def test_same_location_uses_filesystem_identity_before_path_spelling(self) -> None:
        with mock.patch.object(flayr.os.path, "samefile", return_value=True) as samefile:
            self.assertTrue(
                flayr._paths_refer_to_same_location(Path("/tmp/Run"), Path("/tmp/run"))
            )
        samefile.assert_called_once()

    def test_strict_stage_replay_rejects_the_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "run"
            output_dir.mkdir()
            parser = flayr.build_parser()
            for option in ("--stage1-replay-from", "--stage2-replay-from"):
                args = parser.parse_args(
                    [
                        "compare",
                        "--benchmark-video",
                        __file__,
                        "--creator-video",
                        __file__,
                        "--output-dir",
                        str(output_dir),
                        option,
                        str(output_dir),
                        "--verification-stage",
                        "production",
                    ]
                )
                with self.assertRaisesRegex(SystemExit, "in-place strict replay"):
                    flayr.validate_inputs(args)

    def test_reuse_preserves_keyed_product_foundation_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "run"
            output_dir.mkdir()
            cache = output_dir / "product_foundation.json"
            cache.write_text('{"cache_record_schema_version": 1}', encoding="utf-8")
            args = SimpleNamespace(output_dir=output_dir, reuse_preprocessing=True, mode="improve")

            self.assertEqual(flayr.create_run_dir(args), output_dir.resolve())
            self.assertTrue(cache.exists())

    def test_reuse_rejects_unknown_top_level_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "run"
            output_dir.mkdir()
            (output_dir / "notes.txt").write_text("not a Flayr artifact", encoding="utf-8")
            args = SimpleNamespace(output_dir=output_dir, reuse_preprocessing=True, mode="improve")
            with self.assertRaisesRegex(SystemExit, "未识别的旧内容"):
                flayr.create_run_dir(args)

    def test_report_publish_finishes_lifecycle_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            initialize_run_state(run_dir, job_id="job-1")
            args = SimpleNamespace(
                mode="improve",
                analysis_result_json=None,
                llm_model="test-model",
                llm_api_url="https://example.invalid/v1/chat/completions",
            )
            analysis = {"analysis_run_state": "completed", "mode": "improve"}
            with (
                mock.patch.object(flayr, "write_report", return_value=run_dir / "report.html"),
                mock.patch.object(flayr, "write_bd_report", return_value=run_dir / "bd_report.html"),
                mock.patch.object(flayr, "write_creator_report"),
                mock.patch.object(flayr, "write_success_manifest"),
            ):
                flayr._generate_reports_and_publish(
                    run_dir,
                    args,
                    {"benchmark": Path("benchmark.mp4"), "creator": Path("creator.mp4")},
                    analysis,
                    ResourceBudget(),
                )
            self.assertEqual(read_run_state(run_dir)["state"], COMPLETED)

    def test_report_publish_failure_is_recorded_before_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            initialize_run_state(run_dir, job_id="job-1")
            args = SimpleNamespace(
                mode="improve",
                analysis_result_json=None,
                llm_model="test-model",
                llm_api_url="https://example.invalid/v1/chat/completions",
            )
            with mock.patch.object(flayr, "write_report", side_effect=RuntimeError("render failed")):
                with self.assertRaisesRegex(RuntimeError, "render failed"):
                    flayr._generate_reports_and_publish(
                        run_dir,
                        args,
                        {"benchmark": Path("benchmark.mp4"), "creator": Path("creator.mp4")},
                        {"analysis_run_state": "completed", "mode": "improve"},
                        ResourceBudget(),
                    )
            self.assertEqual(read_run_state(run_dir)["state"], FAILED)


if __name__ == "__main__":
    unittest.main()
