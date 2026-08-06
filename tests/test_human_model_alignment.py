from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.evaluate_human_model_alignment import (
    _read_result_artifact,
    _safe_component_map,
    _source_identity_audit,
    aggregate_model,
    score_extraction,
    score_judgment,
)


def _labels() -> dict[str, dict[str, object]]:
    return {
        "S1": {"status": "labeled", "gap_magnitude": "medium", "relation": "benchmark_better"},
        "S2": {"status": "labeled", "gap_magnitude": "none", "relation": "tie"},
        "S3": {"status": "labeled", "gap_magnitude": "large", "relation": "benchmark_better"},
        "S4": {"status": "not_applicable", "gap_magnitude": "na", "relation": None},
        "S5": {"status": "uncertain", "gap_magnitude": "uncertain", "relation": None},
        "S6": {"status": "missing", "gap_magnitude": None, "relation": None},
    }


def _judgment_result() -> dict[str, object]:
    rows = []
    for stage, relation, gap in (
        ("S1 Hook", "creator_better", "medium"),
        ("S2 产品引出", "tie", "small"),
        ("S3 使用过程", "benchmark_better", "large"),
    ):
        rows.append(
            {
                "stage": stage,
                "relation": relation,
                "gap_magnitude": gap,
                "confidence": "high",
                "creator": {"observation_state": "complete", "evidence_ids": [], "reason": "依据"},
                "benchmark": {"observation_state": "complete", "evidence_ids": [], "reason": "依据"},
                "rationale": "阶段依据",
            }
        )
    return {"stage_judgments": rows}


def _quality() -> dict[str, str]:
    return {
        "subject": "correct",
        "visibility": "clear",
        "composition": "central",
        "completion": "complete",
        "proof": "direct_comparison",
        "causal_link": "supported",
    }


