from __future__ import annotations

import unittest

from scripts.flayr_core.postprocess.commercial_priority import (
    classify_painpoint_relevance,
    validate_commercial_priorities,
)
from scripts.flayr_core.postprocess.global_diagnosis import materialize_global_diagnosis


class CommercialPriorityTests(unittest.TestCase):
    def test_missing_and_invalid_relevance_remain_unknown(self) -> None:
        missing = classify_painpoint_relevance(None)
        invalid = classify_painpoint_relevance("not-a-contract-value")
        self.assertEqual(missing["status"], "unknown")
        self.assertEqual(missing["reason_code"], "missing")
        self.assertEqual(invalid["status"], "unknown")
        self.assertEqual(invalid["reason_code"], "invalid_value")
        self.assertIsNone(invalid["value"])
        self.assertIsNone(invalid["priority_rank"])

    def test_generated_priorities_have_a_closed_contract(self) -> None:
        result = {
            "video_understanding": {
                "creator": {"temporal_evidence_mode": "unknown", "evidence_units": []},
                "benchmark": {"temporal_evidence_mode": "unknown", "evidence_units": []},
            },
            "stage_analysis": [
                {
                    "stage": "S3 Usage",
                    "severity": "medium",
                    "creator_absolute_status": "weak",
                    "painpoint_relevance": None,
                    "gap": "使用过程较薄。",
                }
            ],
            "improvements": [{"target_stage": "S3", "priority": 1}],
        }
        materialize_global_diagnosis(result, None)
        self.assertEqual(validate_commercial_priorities(result["commercial_priorities"]), [])
        stage_item = next(item for item in result["commercial_priorities"] if item["source"] == "stage")
        self.assertEqual(stage_item["commercial_relevance"]["status"], "unknown")
        self.assertEqual(stage_item["commercial_relevance"]["reason_code"], "missing")

    def test_unknown_relevance_cannot_be_encoded_as_none(self) -> None:
        item = {
            "schema_version": 1,
            "id": "stage:S3",
            "source": "stage",
            "tier": "P3",
            "title": "S3",
            "summary": "gap",
            "reference_id": "S3",
            "root_cause_ids": [],
            "commercial_relevance": {
                "status": "unknown",
                "value": "none",
                "priority_rank": None,
                "reason_code": "missing",
            },
        }
        errors = validate_commercial_priorities([item])
        self.assertTrue(any("must not contain a value" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
