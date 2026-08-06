from __future__ import annotations

import unittest

from scripts.flayr_core.evaluation_chain import audit_analysis_chain, audit_s4_flag, classify_s5_trust_state


def _s4_flag(*, state: str, evidence_ids: list[str]) -> dict:
    return {
        "effect_evidence_state": state,
        "effect_visible": state != "none",
        "effect_attribution_supported": state == "verified",
        "process_linked_effect": state == "verified",
        "result_only_without_process": state == "result_only",
        "effect_type": "none" if state == "none" else "before_after",
        "evidence_ids": evidence_ids,
    }


class EvaluationChainTests(unittest.TestCase):
    def test_active_stage_contract_rejects_functions_only_ownership(self) -> None:
        checks = [
            {
                "stage": f"S{index}",
                "status": "unknown",
                "coverage": "unknown",
                "evidence_ids": [],
                "observed_signals": [],
                "missing_signals": [],
            }
            for index in range(1, 7)
        ]
        result = {
            "video_understanding": {
                "creator": {
                    "stage_evidence_contract_version": 1,
                    "stage_evidence_checks": checks,
                    "evidence_units": [{"id": "C1", "functions": ["S1_hook"]}],
                },
                "benchmark": {
                    "stage_evidence_contract_version": 1,
                    "stage_evidence_checks": checks,
                    "evidence_units": [{"id": "B1", "functions": ["S1_hook"]}],
                },
            },
            "stage_analysis": [
                {
                    "stage": "S1",
                    "creator_evidence_ids": ["C1"],
                    "benchmark_evidence_ids": ["B1"],
                }
            ],
        }
        audit = audit_analysis_chain(result)
        self.assertEqual(
            audit["stage_evidence"]["roles"]["S1"]["creator"]["status"],
            "invalid",
        )
        self.assertIn(
            "stage_evidence_unresolved:S1:C1",
            audit["stage_evidence"]["roles"]["S1"]["creator"]["errors"],
        )

    def test_s4_audit_is_mechanical_and_detects_impossible_state(self) -> None:
        flag = _s4_flag(state="verified", evidence_ids=["C1"])
        flag["process_linked_effect"] = False
        audit = audit_s4_flag(flag)
        self.assertEqual(audit["status"], "state_conflict")
        self.assertEqual(audit["reason_code"], "state_hard_fact_conflict")
        self.assertIn("verified_without_process_linked_effect", audit["errors"])

    def test_s4_none_cannot_carry_effect_evidence_ids(self) -> None:
        audit = audit_s4_flag(_s4_flag(state="none", evidence_ids=["C4"]))
        self.assertEqual(audit["status"], "state_conflict")
        self.assertIn("none_with_evidence_ids", audit["errors"])

    def test_s5_missing_source_is_uncertain_not_explicit_absence(self) -> None:
        self.assertEqual(
            classify_s5_trust_state({"trust_basis": "none"})["state"],
            "uncertain",
        )
        self.assertEqual(
            classify_s5_trust_state({"trust_basis": "none"})["reason_code"],
            "s5_absence_not_explicit",
        )
        self.assertEqual(
            classify_s5_trust_state(
                {
                    "exists": False,
                    "trust_evidence_type": "none",
                    "trust_basis": "none",
                    "trust_source_visible": False,
                    "trust_source_credible": False,
                }
            )["state"],
            "explicit_absence",
        )
        self.assertEqual(
            classify_s5_trust_state(
                {
                    "exists": False,
                    "trust_evidence_type": "none",
                    "trust_basis": "none",
                    "trust_source_visible": False,
                    "trust_source_credible": False,
                }
            )["reason_code"],
            "s5_explicit_absence",
        )
        self.assertEqual(
            classify_s5_trust_state({"trust_basis": "product_claim"})["state"],
            "product_claim_or_offer",
        )
        self.assertEqual(
            classify_s5_trust_state(
                {
                    "exists": False,
                    "trust_evidence_type": "credible",
                    "trust_basis": "none",
                    "trust_source_visible": False,
                    "trust_source_credible": False,
                    "trust_source_evidence_ids": ["C5"],
                }
            )["state"],
            "uncertain",
        )

    def test_full_audit_separates_s4_temporal_error_and_strength_gate(self) -> None:
        result = {
            "video_understanding": {
                "creator": {
                    "evidence_units": [
                        {"id": "C1", "functions": ["S4"]},
                        {"id": "C5", "functions": ["S5"]},
                    ]
                },
                "benchmark": {"evidence_units": [{"id": "B1", "functions": ["S4"]}]},
            },
            "stage_analysis": [
                {
                    "stage": "S4 效果呈现",
                    "creator_s4": _s4_flag(state="verified", evidence_ids=["C5"]),
                    "benchmark_s4": _s4_flag(state="verified", evidence_ids=["B1"]),
                    "severity": "medium",
                },
                {
                    "stage": "S5 信任放大",
                    "creator_s5": {"trust_basis": "none"},
                    "benchmark_s5": {"trust_basis": "product_claim"},
                    "severity": "small",
                },
            ],
        }
        audit = audit_analysis_chain(result)
        self.assertEqual(audit["s4"]["roles"]["creator"]["status"], "state_conflict")
        self.assertIn("evidence_temporal_mismatch:C5", audit["s4"]["roles"]["creator"]["evidence_errors"])
        self.assertEqual(audit["s5"]["roles"]["creator"]["state"], "uncertain")
        self.assertEqual(audit["s5"]["roles"]["benchmark"]["state"], "product_claim_or_offer")
        self.assertEqual(
            audit["s5"]["roles"]["benchmark"]["reason_code"],
            "s5_product_claim_or_offer",
        )
        self.assertEqual(audit["evidence_strength"]["status"], "gate_closed")


if __name__ == "__main__":
    unittest.main()