class HumanModelAlignmentTests(unittest.TestCase):
    def test_judgment_keeps_na_uncertain_missing_and_direction_errors_separate(self) -> None:
        score = score_judgment(_judgment_result(), _labels(), artifact_status="completed")
        denominator = score["denominator"]
        self.assertEqual(denominator["gt_cells"], 6)
        self.assertEqual(denominator["gt_labeled_cells"], 3)
        self.assertEqual(denominator["gt_not_applicable_cells"], 1)
        self.assertEqual(denominator["gt_uncertain_cells"], 1)
        self.assertEqual(denominator["gt_missing_cells"], 1)
        self.assertEqual(score["metrics"]["gap_accuracy"], 2 / 3)
        self.assertEqual(score["metrics"]["relation_accuracy"], 2 / 3)
        self.assertEqual(score["metrics"]["error_class_counts"]["direction_error"], 1)
        self.assertEqual(score["metrics"]["error_class_counts"]["magnitude_error"], 1)

    def test_judgment_reports_missing_and_invalid_human_direction_separately(self) -> None:
        labels = _labels()
        labels["S1"] = {"status": "labeled", "gap_magnitude": "medium", "relation": None}
        labels["S2"] = {"status": "labeled", "gap_magnitude": "none", "relation": "sideways"}
        score = score_judgment(_judgment_result(), labels, artifact_status="completed")
        self.assertEqual(score["denominator"]["gt_relation_missing_cells"], 1)
        self.assertEqual(score["denominator"]["gt_relation_invalid_cells"], 1)
        self.assertEqual(score["denominator"]["scored_relation_cells"], 1)

    def test_judgment_excludes_gt_relation_gap_conflicts(self) -> None:
        labels = _labels()
        labels["S1"] = {"status": "labeled", "gap_magnitude": "none", "relation": "benchmark_better"}
        score = score_judgment(_judgment_result(), labels, artifact_status="completed")
        self.assertEqual(score["denominator"]["gt_relation_gap_conflict_cells"], 1)
        self.assertEqual(score["denominator"]["gt_invalid_cells"], 1)
        self.assertEqual(score["rows"][0]["error_class"], "gt_relation_gap_conflict")

    def test_invalid_labeled_gap_is_not_scored(self) -> None:
        labels = _labels()
        labels["S1"] = {"status": "labeled", "gap_magnitude": "not-a-gap", "relation": "benchmark_better"}
        score = score_judgment(_judgment_result(), labels, artifact_status="completed")
        row = next(row for row in score["rows"] if row["stage"] == "S1")
        self.assertEqual(score["denominator"]["gt_invalid_cells"], 1)
        self.assertEqual(row["status"], "invalid")
        self.assertEqual(row["error_class"], "gt_invalid")
        self.assertEqual(score["denominator"]["model_available_cells"], 2)

    def test_legacy_severity_cannot_claim_none_as_small(self) -> None:
        legacy = {
            "stage_judgments": [
                {"stage": "S2 产品引出", "severity": "small", "confidence": "high"},
            ]
        }
        labels = {stage: {"status": "missing", "gap_magnitude": None, "relation": None} for stage in ("S1", "S2", "S3", "S4", "S5", "S6")}
        labels["S2"] = {"status": "labeled", "gap_magnitude": "none", "relation": "tie"}
        score = score_judgment(legacy, labels, artifact_status="completed")
        self.assertEqual(score["metrics"]["error_class_counts"]["contract_representation_gap"], 1)
        self.assertIsNone(score["metrics"]["gap_accuracy"])
        self.assertEqual(score["metrics"]["contract_aware_gap_accuracy"], 0.0)
        self.assertEqual(score["denominator"]["contract_representation_gap_cells"], 1)

    def test_legacy_representation_gap_is_excluded_from_exact_metric(self) -> None:
        legacy = {
            "stage_judgments": [
                {
                    "stage": "S2 产品引出",
                    "severity": "small",
                    "relation": "tie",
                    "confidence": "high",
                }
            ]
        }
        labels = {stage: {"status": "missing", "gap_magnitude": None, "relation": None} for stage in ("S1", "S2", "S3", "S4", "S5", "S6")}
        labels["S2"] = {"status": "labeled", "gap_magnitude": "none", "relation": "tie"}
        score = score_judgment(legacy, labels, artifact_status="completed")
        self.assertIsNone(score["metrics"]["exact_direction_and_gap_accuracy"])
        self.assertEqual(score["metrics"]["relation_accuracy"], 1.0)

    def test_aggregate_exposes_stage_large_gap_recall_and_operational_status(self) -> None:
        labels = {stage: {"status": "missing", "gap_magnitude": None, "relation": None} for stage in ("S1", "S2", "S3", "S4", "S5", "S6")}
        labels["S4"] = {"status": "labeled", "gap_magnitude": "large", "relation": "benchmark_better"}
        result = {
            "stage_judgments": [
                {
                    "stage": "S4 效果呈现",
                    "gap_magnitude": "medium",
                    "relation": "benchmark_better",
                    "confidence": "high",
                }
            ]
        }
        score = score_judgment(result, labels, artifact_status="completed")
        aggregate = aggregate_model(
            [
                {
                    "model": "m",
                    "judgment": {
                        "artifact": {"status": "completed"},
                        "score": score,
                    },
                    "extraction": {
                        "artifact": {"status": "not_requested"},
                        "score": score_extraction(None, {}, artifact_status="not_requested"),
                    },
                }
            ],
            "m",
        )
        s4 = aggregate["judgment"]["stage_metrics"]["S4"]
        self.assertEqual(s4["gt_large_cells"], 1)
        self.assertEqual(s4["gt_large_missed_cells"], 1)
        self.assertEqual(s4["gt_large_recall"], 0.0)
        self.assertEqual(aggregate["judgment"]["operational"]["completed_artifacts"], 1)
        self.assertEqual(aggregate["extraction"]["operational"]["requested_artifacts"], 0)

    def test_model_abstention_is_not_counted_as_semantic_error(self) -> None:
        result = _judgment_result()
        result["stage_judgments"][0]["gap_magnitude"] = "uncertain"
        result["stage_judgments"][0]["relation"] = "uncertain"
        score = score_judgment(result, _labels(), artifact_status="completed")
        self.assertEqual(score["metrics"]["error_class_counts"]["prediction_unavailable"], 1)
        self.assertNotIn("prediction_unavailable", {"direction_error", "magnitude_error"})

    def test_relation_accuracy_has_its_own_denominator(self) -> None:
        result = _judgment_result()
        result["stage_judgments"][0]["gap_magnitude"] = "uncertain"
        result["stage_judgments"][0]["relation"] = "benchmark_better"
        score = score_judgment(result, _labels(), artifact_status="completed")
        self.assertEqual(score["denominator"]["scored_relation_cells"], 3)
        self.assertEqual(score["metrics"]["relation_accuracy"], 1.0)
        self.assertEqual(score["metrics"]["gap_accuracy"], 1 / 2)
        self.assertEqual(score["metrics"]["exact_direction_and_gap_accuracy"], 1 / 2)

    def test_invalid_model_relation_is_unavailable_not_direction_error(self) -> None:
        result = _judgment_result()
        result["stage_judgments"][0]["relation"] = "invalid"
        score = score_judgment(result, _labels(), artifact_status="completed")
        self.assertEqual(score["denominator"]["scored_relation_cells"], 2)
        self.assertEqual(score["metrics"]["relation_accuracy"], 1.0)
        self.assertEqual(score["metrics"]["error_class_counts"]["prediction_unavailable"], 1)

    def test_aggregate_recall_excludes_failed_sample_events(self) -> None:
        completed_result = {
            "creator_evidence_units": [],
            "benchmark_evidence_units": [],
        }
        sample = {
            "key_events": [
                {"role": "creator", "stage": "S1", "time_range": [0.0, 1.0]},
            ]
        }
        completed = score_extraction(completed_result, sample, artifact_status="completed")
        failed = score_extraction(None, sample, artifact_status="contract_failed")
        aggregate = aggregate_model(
            [
                {"model": "m", "judgment": {"score": score_judgment(None, _labels(), artifact_status="missing")}, "extraction": {"score": completed}},
                {"model": "m", "judgment": {"score": score_judgment(None, _labels(), artifact_status="missing")}, "extraction": {"score": failed}},
            ],
            "m",
        )
        self.assertEqual(aggregate["extraction"]["denominator"]["required_key_events"], 2)
        self.assertEqual(aggregate["extraction"]["denominator"]["scored_key_events"], 1)
        self.assertEqual(aggregate["extraction"]["temporal_stage_recall_proxy"], 0.0)

    def test_extraction_scores_stage_time_proxy_and_s3_s4_quality(self) -> None:
        result = {
            "creator_evidence_units": [
                {
                    "id": "C1",
                    "time_range": "1s - 3s",
                    "functions": ["S3"],
                    "information": "使用动作",
                    "fact_quality": {**_quality(), "proof": "not_applicable"},
                },
                {
                    "id": "C2",
                    "time_range": "6s - 8s",
                    "functions": ["S4"],
                    "information": "效果对比",
                    "fact_quality": {**_quality(), "causal_link": "weak"},
                },
            ],
            "benchmark_evidence_units": [
                {
                    "id": "B1",
                    "time_range": "10s - 12s",
                    "functions": ["S3"],
                    "information": "标杆使用",
                    "fact_quality": {**_quality(), "proof": "not_applicable"},
                }
            ],
        }
        sample = {
            "key_events": [
                {"id": "creator_usage", "role": "creator", "stage": "S3", "time_range": [2.0, 2.5]},
                {"id": "benchmark_usage", "role": "benchmark", "stage": "S3", "time_range": [10.5, 11.0]},
                {"id": "creator_effect", "role": "creator", "stage": "S4", "time_range": [6.5, 7.0]},
                {"id": "benchmark_effect", "role": "benchmark", "stage": "S4", "time_range": [20.0, 21.0]},
            ]
        }
        score = score_extraction(result, sample, artifact_status="completed")
        self.assertEqual(score["denominator"]["required_key_events"], 4)
        self.assertEqual(score["denominator"]["matched_key_events"], 3)
        self.assertEqual(score["metrics"]["temporal_stage_recall_proxy"], 0.75)
        self.assertEqual(score["stage_metrics"]["S3"]["recall"], 1.0)
        self.assertEqual(score["stage_metrics"]["S4"]["recall"], 0.5)
        self.assertEqual(score["stage_metrics"]["S4"]["quality_counts"]["causal_link"]["weak"], 1)

    def test_extraction_without_human_key_events_is_not_scored_as_zero(self) -> None:
        result = {
            "creator_evidence_units": [
                {
                    "id": "C1",
                    "time_range": "1s - 2s",
                    "functions": ["S3"],
                    "information": "使用动作",
                }
            ],
            "benchmark_evidence_units": [],
        }
        score = score_extraction(result, {}, artifact_status="completed")
        self.assertIsNone(score["metrics"]["temporal_stage_recall_proxy"])
        self.assertIsNone(score["metrics"]["temporal_stage_precision_proxy"])

    def test_invalid_human_key_events_are_excluded_from_recall_denominator(self) -> None:
        score = score_extraction(
            {"creator_evidence_units": [], "benchmark_evidence_units": []},
            {
                "key_events": [
                    {"role": "creator", "stage": "S3", "time_range": [4.0, 1.0]},
                    {
                        "role": "creator",
                        "stage": "S5",
                        "time_range": [1.0, 2.0],
                        "expected_state": "absent",
                    },
                ]
            },
            artifact_status="completed",
        )
        self.assertEqual(score["denominator"]["required_key_events"], 2)
        self.assertEqual(score["denominator"]["invalid_key_events"], 2)
        self.assertEqual(score["denominator"]["present_key_events"], 0)
        self.assertEqual(score["denominator"]["absence_checks"], 0)

    def test_source_identity_mismatch_is_explicit(self) -> None:
        audit = _source_identity_audit(
            {
                "status": "completed",
                "source_digest": "source-a",
                "video_role_order": ["creator", "benchmark"],
                "video_source_sha256": ["a", "b"],
            },
            {
                "status": "completed",
                "base_source_digest": "source-b",
                "source_extraction": {
                    "video_role_order": ["creator", "benchmark"],
                    "video_source_sha256": ["a", "b"],
                },
            },
        )
        self.assertEqual(audit["status"], "blocked_source_identity_mismatch")
        self.assertEqual(audit["mismatches"][0]["field"], "source_digest")

    def test_model_independent_pairing_uses_raw_extraction_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            result_path = output_dir / "model_independent_evaluation.json"
            result_path.write_text(
                json.dumps(
                    {
                        "status": "completed",
                        "source_digest": "derived-fact-bundle",
                        "result": {},
                    }
                ),
                encoding="utf-8",
            )
            (output_dir / "compact_request_metadata.json").write_text(
                json.dumps({"protocol_hash": "protocol", "source_commit": "commit"}),
                encoding="utf-8",
            )
            (output_dir / "model_independent_input_metadata.json").write_text(
                json.dumps(
                    {
                        "base_source_digest": "visual-facts-bundle",
                        "source_extraction": {
                            "source_digest": "raw-video-bundle",
                            "video_role_order": ["creator", "benchmark"],
                            "video_source_sha256": ["a", "b"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            _, metadata = _read_result_artifact(result_path, "result")
            self.assertEqual(metadata["source_digest"], "derived-fact-bundle")
            self.assertEqual(metadata["base_source_digest"], "visual-facts-bundle")
            self.assertEqual(metadata["paired_source_digest"], "raw-video-bundle")
            audit = _source_identity_audit(
                {
                    "status": "completed",
                    "source_digest": "raw-video-bundle",
                    "video_role_order": ["creator", "benchmark"],
                    "video_source_sha256": ["a", "b"],
                },
                metadata,
            )
            self.assertEqual(audit["status"], "matched")

    def test_missing_base_source_digest_is_incomplete_not_a_false_mismatch(self) -> None:
        audit = _source_identity_audit(
            {
                "status": "completed",
                "source_digest": "source-a",
                "video_role_order": ["creator", "benchmark"],
                "video_source_sha256": ["a", "b"],
            },
            {
                "status": "completed",
                "source_digest": "derived-fact-bundle",
                "paired_source_digest": None,
                "video_role_order": ["creator", "benchmark"],
                "video_source_sha256": ["a", "b"],
            },
        )
        self.assertEqual(audit["status"], "source_identity_incomplete")
        self.assertEqual(audit["mismatches"], [])

    def test_source_identity_mismatch_is_excluded_from_aggregate_metrics(self) -> None:
        record = {
            "model": "m",
            "source_identity": {"status": "blocked_source_identity_mismatch"},
            "judgment": {"score": score_judgment(_judgment_result(), _labels(), artifact_status="completed")},
            "extraction": {
                "score": score_extraction(None, {}, artifact_status="not_requested"),
            },
        }
        aggregate = aggregate_model([record], "m")
        self.assertEqual(aggregate["sample_count"], 1)
        self.assertEqual(aggregate["scored_sample_count"], 0)
        self.assertEqual(aggregate["source_identity_mismatch_sample_count"], 1)
        self.assertIsNone(aggregate["judgment"]["gap_accuracy"])

    def test_incomplete_source_identity_is_excluded_from_aggregate_metrics(self) -> None:
        record = {
            "model": "m",
            "source_identity": {"status": "source_identity_incomplete"},
            "judgment": {"score": score_judgment(_judgment_result(), _labels(), artifact_status="completed")},
            "extraction": {
                "score": score_extraction(None, {}, artifact_status="not_requested"),
            },
        }
        aggregate = aggregate_model([record], "m")
        self.assertEqual(aggregate["sample_count"], 1)
        self.assertEqual(aggregate["scored_sample_count"], 0)
        self.assertEqual(aggregate["source_identity_incomplete_sample_count"], 1)
        self.assertIsNone(aggregate["judgment"]["gap_accuracy"])

    def test_sanitized_alignment_paths_cannot_collide(self) -> None:
        with self.assertRaisesRegex(ValueError, "share output component"):
            _safe_component_map(["sample/a", "sample_a"], label="sample_id")

    def test_extraction_proxy_rejects_unit_outside_artifact_video_duration(self) -> None:
        result = {
            "creator_evidence_units": [
                {
                    "id": "C1",
                    "time_range": "20s - 21s",
                    "functions": ["S3"],
                    "information": "越界事实",
                }
            ],
            "benchmark_evidence_units": [],
        }
        sample = {"key_events": [{"role": "creator", "stage": "S3", "time_range": [20.0, 20.5]}]}
        score = score_extraction(
            result,
            sample,
            artifact_status="completed",
            source_durations={"creator": 10.0},
        )
        self.assertEqual(score["denominator"]["matched_key_events"], 0)
        self.assertEqual(score["denominator"]["valid_model_units"], 0)

    def test_extraction_treats_absent_key_events_as_negative_checks(self) -> None:
        result = {
            "creator_evidence_units": [
                {
                    "id": "C1",
                    "time_range": "1s - 2s",
                    "functions": ["S5"],
                    "information": "认证机构背书",
                }
            ],
            "benchmark_evidence_units": [],
        }
        sample = {
            "key_events": [
                {"id": "present", "role": "creator", "stage": "S5", "time_range": [1.0, 2.0], "expected_state": "present"},
                {"id": "absent", "role": "creator", "stage": "S5", "time_range": [1.0, 2.0], "expected_state": "absent", "terms_any": ["认证"]},
            ]
        }
        score = score_extraction(result, sample, artifact_status="completed")
        self.assertEqual(score["denominator"]["required_key_events"], 2)
        self.assertEqual(score["denominator"]["present_key_events"], 1)
        self.assertEqual(score["denominator"]["scored_key_events"], 1)
        self.assertEqual(score["denominator"]["matched_key_events"], 1)
        self.assertEqual(score["denominator"]["absence_checks"], 1)
        self.assertEqual(score["denominator"]["absence_respected"], 0)
        self.assertEqual(score["metrics"]["absence_respected_rate"], 0.0)

    def test_absent_key_event_uses_terms_to_ignore_unrelated_overlap(self) -> None:
        result = {
            "creator_evidence_units": [
                {
                    "id": "C1",
                    "time_range": "1s - 2s",
                    "functions": ["S5"],
                    "information": "产品规格和容量",
                }
            ],
            "benchmark_evidence_units": [],
        }
        sample = {
            "key_events": [
                {
                    "id": "absent",
                    "role": "creator",
                    "stage": "S5",
                    "time_range": [1.0, 2.0],
                    "expected_state": "absent",
                    "terms_any": ["认证", "授权"],
                }
            ]
        }
        score = score_extraction(result, sample, artifact_status="completed")
        self.assertEqual(score["denominator"]["absence_respected"], 1)
        self.assertEqual(score["metrics"]["absence_respected_rate"], 1.0)

    def test_failed_extraction_is_excluded_from_absence_denominator(self) -> None:
        sample = {
            "key_events": [
                {
                    "id": "absent",
                    "role": "creator",
                    "stage": "S5",
                    "time_range": [1.0, 2.0],
                    "expected_state": "absent",
                    "terms_any": ["认证"],
                }
            ]
        }
        score = score_extraction(None, sample, artifact_status="failed")
        self.assertEqual(score["denominator"]["absence_checks"], 0)
        self.assertIsNone(score["metrics"]["absence_respected_rate"])

    def test_aggregate_preserves_failure_in_operational_denominator(self) -> None:
        labels = _labels()
        completed = score_judgment(_judgment_result(), labels, artifact_status="completed")
        failed = score_judgment(None, labels, artifact_status="contract_failed")
        records = [
            {"model": "m", "judgment": {"score": completed}, "extraction": {"score": score_extraction(None, {}, artifact_status="missing")}},
            {"model": "m", "judgment": {"score": failed}, "extraction": {"score": score_extraction(None, {}, artifact_status="contract_failed")}},
        ]
        aggregate = aggregate_model(records, "m")
        self.assertEqual(aggregate["sample_count"], 2)
        self.assertEqual(aggregate["judgment"]["denominator"]["model_failed_or_missing_cells"], 3)
        self.assertIsNone(aggregate["extraction"]["temporal_stage_recall_proxy"])
        self.assertEqual(aggregate["extraction"]["denominator"]["scored_key_events"], 0)


if __name__ == "__main__":
    unittest.main()
