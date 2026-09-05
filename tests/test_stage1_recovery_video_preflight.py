from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm.payload import (
    _recovery_stage_windows,
    _replace_recovery_full_media,
    _validate_stage1_recovery_video_windows,
)
from flayr_core.llm.pipeline import _maybe_recover_video_facts
from flayr_core.stage_evidence_contracts import (
    STAGE_EVIDENCE_CONTRACT_VERSION,
    stage_codes,
)


class Stage1RecoveryVideoPreflightTests(unittest.TestCase):
    @staticmethod
    def _analysis(root: Path, duration: float) -> dict[str, object]:
        video = root / "creator.mp4"
        video.write_bytes(b"fixture")
        return {
            "videos": {
                "creator": {
                    "duration_seconds": duration,
                    "path": str(video),
                    "work_dir": str(root),
                }
            }
        }

    @staticmethod
    def _unknown_facts() -> dict[str, object]:
        return {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "stage_evidence_checks": [
                {"stage": stage, "status": "unknown", "coverage": "unknown"}
                for stage in stage_codes()
            ],
            "evidence_units": [],
        }

    def test_18_1s_s5_s6_short_window_fails_before_any_native_encoding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            analysis = self._analysis(root, 18.1)
            windows = _recovery_stage_windows(
                analysis,
                "creator",
                ["S5", "S6"],
                s6_tail_review=True,
            )
            self.assertEqual([item[0] for item in windows], ["S5", "S6"])
            self.assertAlmostEqual(windows[0][2] - windows[0][1], 0.5)
            self.assertAlmostEqual(windows[1][2] - windows[1][1], 10.5)

            with (
                patch("flayr_core.llm.payload.can_analyze_native_video", return_value=True),
                patch(
                    "flayr_core.llm.payload.video_to_data_url",
                    return_value="data:video/mp4;base64,clip",
                ) as video_encoder,
                patch(
                    "flayr_core.llm.payload.audio_to_mp3_data_url",
                    return_value="data:audio/mpeg;base64,audio",
                ) as audio_encoder,
                self.assertRaises(ValueError) as raised,
            ):
                _replace_recovery_full_media(
                    [],
                    analysis,
                    "creator",
                    ["S5", "S6"],
                    api_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                    model="qwen3-vl-plus",
                    budget=None,
                    s6_tail_review=True,
                )

            message = str(raised.exception)
            for token in (
                "reason_code=stage1_recovery_video_window_too_short",
                "role=creator",
                "label=S5",
                "start=17.6",
                "end=18.1",
                "min=2",
            ):
                self.assertIn(token, message)
            video_encoder.assert_not_called()
            audio_encoder.assert_not_called()

    def test_23_x_short_nonzero_window_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            windows = _recovery_stage_windows(
                self._analysis(Path(temp_dir), 23.25),
                "creator",
                ["S5"],
            )
        self.assertGreater(windows[0][2] - windows[0][1], 0.0)
        self.assertLess(windows[0][2] - windows[0][1], 2.0)
        with self.assertRaisesRegex(
            ValueError,
            r"reason_code=stage1_recovery_video_window_too_short.*label=S5",
        ):
            _validate_stage1_recovery_video_windows(windows, "creator")

    def test_two_second_boundary_and_60_second_ranges_are_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            boundary_analysis = self._analysis(root, 24.5)
            boundary = _recovery_stage_windows(boundary_analysis, "creator", ["S5"])
            self.assertAlmostEqual(boundary[0][2] - boundary[0][1], 2.0)
            with (
                patch("flayr_core.llm.payload.can_analyze_native_video", return_value=True),
                patch(
                    "flayr_core.llm.payload.video_to_data_url",
                    return_value="data:video/mp4;base64,clip",
                ) as video_encoder,
            ):
                result = _replace_recovery_full_media(
                    [],
                    boundary_analysis,
                    "creator",
                    ["S5"],
                    api_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                    model="qwen3-vl-plus",
                    budget=None,
                )

        self.assertIn("video_url", [item.get("type") for item in result])
        video_encoder.assert_called_once()
        self.assertAlmostEqual(video_encoder.call_args.kwargs["duration"], 2.0)

        with tempfile.TemporaryDirectory() as temp_dir:
            normal = _recovery_stage_windows(
                self._analysis(Path(temp_dir), 60.0),
                "creator",
                ["S2", "S3", "S6"],
            )
        before = list(normal)
        _validate_stage1_recovery_video_windows(normal, "creator")
        self.assertEqual(normal, before)
        self.assertEqual(
            normal,
            [("S2+S3", 2.5, 15.5), ("S6", 54.5, 60.0)],
        )

    def test_native_preflight_checks_valid_window_before_short_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            analysis = self._analysis(root, 60.0)
            with (
                patch(
                    "flayr_core.llm.payload._recovery_stage_windows",
                    return_value=[("S6", 0.0, 2.0), ("S5", 2.0, 3.0)],
                ),
                patch("flayr_core.llm.payload.can_analyze_native_video", return_value=True),
                patch("flayr_core.llm.payload.video_to_data_url") as video_encoder,
                patch("flayr_core.llm.payload.audio_to_mp3_data_url") as audio_encoder,
                self.assertRaisesRegex(
                    ValueError,
                    r"reason_code=stage1_recovery_video_window_too_short.*label=S5",
                ),
            ):
                _replace_recovery_full_media(
                    [],
                    analysis,
                    "creator",
                    ["S5", "S6"],
                    api_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                    model="qwen3-vl-plus",
                    budget=None,
                )

            video_encoder.assert_not_called()
            audio_encoder.assert_not_called()

    def test_non_native_video_path_is_not_blocked_by_short_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            analysis = self._analysis(root, 23.25)
            with (
                patch("flayr_core.llm.payload.can_analyze_native_video", return_value=False),
                patch("flayr_core.llm.payload.can_analyze_native_audio", return_value=True),
                patch("flayr_core.llm.payload.can_send_standalone_audio", return_value=True),
                patch("flayr_core.llm.payload.video_to_data_url") as video_encoder,
                patch(
                    "flayr_core.llm.payload.audio_to_mp3_data_url",
                    return_value="data:audio/mpeg;base64,audio",
                ) as audio_encoder,
            ):
                result = _replace_recovery_full_media(
                    [],
                    analysis,
                    "creator",
                    ["S5"],
                    api_url="https://example.invalid/api",
                    model="vision-test",
                    budget=None,
                )

        self.assertIn("input_audio", [item.get("type") for item in result])
        video_encoder.assert_not_called()
        audio_encoder.assert_called_once()

    def test_pipeline_keeps_short_window_failure_unknown_without_provider_metadata(self) -> None:
        args = SimpleNamespace(
            llm_dry_run=False,
            llm_model="qwen3-vl-plus",
            vision_model="qwen3-vl-plus",
            judgment_model="qwen3-vl-plus",
            llm_api_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            stage1_replay_from=None,
            stage1_resume_from=None,
            provider_replay_from=None,
            _resource_budget=None,
        )
        facts = self._unknown_facts()
        plan = {
            "budget_flag": False,
            "contract_issues": [],
            "targets": ["S5", "S6"],
            "trigger_reasons": ["stage_coverage_incomplete"],
            "s6_explicitly_absent": False,
            "s6_tail_review_required": True,
        }
        short_window_error = ValueError(
            "reason_code=stage1_recovery_video_window_too_short "
            "role=creator label=S5 start=17.6 end=18.1 min=2"
        )
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "flayr_core.llm.pipeline._stage1_recovery_plan",
            return_value=plan,
        ), patch(
            "flayr_core.llm.pipeline._prepare_stage1_recovery_request",
            side_effect=short_window_error,
        ), patch(
            "flayr_core.llm.pipeline._obtain_stage1_recovery_response",
            side_effect=AssertionError("provider must not be reached"),
        ) as obtain, patch(
            "flayr_core.llm.pipeline.fetch_json_completion",
            side_effect=AssertionError("provider must not be reached"),
        ) as fetch:
            result = _maybe_recover_video_facts(
                args,
                {"videos": {}},
                Path(temp_dir),
                "secret",
                "creator",
                facts,
            )

        self.assertIs(result, facts)
        statuses = {item["stage"]: item["status"] for item in result["stage_evidence_checks"]}
        self.assertEqual(statuses["S5"], "unknown")
        self.assertEqual(statuses["S6"], "unknown")
        self.assertNotIn("absent", statuses.values())
        recovery = result["stage1_recovery"]
        self.assertIn("stage1_recovery_video_window_too_short", recovery["failure_reason"])
        self.assertNotIn("provider_status", recovery)
        self.assertNotIn("execution_source", recovery)
        obtain.assert_not_called()
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
