from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm.payload import (
    _recovery_stage_windows,
    _replace_recovery_full_media,
    _validate_stage1_recovery_video_windows,
    build_video_fact_recovery_payload,
)
from flayr_core.llm.pipeline import _maybe_recover_video_facts
from flayr_core.stage_evidence_contracts import (
    STAGE_EVIDENCE_CONTRACT_VERSION,
    stage_codes,
)


class Stage1RecoveryVideoPreflightTests(unittest.TestCase):
    @staticmethod
    def _analysis(root: Path, duration: Any) -> dict[str, object]:
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

    def test_normal_durations_use_one_full_context_video_block(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for duration in (2.0, 14.58, 18.1, 23.25):
                with self.subTest(duration=duration), patch(
                    "flayr_core.llm.payload.can_analyze_native_video", return_value=True
                ), patch(
                    "flayr_core.llm.payload.video_to_data_url",
                    return_value="data:video/mp4;base64,clip",
                ) as video_encoder, patch(
                    "flayr_core.llm.payload.audio_to_mp3_data_url",
                    return_value="data:audio/mpeg;base64,audio",
                ) as audio_encoder:
                    analysis = self._analysis(root, duration)
                    windows = _recovery_stage_windows(
                        analysis,
                        "creator",
                        ["S5", "S6"],
                        s6_tail_review=True,
                    )
                    self.assertEqual(windows, [("S5+S6", 0.0, duration)])
                    result = _replace_recovery_full_media(
                        [],
                        analysis,
                        "creator",
                        ["S5", "S6"],
                        api_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                        model="qwen3-vl-plus",
                        budget=None,
                        s6_tail_review=True,
                    )
                    self.assertEqual(
                        sum(item.get("type") == "video_url" for item in result),
                        1,
                    )
                    video_encoder.assert_called_once()
                    self.assertEqual(video_encoder.call_args.kwargs["start"], 0.0)
                    self.assertEqual(video_encoder.call_args.kwargs["duration"], duration)
                    audio_encoder.assert_not_called()
                    video_encoder.reset_mock()

    def test_native_recovery_rejects_video_shorter_than_two_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            analysis = self._analysis(root, 1.999)
            windows = _recovery_stage_windows(analysis, "creator", ["S5"])
            self.assertEqual(windows, [("S5", 0.0, 1.999)])
            with self.assertRaisesRegex(
                ValueError,
                r"reason_code=stage1_recovery_video_window_too_short.*label=S5",
            ):
                _validate_stage1_recovery_video_windows(windows, "creator")
            with (
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
                    ["S5"],
                    api_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                    model="qwen3-vl-plus",
                    budget=None,
                )
            video_encoder.assert_not_called()
            audio_encoder.assert_not_called()

    def test_invalid_duration_fails_closed_before_any_media_encoding(self) -> None:
        for invalid_duration in (None, "not-a-duration"):
            with tempfile.TemporaryDirectory() as temp_dir, self.subTest(
                duration=invalid_duration
            ):
                analysis = self._analysis(Path(temp_dir), invalid_duration)
                with (
                    patch("flayr_core.llm.payload.can_analyze_native_video", return_value=True),
                    patch("flayr_core.llm.payload.video_to_data_url") as video_encoder,
                    patch("flayr_core.llm.payload.audio_to_mp3_data_url") as audio_encoder,
                    self.assertRaisesRegex(
                        ValueError,
                        r"reason_code=stage1_recovery_video_duration_invalid",
                    ),
                ):
                    _replace_recovery_full_media(
                        [],
                        analysis,
                        "creator",
                        ["S5"],
                        api_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                        model="qwen3-vl-plus",
                        budget=None,
                    )
                video_encoder.assert_not_called()
                audio_encoder.assert_not_called()

    def test_native_encoder_failure_does_not_fall_back_to_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            analysis = self._analysis(Path(temp_dir), 181.0)
            with (
                patch("flayr_core.llm.payload.can_analyze_native_video", return_value=True),
                patch("flayr_core.llm.payload.video_to_data_url", return_value=None) as video_encoder,
                patch(
                    "flayr_core.llm.payload.audio_to_mp3_data_url",
                    return_value="data:audio/mpeg;base64,audio",
                ) as audio_encoder,
                self.assertRaisesRegex(
                    ValueError,
                    r"reason_code=stage1_recovery_video_encoding_failed.*label=S5",
                ),
            ):
                _replace_recovery_full_media(
                    [],
                    analysis,
                    "creator",
                    ["S5"],
                    api_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                    model="qwen3-vl-plus",
                    budget=None,
                )
            video_encoder.assert_called_once()
            audio_encoder.assert_not_called()

    def test_recovery_payload_entry_reencodes_one_full_context_for_multiple_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            analysis = self._analysis(root, 18.782993)
            initial_payload = {
                "model": "test-model",
                "messages": [
                    {"role": "system", "content": "system"},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "initial"},
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/jpeg;base64,frame-one"},
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/jpeg;base64,frame-one"},
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/jpeg;base64,frame-three"},
                            },
                            {"type": "video_url", "video_url": {"url": "stale-video"}},
                            {"type": "input_audio", "input_audio": {"data": "stale-audio"}},
                        ],
                    },
                ],
            }
            visual_inputs = [
                {
                    "role": "creator",
                    "label": "creator @ 1.0s frame-one.jpg",
                    "path": "/private/not-forwarded/frame-one.jpg",
                },
                {
                    "role": "creator",
                    "label": "creator @ 9.3s frame-two.jpg",
                    "path": "/private/not-forwarded/frame-two.jpg",
                },
                {
                    "role": "creator",
                    "label": "creator @ 17.7s frame-three.jpg",
                    "path": "/private/not-forwarded/frame-three.jpg",
                },
            ]
            media_windows = [
                {
                    "role": "creator",
                    "window_label": "S2+S5+S6",
                    "start_seconds": 0.0,
                    "end_seconds": 18.782993,
                }
            ]
            with (
                patch(
                    "flayr_core.llm.payload.build_video_fact_payload",
                    return_value=initial_payload,
                ) as primary_builder,
                patch("flayr_core.llm.payload.can_analyze_native_video", return_value=True),
                patch(
                    "flayr_core.llm.payload.load_transcript_words",
                    return_value=[
                        {"start_seconds": 3.0, "end_seconds": 3.4, "text": "SAME_WINDOW"}
                    ],
                ),
                patch(
                    "flayr_core.llm.payload.video_to_data_url",
                    return_value="data:video/mp4;base64,recovery",
                ) as video_encoder,
                patch("flayr_core.llm.payload.audio_to_mp3_data_url") as audio_encoder,
            ):
                payload = build_video_fact_recovery_payload(
                    "test-model",
                    "creator",
                    analysis,
                    visual_inputs,
                    {"evidence_units": [], "stage_evidence_checks": []},
                    ["S2", "S5", "S6"],
                    api_url="https://example.invalid/api",
                    media_windows=media_windows,
                )

            primary_builder.assert_called_once()
            video_encoder.assert_called_once_with(
                Path(analysis["videos"]["creator"]["path"]),
                start=0.0,
                duration=18.782993,
                budget=None,
            )
            audio_encoder.assert_not_called()
            content = payload["messages"][1]["content"]
            self.assertEqual(
                sum(item.get("type") == "video_url" for item in content),
                1,
            )
            self.assertNotIn("stale-video", str(payload))
            self.assertNotIn("stale-audio", str(payload))
            image_labels = [
                item["text"]
                for item in content
                if item.get("type") == "text" and item.get("text", "").startswith("图片：")
            ]
            self.assertEqual(
                image_labels,
                [
                    "图片：creator @ 1.0s frame-one.jpg",
                    "图片：creator @ 9.3s frame-two.jpg",
                    "图片：creator @ 17.7s frame-three.jpg",
                ],
            )
            self.assertTrue(all("S5" not in label for label in image_labels))
            self.assertNotIn("/private/not-forwarded", str(payload))
            asr_text = next(
                item["text"]
                for item in content
                if item.get("type") == "text"
                and item.get("text", "").startswith("Stage1-C 完整上下文 Fun-ASR")
            )
            self.assertIn("S2+S5+S6", asr_text)
            self.assertIn("0.0s - 18.8s", asr_text)
            self.assertIn("SAME_WINDOW", asr_text)

    def test_native_capability_with_missing_source_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            analysis = self._analysis(root, 18.1)
            Path(analysis["videos"]["creator"]["path"]).unlink()
            with (
                patch("flayr_core.llm.payload.can_analyze_native_video", return_value=True),
                patch("flayr_core.llm.payload.video_to_data_url") as video_encoder,
                patch("flayr_core.llm.payload.audio_to_mp3_data_url") as audio_encoder,
                self.assertRaisesRegex(
                    ValueError,
                    r"reason_code=stage1_recovery_video_source_missing",
                ),
            ):
                _replace_recovery_full_media(
                    [],
                    analysis,
                    "creator",
                    ["S5"],
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
        self.assertEqual(audio_encoder.call_args.kwargs["start"], 0.0)
        self.assertEqual(audio_encoder.call_args.kwargs["duration"], 23.25)

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
