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
)


class SeverityEvaluationDiagnosticsTest(unittest.TestCase):
    def test_gt_event_time_bounds_reject_reverse_and_nonfinite_values(self) -> None:
        self.assertEqual(_event_time_bounds([1.0, 3.0]), (1.0, 3.0))
        self.assertIsNone(_event_time_bounds([3.0, 1.0]))
        self.assertIsNone(_event_time_bounds([float("nan"), 3.0]))

    def test_model_preserved_has_no_score_or_threshold_path(self) -> None:
        diagnostics = severity_diagnostics(
            "medium",
            "medium",
            {
                "severity_derivation": {
                    "status": "model_preserved",
                    "severity": "medium",
                    "constraints": [],
                }
            },
        )
        self.assertEqual(diagnostics["score"], None)
        self.assertEqual(diagnostics["score_bucket"], None)
        self.assertEqual(diagnostics["derivation_path"], "model_preserved")
        self.assertEqual(diagnostics["decision_mechanism"], "model_default")
        self.assertIsNone(diagnostics["near_threshold"])

    def test_constraint_path_reports_clamp_without_score(self) -> None:
        diagnostics = severity_diagnostics(
            "small",
            "medium",
            {
                "severity_derivation": {
                    "status": "constrained",
                    "severity": "medium",
                    "constraints": [{"kind": "floor", "level": "medium", "rule": "S1_landing_floor"}],
                }
            },
        )
        self.assertEqual(diagnostics["ordinal_distance"], 1)
        self.assertEqual(diagnostics["derivation_path"], "constraint")
        self.assertEqual(diagnostics["decision_mechanism"], "floor_ceiling_clamp")
        self.assertIsNone(diagnostics["near_threshold"])

    def test_constraint_conflict_is_explicit(self) -> None:
        diagnostics = severity_diagnostics(
            "small",
            "small",
            {
                "severity_derivation": {
                    "status": "conflict",
                    "severity": "small",
                    "constraints": [
                        {"kind": "floor", "level": "large", "rule": "S1_hook_exists_floor"},
                        {"kind": "ceiling", "level": "medium", "rule": "S5_no_trust_ceiling"},
                    ],
                }
            },
        )
        self.assertEqual(diagnostics["derivation_path"], "constraint_conflict")
        self.assertEqual(diagnostics["decision_mechanism"], "floor_ceiling_conflict")
        self.assertTrue(diagnostics["constraint_conflict"])


class LayeredEvaluationTest(unittest.TestCase):
    def _write_result(self, root: Path, value: dict) -> Path:
        path = root / "analysis.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def test_stage_oracle_separates_execution_and_complete_derive_replay(self) -> None:
        labels = {
            "samples": {
                "sample": {
                    "stages": {"S6": "small"},
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
        self.assertEqual(record["derive_replay_severity"], "small")
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
