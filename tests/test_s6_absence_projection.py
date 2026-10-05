"""Closed S6 absence must not depend on an optional provider flag."""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from flayr_core.llm.stage2_projection import (
    _normalize_segmented_stage,
    _segmented_explicit_absence_flag,
)
from flayr_core.postprocess.validate import validate_s6_cta_flags


class S6AbsenceProjectionTests(unittest.TestCase):
    def project(self, absent_role, status="absent"):
        present_role = "creator" if absent_role == "benchmark" else "benchmark"
        flag = {
            "exists": True, "module_type": "C", "start_seconds": 1,
            "end_seconds": 3, "cta_reason": "Buy now", "evidence_ids": ["E1"],
        }
        for field in ("direct_order_met", "action_path_clear", "soft_purchase_invitation_met",
                      "offer_or_incentive_clear", "price_anchor_met", "urgency_evidence_met",
                      "gift_stack_met", "guarantee_clear_met", "urgency_met",
                      "product_value_recalled", "module_fit_met", "ending_position_met",
                      "depends_on_valid_s4", "compliance_risk"):
            flag[field] = False
        flag["direct_order_met"] = True
        raw = {"stage_state": "completed", "relation": f"{present_role}_better",
               "model_gap_magnitude": "medium", "judgment_reason": "E1: Buy now",
               f"{present_role}_evidence_ids": ["E1"], f"{present_role}_s6": flag}
        facts = {absent_role: {"test_status": status}, present_role: {"test_status": "present"}}
        original = copy.deepcopy((raw, facts))
        with patch("flayr_core.llm.stage2_projection.stage_evidence_readiness",
                   side_effect=lambda side, stage: side["test_status"]), patch(
            "flayr_core.llm.stage2_projection.qualified_stage_evidence_ids",
            side_effect=lambda side, stage: {"E1"} if side["test_status"] == "present" else set(),
        ):
            projected = _normalize_segmented_stage(raw, "S6", facts)
        self.assertEqual((raw, facts), original)
        return projected

    def test_closed_absence_both_roles_pass_strict_validator(self):
        for role in ("benchmark", "creator"):
            with self.subTest(role=role):
                stage = self.project(role)
                self.assertFalse(stage[f"{role}_s6"]["exists"])
                self.assertEqual(stage[f"{role}_s6"]["evidence_ids"], [])
                self.assertEqual(stage["model_gap_magnitude"], "medium")
                validate_s6_cta_flags({"stage_analysis": [{}, {}, {}, {}, {}, stage]},
                                     {"s6_flags_required": True})

    def test_unknown_and_partial_are_never_negative_facts(self):
        for status in ("unknown", "conflict", "partial"):
            stage = self.project("benchmark", status)
            self.assertNotIn("benchmark_s6", stage)

    def test_helper_does_not_invent_positive_flags(self):
        flag = _segmented_explicit_absence_flag("S6", "benchmark")
        self.assertIsNotNone(flag)
        self.assertFalse(flag["exists"])
        self.assertFalse(flag["direct_order_met"])
        self.assertIn("absent", flag["cta_reason"])


if __name__ == "__main__":
    unittest.main()
