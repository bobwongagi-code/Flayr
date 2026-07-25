from __future__ import annotations

import unittest

from scripts.flayr_core.postprocess.validate import (
    classify_variant_data_issues,
    validate_global_gate_observations,
)


class GlobalGateQaTests(unittest.TestCase):
    def test_generated_evidence_units_are_not_variant_observation_conflicts(self) -> None:
        issues = classify_variant_data_issues([
            {"id": "C_CERT_S5", "variant_data_valid": False},
            {"id": "C_CTA_SRT", "variant_data_valid": False},
            {
                "id": "C1",
                "variant_data_valid": False,
                "variant_ids": ["black", "silver"],
                "variant_visual_shares": {"black": 1.0},
            },
        ])
        self.assertEqual(issues["non_variant_generated_units"], ["C_CERT_S5", "C_CTA_SRT"])
        self.assertEqual(issues["observation_conflicts"], ["C1"])

    def test_warning_taxonomy_keeps_generated_units_separate(self) -> None:
        result = {
            "video_understanding": {
                "creator": {
                    "gate_observation_status": {
                        "selling_point_route": "complete",
                        "variant_focus": "complete",
                        "attention_scan": "complete",
                    },
                    "evidence_units": [
                        {"id": "C_CERT_S5", "variant_data_valid": False},
                        {"id": "C1", "variant_data_valid": False, "variant_ids": ["black"]},
                    ],
                },
                "benchmark": {
                    "gate_observation_status": {
                        "selling_point_route": "complete",
                        "variant_focus": "complete",
                        "attention_scan": "complete",
                    },
                    "evidence_units": [],
                },
            },
            "global_diagnosis": {"findings": []},
        }
        validate_global_gate_observations(result)
        warnings = result["qa_warnings"]
        self.assertTrue(any("QA-NON-VARIANT-UNIT-SKIPPED" in item and "C_CERT_S5" in item for item in warnings))
        self.assertTrue(any("QA-VARIANT-OBSERVATION-CONFLICT" in item and "C1" in item for item in warnings))
