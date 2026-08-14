from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.evaluate_analysis import (
    _decision_gt_audit,
    _event_time_bounds,
    _human_key_event_audit,
    _layer_attribution,
    _phase_c_audit,
    _stage_oracle_audit,
    blind_contract_violations,
    evaluate,
    ground_truth_label_inventory,
    normalize_human_gap,
    promotion_readiness,
    semantic_acceptance,
    severity_diagnostics,
)


class SeverityEvaluationDiagnosticsTest(unittest.TestCase):
    def test_evaluate_uses_semantic_gap_axis_and_tracks_unavailable_predictions(self) -> None:
        labels = {
            "samples": {
                "sample": {
                    "partition": "new",
                    "human_gap": {
                        "S1": "none",
                        "S2": "large",
                        "S3": "none",
                    },
                }
            }
        }
        result = {
            "comparison_eligibility": {"direct_product_stages": ["S1", "S2"]},
            "stage_analysis": [
                {
                    "stage": "S1 Hook",
                    "severity": None,
                    "model_gap_magnitude": "none",
                    "model_severity": None,
                    "stage_state": "completed",
                    "analysis_status": "grounded",
                },
                {
                    "stage": "S2 Product",
                    "severity": "large",
                    "model_gap_magnitude": "medium",
                    "model_severity": "medium",
                    "severity_derivation": {
                        "status": "constrained",
                        "constraints": [{"kind": "floor", "level": "large"}],
                    },
                },
                {
                    "stage": "S3 Usage",
                    "severity": None,
                    "model_gap_magnitude": "none",
                    "model_severity": None,
                    "stage_state": "unknown",
                    "analysis_status": "evidence_blocked",
                },
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "analysis.json"
            path.write_text(json.dumps(result), encoding="utf-8")
            report = evaluate(labels, {}, {"sample": path})

        self.assertEqual(report["summary"]["valid_gt_cells"], 3)
        self.assertEqual(report["summary"]["evaluated"], 2)
        self.assertEqual(report["summary"]["matched"], 2)
        self.assertEqual(report["summary"]["prediction_unavailable"], 1)
        self.assertEqual(report["summary"]["accuracy"], 0.6667)
        self.assertEqual(report["summary"]["accuracy_on_available"], 1.0)
        self.assertEqual(report["summary"]["evaluation_coverage"], 0.6667)
        self.assertEqual(report["schema_version"], 5)
        self.assertEqual(report["semantic_acceptance"]["overall"], "incomplete")
        self.assertEqual(report["confusion_matrix"]["values"]["none"]["none"], 1)
        self.assertEqual(report["prediction_unavailable"][0]["stage"], "S3")

    def test_promotion_lock_rejects_visual_model_drift(self) -> None:
        rows = [
            {
                "sample_id": "sample",
                "partition": "blind",
                "stage": "S1",
                "expected": "small",
                "matched": True,
                "ordinal_distance": 0,
                "run_metadata": {
                    "llm_model": "qwen3.7-plus",
                    "judgment_model": "qwen3.7-plus",
                    "vision_model": "other-vision",
                    "comparison_temperature": 0.0,
                },
            }
        ]
        lock = {
            "status": "frozen",
            "sample_ids": ["sample"],
            "model_config": {
                "schema_version": 2,
                "model": "qwen3.7-plus",
                "judgment_model": "qwen3.7-plus",
                "vision_model": "qwen3-vl-plus",
                "temperature": 0.0,
            },
        }
        with patch("scripts.evaluate_analysis.verify_cohort_lock", return_value=[]):
            readiness = promotion_readiness(
                rows,
                {"samples": {}},
                {
                    "samples": [
                        {
                            "id": "sample",
                            "group": "blind",
                            "product_category": "test",
                            "target_market": "my",
                        }
                    ]
                },
                lock,
                {},
                {},
                {},
            )
        self.assertIn(
            "analysis_result vision model 与 cohort lock 不一致或缺失",
            readiness["reasons"],
        )

    def test_canonical_human_gap_keeps_none_and_uncertain_out_of_severity_normalization(self) -> None:
        self.assertEqual(normalize_human_gap("none"), "none")
        self.assertEqual(normalize_human_gap("uncertain"), "uncertain")
        self.assertEqual(normalize_human_gap("not_applicable"), "na")

    def test_gt_inventory_separates_not_applicable_from_missing(self) -> None:
        inventory = ground_truth_label_inventory({
            "samples": {
                "sample": {
                    "stages": {"S1": "small", "S2": "na"},
                    "stage_label_statuses": {
                        "S2": {"status": "not_applicable", "reason": "该阶段不适用。"}
                    },
                }
            }
        })
        self.assertEqual(inventory["counts"]["not_applicable"], 1)
        self.assertEqual(inventory["counts"]["missing"], 4)
        self.assertEqual(inventory["non_labeled_stages"][0]["reason"], "该阶段不适用。")
        whole_video = ground_truth_label_inventory({
            "samples": {
                "whole": {
                    "evaluation_scope": "whole_video_observation",
                    "overall_verdict": "viable",
                    "overall_reason": "该样本只做全片观察。",
                }
            }
        })
        self.assertEqual(whole_video["counts"], {})
        self.assertEqual(whole_video["whole_video_observation_samples"], ["whole"])
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

    def test_semantic_acceptance_requires_every_human_axis(self) -> None:
        rows = [
            {
                "sample_id": f"sample-{index}",
                "matched": True,
                "ordinal_distance": 0,
            }
            for index in range(12)
        ]
        gate = semantic_acceptance(rows, [], {"records": []}, {"summary": {}})
        self.assertEqual(gate["gap_magnitude"]["status"], "passed")
        self.assertEqual(gate["relation"]["status"], "unavailable")
        self.assertEqual(gate["fact_recall"]["status"], "unavailable")
        self.assertEqual(gate["overall"], "incomplete")
        self.assertTrue(gate["independent_from_engineering_acceptance"])

    def test_semantic_acceptance_passes_only_when_all_components_pass(self) -> None:
        rows = [
            {
                "sample_id": f"sample-{index}",
                "matched": True,
                "ordinal_distance": 0,
            }
            for index in range(12)
        ]
        relation_records = [
            {"expected_relation": "benchmark_better", "relation_match": True}
            for _ in range(12)
        ]
        gate = semantic_acceptance(
            rows,
            [],
            {"records": relation_records},
            {"summary": {"present_events": 12, "stage1_recall": 1.0}},
        )
        self.assertEqual(gate["overall"], "passed")
        self.assertEqual(gate["gap_magnitude"]["status"], "passed")
        self.assertEqual(gate["relation"]["status"], "passed")
        self.assertEqual(gate["fact_recall"]["status"], "passed")

        rows[0]["ordinal_distance"] = 2
        rows[0]["matched"] = False
        failed = semantic_acceptance(
            rows,
            [],
            {"records": relation_records},
            {"summary": {"present_events": 12, "stage1_recall": 1.0}},
        )
        self.assertEqual(failed["gap_magnitude"]["status"], "failed")
        self.assertEqual(failed["overall"], "failed")


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
                "schema_version": 2,
                "snapshot_schema": "phase_c_patch_snapshot_v1",
                "applied": True,
                "requested_stages": ["S4"],
                "patches": [{
                    "stage": "S4",
                    "before": {"resolution": {"severity": "medium"}},
                    "after": {"resolution": {"severity": "small"}},
                }],
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
