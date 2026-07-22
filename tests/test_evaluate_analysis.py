from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.evaluate_analysis import (
    _decision_gt_audit,
    _event_time_bounds,
    _human_key_event_audit,
    _layer_attribution,
    _phase_c_audit,
    _stage_oracle_audit,
    severity_diagnostics,
    severity_from_score,
)


class SeverityEvaluationDiagnosticsTest(unittest.TestCase):
    def test_gt_event_time_bounds_reject_reverse_and_nonfinite_values(self) -> None:
        self.assertEqual(_event_time_bounds([1.0, 3.0]), (1.0, 3.0))
        self.assertIsNone(_event_time_bounds([3.0, 1.0]))
        self.assertIsNone(_event_time_bounds([float("nan"), 3.0]))

    def test_score_bucket_uses_derive_boundaries(self) -> None:
        self.assertEqual(severity_from_score(1.2), "small")
        self.assertEqual(severity_from_score(1.21), "medium")
        self.assertEqual(severity_from_score(2.5), "medium")
        self.assertEqual(severity_from_score(2.51), "large")

    def test_near_threshold_mismatch_is_diagnostic_only(self) -> None:
        diagnostics = severity_diagnostics(
            "medium",
            "small",
            {
                "severity_derivation": {
                    "status": "derived",
                    "severity": "small",
                    "S": 1.15,
                    "reason": "E = 标杆执行分 1.0 - 达人执行分 0.5",
                }
            },
        )
        self.assertEqual(diagnostics["ordinal_distance"], 1)
        self.assertEqual(diagnostics["derivation_path"], "threshold")
        self.assertTrue(diagnostics["near_threshold"])

    def test_floor_override_is_not_explained_as_boundary_noise(self) -> None:
        diagnostics = severity_diagnostics(
            "small",
            "medium",
            {
                "severity_derivation": {
                    "status": "derived",
                    "severity": "medium",
                    "S": 0.9,
                    "reason": "landing 下限：标杆钩子立住、达人未立住",
                }
            },
        )
        self.assertEqual(diagnostics["ordinal_distance"], 1)
        self.assertEqual(diagnostics["derivation_path"], "override_or_floor")
        self.assertIsNone(diagnostics["near_threshold"])

    def test_two_band_error_is_preserved(self) -> None:
        diagnostics = severity_diagnostics("large", "small", {})
        self.assertEqual(diagnostics["ordinal_distance"], 2)
        self.assertEqual(diagnostics["derivation_path"], "non_score_path")
        self.assertEqual(diagnostics["decision_mechanism"], "other_non_score_path")

    def test_non_positive_gap_is_not_classified_as_missing_evidence(self) -> None:
        diagnostics = severity_diagnostics(
            "medium",
            "small",
            {
                "severity_derivation": {
                    "status": "derived",
                    "severity": "small",
                    "E": 0,
                    "reason": "E = 标杆执行分 2.0 - 达人执行分 2.0；达人持平或更优（亮点，零差距红线）",
                }
            },
        )
        self.assertEqual(diagnostics["decision_mechanism"], "non_positive_execution_gap")

    def test_both_absent_has_its_own_mechanism(self) -> None:
        diagnostics = severity_diagnostics(
            "large",
            "small",
            {
                "severity_derivation": {
                    "status": "derived",
                    "severity": "small",
                    "E": 0,
                    "reason": "双方均未涉及（执行分均为 0），不进公式",
                }
            },
        )
        self.assertEqual(diagnostics["decision_mechanism"], "both_absent")

    def test_non_score_floor_is_classified_as_structural_override(self) -> None:
        diagnostics = severity_diagnostics(
            "medium",
            "medium",
            {
                "severity_derivation": {
                    "status": "derived",
                    "severity": "medium",
                    "E": 0,
                    "reason": "E = 标杆执行分 2.0 - 达人执行分 2.0；命题锚下限：标杆锚定核心命题",
                }
            },
        )
        self.assertEqual(diagnostics["decision_mechanism"], "structural_override")


