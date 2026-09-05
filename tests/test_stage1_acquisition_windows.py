"""Stage1 native-video acquisition window contract regressions."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm.pipeline import (
    _extend_stage1_acquisition_for_recovery,
    _prepare_stage1_recovery_request,
)
from flayr_core.stage_evidence_contracts import (
    STAGE1_ACQUISITION_VERSION,
    normalize_stage1_acquisition,
    stage1_acquisition_issues,
    stage_codes,
    stage_evidence_contract,
)


class Stage1AcquisitionWindowTests(unittest.TestCase):
    @staticmethod
    def _acquisition(
        *,
        version: int = STAGE1_ACQUISITION_VERSION,
        input_mode: str = "native_video",
        duration: float | None = 10.0,
        windows: list[dict[str, object]] | None = None,
        errors: list[str] | None = None,
        status: str = "complete",
    ) -> dict[str, object]:
        return {
            "version": version,
            "source": "pipeline",
            "status": status,
            "input_mode": input_mode,
            "speech_mode": "visual_driven",
            "duration_seconds": duration,
            "channels": {
                "visual": {
                    "status": "ready",
                    "coverage": "full",
                    "count": 1,
                    "boundary_precision": "continuous",
                },
                "voiceover": {"status": "unknown", "coverage": "unknown", "count": 0},
                "subtitle": {"status": "unknown", "coverage": "unknown", "count": 0},
                "audio": {"status": "unknown", "coverage": "none", "count": 0},
            },
            "stage_coverage": {
                stage: {"status": "observed", "count": 1}
                for stage in stage_codes()
            },
            "visual_input_timestamps": [],
            "native_video_windows": windows if windows is not None else [],
            "provider_artifacts": [],
            "errors": errors or [],
        }

    @staticmethod
    def _side(acquisition: dict[str, object], *, time_range: str = "2s - 4s", status: str = "present") -> dict[str, object]:
        stage = "S4"
        contract = stage_evidence_contract(stage)
        checks = []
        for code in stage_codes():
            if code == stage:
                checks.append(
                    {
                        "stage": stage,
                        "status": status,
                        "coverage": "complete",
                        "evidence_ids": ["C4"] if status == "present" else [],
                        "observed_signals": list(contract.required_signals) if status == "present" else [],
                        "missing_signals": list(contract.required_signals) if status == "absent" else [],
                        "signal_bindings": {
                            signal: {
                                "status": "supported",
                                "evidence_ids": ["C4"],
                                "invalid_evidence_ids": [],
                                "reason": "test binding",
                            }
                            for signal in contract.required_signals
                        } if status == "present" else {},
                    }
                )
            else:
                checks.append(
                    {
                        "stage": code,
                        "status": "unknown",
                        "coverage": "unknown",
                        "evidence_ids": [],
                        "observed_signals": [],
                        "missing_signals": [],
                    }
                )
        return {
            "stage_evidence_checks": checks,
            "stage1_acquisition": acquisition,
            "evidence_units": [
                {
                    "id": "C4",
                    "time_range": time_range,
                    "visual_fact": "直接可见的效果变化",
                    "evidence_strength": "direct",
                }
            ],
        }

    @staticmethod
    def _analysis(duration: float | None = 10.0) -> dict[str, object]:
        return {"videos": {"creator": {"duration_seconds": duration}}}

    @staticmethod
    def _video_payload(count: int = 1) -> dict[str, object]:
        return {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "video_url", "video_url": {"url": f"data:video/mp4;base64:{index}"}}
                        for index in range(count)
                    ],
                }
            ]
        }

    def test_exact_native_union_is_full_without_rounding_endpoints(self) -> None:
        duration = 10.3000000007
        windows = [
            {"start_seconds": 0.0, "end_seconds": 4.1000000003},
            {"start_seconds": 4.1000000003, "end_seconds": duration},
        ]
        normalized = normalize_stage1_acquisition(
            self._acquisition(duration=duration, windows=windows)
        )
        self.assertEqual(normalized["channels"]["visual"]["coverage"], "full")
        self.assertEqual(normalized["status"], "complete")
        self.assertEqual(normalized["native_video_windows"], windows)
        self.assertTrue(
            all(item["status"] == "observed" for item in normalized["stage_coverage"].values())
        )

    def test_partial_and_holey_native_union_is_not_full(self) -> None:
        for windows in (
            [{"start_seconds": 0.0, "end_seconds": 4.0}],
            [
                {"start_seconds": 0.0, "end_seconds": 4.0},
                {"start_seconds": 6.0, "end_seconds": 10.0},
            ],
        ):
            with self.subTest(windows=windows):
                normalized = normalize_stage1_acquisition(
                    self._acquisition(duration=10.0, windows=windows)
                )
                self.assertEqual(normalized["channels"]["visual"]["coverage"], "partial")
                self.assertEqual(normalized["status"], "partial")
                self.assertTrue(
                    all(item["status"] == "unknown" for item in normalized["stage_coverage"].values())
                )

    def test_old_or_missing_native_metadata_cannot_claim_full(self) -> None:
        old = normalize_stage1_acquisition(
            self._acquisition(version=4, windows=[{"start_seconds": 0.0, "end_seconds": 10.0}])
        )
        self.assertIsNone(old["version"])
        self.assertNotEqual(old["channels"]["visual"]["coverage"], "full")

        missing = normalize_stage1_acquisition(
            self._acquisition(windows=[], status="complete")
        )
        self.assertEqual(missing["channels"]["visual"]["coverage"], "none")
        self.assertEqual(missing["channels"]["visual"]["status"], "unknown")
        self.assertEqual(missing["status"], "unknown")
        self.assertTrue(all(item["status"] == "unknown" for item in missing["stage_coverage"].values()))

        canonical = normalize_stage1_acquisition(
            self._acquisition(
                input_mode="canonical_frames",
                windows=[],
                status="complete",
            )
            | {"visual_input_timestamps": [1.0]}
        )
        self.assertEqual(canonical["channels"]["visual"]["coverage"], "sampled")
        self.assertEqual(canonical["status"], "partial")

        unknown_mode = normalize_stage1_acquisition(
            self._acquisition(input_mode="unknown", windows=[], status="complete")
            | {"visual_input_timestamps": [1.0]}
        )
        self.assertEqual(unknown_mode["channels"]["visual"]["coverage"], "sampled")
        self.assertEqual(unknown_mode["status"], "partial")

    def test_invalid_window_subset_and_errors_are_idempotent_and_not_full(self) -> None:
        raw = self._acquisition(
            duration=10.0,
            windows=[
                {"start_seconds": 0.0, "end_seconds": 5.0},
                {"start_seconds": 5.0, "end_seconds": 10.0},
                {"start_seconds": 8.0, "end_seconds": 11.0},
                {"start_seconds": float("nan"), "end_seconds": 10.0},
                {"start_seconds": 3.0, "end_seconds": 2.0},
            ],
        )
        first = normalize_stage1_acquisition(raw)
        second = normalize_stage1_acquisition(first)
        self.assertEqual(first, second)
        self.assertEqual(
            first["native_video_windows"],
            [
                {"start_seconds": 0.0, "end_seconds": 5.0},
                {"start_seconds": 5.0, "end_seconds": 10.0},
            ],
        )
        self.assertIn("native_video_windows_invalid", first["errors"])
        self.assertNotEqual(first["channels"]["visual"]["coverage"], "full")
        self.assertTrue(all(item["status"] == "unknown" for item in first["stage_coverage"].values()))

    def test_unknown_duration_windows_cannot_support_positive_visual_fact(self) -> None:
        side = self._side(
            self._acquisition(duration=None, windows=[{"start_seconds": 0.0, "end_seconds": 10.0}])
        )
        issues = stage1_acquisition_issues(side, "S4")
        self.assertIn("S4:acquisition_visual_input_unobserved", issues)

    def test_native_windows_must_cover_positive_evidence_range_without_a_frame(self) -> None:
        covered = self._side(
            self._acquisition(windows=[{"start_seconds": 0.0, "end_seconds": 5.0}]),
            time_range="2s - 4s",
        )
        self.assertEqual(stage1_acquisition_issues(covered, "S4"), [])

        outside = self._side(
            self._acquisition(windows=[{"start_seconds": 0.0, "end_seconds": 5.0}]),
            time_range="5.1s - 6s",
        )
        self.assertIn("S4:acquisition_visual_input_unobserved", stage1_acquisition_issues(outside, "S4"))

        frame_supported = self._side(
            self._acquisition(
                windows=[{"start_seconds": 0.0, "end_seconds": 1.0}],
            ),
            time_range="5.1s - 6s",
        )
        frame_supported["stage1_acquisition"]["visual_input_timestamps"] = [5.5]
        self.assertEqual(stage1_acquisition_issues(frame_supported, "S4"), [])

    def test_absent_claim_still_requires_full_visual_coverage(self) -> None:
        side = self._side(
            self._acquisition(windows=[{"start_seconds": 0.0, "end_seconds": 5.0}]),
            status="absent",
        )
        self.assertIn(
            "S4:acquisition_channel_coverage_incomplete:visual",
            stage1_acquisition_issues(side, "S4"),
        )

    def test_recovery_payload_video_requires_matching_windows_before_extension(self) -> None:
        kwargs = {
            "analysis": self._analysis(),
            "role": "creator",
            "facts": {},
            "payload": self._video_payload(),
            "recovery_visual_inputs": [],
            "direct_audio": False,
        }
        with self.assertRaisesRegex(ValueError, "no matching media windows"):
            _extend_stage1_acquisition_for_recovery(**kwargs, media_windows=[])
        with self.assertRaisesRegex(ValueError, "video-block count"):
            _extend_stage1_acquisition_for_recovery(
                **kwargs,
                media_windows=[
                    {"role": "creator", "window_label": "S4", "start_seconds": 0.0, "end_seconds": 5.0},
                    {"role": "creator", "window_label": "S4", "start_seconds": 5.0, "end_seconds": 10.0},
                ],
            )

    def test_recovery_discards_invalid_old_range_but_new_full_video_can_reestablish_full(self) -> None:
        old = self._acquisition(
            windows=[
                {"start_seconds": 0.0, "end_seconds": 5.0},
                {"start_seconds": 5.0, "end_seconds": 10.0},
                {"start_seconds": 0.0, "end_seconds": 11.0},
            ]
        )
        old["errors"] = ["native_video_windows_invalid"]
        facts = {"stage1_acquisition": old}
        result = _extend_stage1_acquisition_for_recovery(
            self._analysis(),
            "creator",
            facts,
            {"messages": []},
            [],
            direct_audio=False,
            media_windows=[],
        )
        self.assertIn("native_video_windows_invalid", result["errors"])
        self.assertNotEqual(result["channels"]["visual"]["coverage"], "full")

        result = _extend_stage1_acquisition_for_recovery(
            self._analysis(),
            "creator",
            facts,
            self._video_payload(),
            [],
            direct_audio=False,
            media_windows=[
                {"role": "creator", "window_label": "S4", "start_seconds": 0.0, "end_seconds": 10.0}
            ],
        )
        self.assertEqual(result["channels"]["visual"]["coverage"], "full")
        self.assertEqual(
            result["native_video_windows"],
            [{"start_seconds": 0.0, "end_seconds": 10.0}],
        )
        self.assertNotIn("native_video_windows_invalid", result["errors"])

    def test_prepare_rejects_video_window_mismatch_before_provider_call(self) -> None:
        args = SimpleNamespace(
            vision_model="vision",
            llm_model="vision",
            llm_api_url="https://example.invalid/api",
            llm_image_limit=4,
            _resource_budget=None,
        )
        planned = [
            {"role": "creator", "window_label": "S4+S5", "start_seconds": 0.0, "end_seconds": 10.0},
            {"role": "creator", "window_label": "S4+S5", "start_seconds": 0.0, "end_seconds": 10.0},
        ]
        with patch(
            "flayr_core.llm.pipeline.stage1_recovery_media_windows",
            return_value=planned,
        ), patch(
            "flayr_core.llm.pipeline.select_stage_recovery_visual_inputs",
            return_value=[],
        ), patch(
            "flayr_core.llm.pipeline.build_video_fact_recovery_payload",
            return_value=self._video_payload(),
        ):
            with self.assertRaisesRegex(ValueError, "video-block count"):
                _prepare_stage1_recovery_request(
                    args=args,
                    analysis=self._analysis(),
                    role="creator",
                    facts={},
                    targets=["S4", "S5"],
                    review_s6_tail=False,
                )


if __name__ == "__main__":
    unittest.main()
