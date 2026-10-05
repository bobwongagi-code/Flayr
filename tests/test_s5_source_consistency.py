from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from flayr_core.stage_evidence_contracts import (
    _stage_check_issues,
    stage_evidence_contract,
)


class S5SourceConsistencyTests(unittest.TestCase):
    def negative_check(self, status="absent"):
        contract = stage_evidence_contract("S5")
        return {"status": status, "coverage": "complete", "evidence_ids": [],
                "observed_signals": [], "missing_signals": list(contract.required_signals),
                "signal_bindings": {}, "reason": "No independent source established."}

    def issues(self, unit, status="absent"):
        return _stage_check_issues(stage_evidence_contract("S5"), self.negative_check(status), {"C1": unit})

    def test_absence_cannot_ignore_explicit_typed_source(self):
        unit = {"trust_source_signals": ["authority"], "trust_source_reference": "Visible certification mark",
                "trust_source_status": "explicit_present"}
        self.assertIn("S5:negative_with_observed_trust_source:C1", self.issues(unit))

    def test_unanswered_visual_source_blocks_negative_closure(self):
        unit = {"endorsement_visual": True, "trust_source_status": "missing"}
        self.assertIn("S5:negative_with_unresolved_visual_source:C1", self.issues(unit))

    def test_visual_source_and_explicit_absence_are_contradictory(self):
        unit = {"endorsement_visual": True, "trust_source_signals": [],
                "trust_source_reference": "", "trust_source_status": "explicit_absent"}
        self.assertIn("S5:negative_with_unresolved_visual_source:C1", self.issues(unit))

    def test_source_type_without_reference_cannot_close_absent(self):
        unit = {"trust_source_signals": ["authority"], "trust_source_status": "uncertain"}
        self.assertIn("S5:negative_with_incomplete_source:C1", self.issues(unit))

    def test_unanswered_verbal_source_is_not_explicit_absence(self):
        self.assertIn("S5:negative_with_incomplete_source:C1", self.issues(
            {"endorsement_verbal": True, "trust_source_status": "missing"}))

    def test_negative_certification_mention_can_explicitly_have_no_source(self):
        unit = {"endorsement_verbal": True, "trust_source_signals": [], "trust_source_reference": "",
                "trust_source_status": "explicit_absent", "voiceover": "No certification is claimed."}
        self.assertEqual(self.issues(unit), [])

    def test_unflagged_missing_fields_do_not_block_every_legacy_fact(self):
        self.assertEqual(self.issues({"visual_fact": "Ordinary brand logo", "trust_source_status": "missing"}), [])

    def test_explicitly_wrong_subject_does_not_force_source_review(self):
        unit = {"endorsement_visual": True, "trust_source_signals": ["authority"],
                "trust_source_reference": "Certification on an unrelated object",
                "trust_source_status": "explicit_present", "fact_quality": {"subject": "incorrect"}}
        self.assertEqual(self.issues(unit), [])

    def test_unknown_remains_valid_and_records_are_not_rewritten(self):
        unit = {"endorsement_visual": True, "trust_source_status": "missing"}
        check = {"status": "unknown", "coverage": "unknown", "evidence_ids": [],
                 "observed_signals": [], "missing_signals": [], "signal_bindings": {}}
        snapshot = copy.deepcopy((unit, check))
        self.assertEqual(_stage_check_issues(stage_evidence_contract("S5"), check, {"C1": unit}), [])
        self.assertEqual((unit, check), snapshot)

    def test_not_applicable_cannot_hide_a_visible_source(self):
        self.assertIn("S5:negative_with_unresolved_visual_source:C1", self.issues(
            {"endorsement_visual": True, "trust_source_status": "missing"}, "not_applicable"))

    def test_other_stages_are_unaffected(self):
        contract = stage_evidence_contract("S4")
        check = self.negative_check()
        check["missing_signals"] = list(contract.required_signals)
        self.assertEqual(_stage_check_issues(contract, check, {"C1": {"endorsement_visual": True}}), [])

    def test_existing_recovery_plan_targets_the_source_conflict(self):
        from flayr_core.llm.pipeline import _stage1_recovery_plan

        side = {"stage_evidence_contract_version": 9,
                "evidence_units": [{"id": "C1", "endorsement_visual": True, "trust_source_status": "missing"}],
                "stage_evidence_checks": [{"stage": "S5", **self.negative_check()}]}
        plan = _stage1_recovery_plan(side)
        self.assertIn("S5", plan["targets"])
        self.assertIn("S5:negative_with_unresolved_visual_source:C1", plan["contract_issues"])
        self.assertIn("evidence_qualification_conflict", plan["trigger_reasons"])

    def test_existing_blocker_preserves_facts_and_other_stage_status(self):
        from flayr_core.llm.pipeline import _block_stage_qualifications

        side = {"evidence_units": [{"id": "C1", "endorsement_visual": True, "trust_source_status": "missing"}],
                "stage_evidence_checks": [{"stage": "S5", **self.negative_check()},
                                          {"stage": "S4", **self.negative_check()}]}
        original_units = copy.deepcopy(side["evidence_units"])
        result = _block_stage_qualifications(side, ["S5"], reason="Source record unresolved.")
        checks = {check["stage"]: check for check in result["stage_evidence_checks"]}
        self.assertEqual(checks["S5"]["status"], "unknown")
        self.assertEqual(checks["S5"]["evidence_ids"], [])
        self.assertEqual(checks["S4"]["status"], "absent")
        self.assertEqual(result["evidence_units"], original_units)


if __name__ == "__main__":
    unittest.main()