class LayeredEvaluationTest(unittest.TestCase):
    def _write_result(self, root: Path, value: dict) -> Path:
        path = root / "analysis.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def test_stage_oracle_separates_execution_and_complete_derive_replay(self) -> None:
        labels = {
            "samples": {
                "sample": {
                    "stages": {"S6": "large"},
                    "stage_oracles": {
                        "S6": {
                            "creator_execution": 0.0,
                            "benchmark_execution": 2.0,
                            "relation": "benchmark_better",
                            "decision_event_ids": [],
                            "confidence": "high",
                            "derive_patch": {},
                        }
                    },
                }
            }
        }
        result = {
            "stage_analysis": [{
                "stage": "S6 CTA",
                "severity": "small",
                "creator_execution": 1.0,
                "benchmark_execution": 1.0,
                "severity_derivation": {
                    "derived_creator_execution": 0.5,
                    "derived_benchmark_execution": 1.0,
                },
                "creator_s6": {"cta_exists": True, "cta_explicit": True},
                "benchmark_s6": {"cta_exists": True, "cta_explicit": True},
            }],
            "video_understanding": {"creator": {"evidence_units": []}, "benchmark": {"evidence_units": []}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_result(Path(tmp), result)
            audit = _stage_oracle_audit(labels, {"sample": path})
        record = audit["records"][0]
        self.assertFalse(record["execution_match"])
        self.assertEqual(record["actual_creator_execution"], 0.5)
        self.assertEqual(record["derive_replay_status"], "complete_oracle_patch")
        self.assertEqual(record["derive_replay_severity"], "large")
        self.assertTrue(record["derive_replay_match"])

    def test_human_key_event_audit_separates_present_and_absent_evidence(self) -> None:
        labels = {
            "samples": {
                "sample": {
                    "key_events": [
                        {
                            "id": "creator_usage",
                            "role": "creator",
                            "stage": "S3",
                            "time_range": [1.0, 3.0],
                            "channels_any": ["visual_fact"],
                            "terms_any": ["涂抹"],
                            "expected_state": "present",
                        },
                        {
                            "id": "creator_certification_absent",
                            "role": "creator",
                            "stage": "S5",
                            "time_range": [0.0, 5.0],
                            "terms_any": ["认证"],
                            "expected_state": "absent",
                        },
                    ]
                }
            }
        }
        result = {
            "videos": {"creator": {"path": "/tmp/creator.mp4", "frame_count": 3}},
            "video_understanding": {
                "creator": {
                    "evidence_units": [{
                        "id": "C1",
                        "time_range": "1.0s - 3.0s",
                        "visual_fact": "达人把粉饼涂抹在脸上",
                    }]
                },
                "benchmark": {"evidence_units": []},
            },
            "stage_analysis": [{"stage": "S3 使用", "creator_evidence_ids": ["C1"]}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_result(Path(tmp), result)
            audit = _human_key_event_audit(labels, {"sample": path})
        self.assertEqual(audit["summary"]["stage1_recall"], 1.0)
        self.assertEqual(audit["summary"]["stage2_use_given_recall"], 1.0)
        self.assertEqual(audit["summary"]["absence_false_positive_rate"], 0.0)
        self.assertEqual(audit["unexpected_absence_claims"], [])

    def test_phase_c_audit_detects_regression(self) -> None:
        labels = {"samples": {"sample": {"stages": {"S4": "medium"}}}}
        result = {
            "phase_c_review": {
                "applied": True,
                "requested_stages": ["S4"],
                "before_stage_analysis": [{"stage": "S4 效果", "severity": "medium"}],
                "after_stage_analysis": [{"stage": "S4 效果", "severity": "small"}],
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_result(Path(tmp), result)
            audit = _phase_c_audit(labels, {"sample": path})
        self.assertEqual(audit["summary"]["regressed"], 1)
        self.assertEqual(audit["summary"]["net_corrections"], -1)

    def test_decision_gt_uses_closed_reference_ids(self) -> None:
        labels = {
            "samples": {
                "sample": {
                    "decision_gt": {
                        "top_root_causes": [
                            {"priority": 1, "reference_id": "selling_point_route"},
                            {"priority": 2, "reference_id": "S4"},
                        ]
                    }
                }
            }
        }
        result = {"commercial_priorities": [{"reference_id": "S4"}, {"reference_id": "S3"}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_result(Path(tmp), result)
            audit = _decision_gt_audit(labels, {"sample": path})
        self.assertEqual(audit["summary"]["root_cause_recall"], 0.5)
        self.assertFalse(audit["records"][0]["exact_order_match"])

    def test_layer_attribution_stops_at_earliest_proven_failure(self) -> None:
        mismatches = [{"sample_id": "sample", "stage": "S3", "expected": "large", "final": "small"}]
        human_events = {
            "records": [{
                "sample_id": "sample", "event_id": "e1", "expected_state": "present",
                "source_artifact_ready": True, "stage1_recalled": False, "stage2_referenced": False,
            }]
        }
        oracles = {"records": [{"sample_id": "sample", "stage": "S3", "decision_event_ids": ["e1"]}]}
        audit = _layer_attribution(mismatches, human_events, oracles, {"records": []})
        self.assertEqual(audit["records"][0]["layer"], "L1_fact_recall")


if __name__ == "__main__":
    unittest.main()
