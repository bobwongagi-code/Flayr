import itertools
import random
import unittest

from scripts.flayr_core.llm.parse import normalize_s3_flags, normalize_video_understanding
from scripts.flayr_core.postprocess.chain import finalize_severity_after_repairs
from scripts.flayr_core.postprocess.derive import (
    SeverityConstraint,
    _derive_one,
    resolve_severity,
)
from scripts.flayr_core.postprocess.repair_evidence import reconcile_s5_trust_sources


class DeriveResolverTests(unittest.TestCase):
    def test_floor_and_ceiling_aggregation_is_order_independent(self) -> None:
        floors = (
            SeverityConstraint("floor", "medium", "S2_contract_floor", "contract"),
            SeverityConstraint("floor", "small", "S1_landing_floor", "landing"),
        )
        ceilings = (
            SeverityConstraint("ceiling", "large", "S5_no_trust_ceiling", "trust"),
            SeverityConstraint("ceiling", "medium", "S6_creator_cta_ceiling", "cta"),
        )
        expected = resolve_severity("large", floors, ceilings)
        for floor_order in itertools.permutations(floors):
            for ceiling_order in itertools.permutations(ceilings):
                self.assertEqual(resolve_severity("large", floor_order, ceiling_order), expected)

        self.assertEqual(expected["floor"], "medium")
        self.assertEqual(expected["ceiling"], "medium")
        self.assertEqual(expected["severity"], "medium")

    def test_generated_constraints_are_commutative(self) -> None:
        """随机生成多条约束，固定种子验证 max/min 不依赖触发顺序。"""
        rng = random.Random(20260724)
        levels = ("small", "medium", "large")
        for case_index in range(40):
            model = rng.choice(levels)
            floor_count = rng.randint(0, 5)
            ceiling_count = rng.randint(0, 5)
            floors = tuple(
                SeverityConstraint(
                    "floor",
                    rng.choice(levels),
                    f"floor_{case_index}_{index}",
                    f"floor reason {index}",
                )
                for index in range(floor_count)
            )
            ceilings = tuple(
                SeverityConstraint(
                    "ceiling",
                    rng.choice(levels),
                    f"ceiling_{case_index}_{index}",
                    f"ceiling reason {index}",
                )
                for index in range(ceiling_count)
            )
            expected = resolve_severity(model, floors, ceilings)
            for _ in range(20):
                shuffled_floors = list(floors)
                shuffled_ceilings = list(ceilings)
                rng.shuffle(shuffled_floors)
                rng.shuffle(shuffled_ceilings)
                self.assertEqual(
                    resolve_severity(model, tuple(shuffled_floors), tuple(shuffled_ceilings)),
                    expected,
                )

    def test_equal_floor_and_ceiling_is_a_valid_clamp(self) -> None:
        result = resolve_severity(
            "small",
            (SeverityConstraint("floor", "medium", "floor", "floor"),),
            (SeverityConstraint("ceiling", "medium", "ceiling", "ceiling"),),
        )
        self.assertEqual(result["severity"], "medium")
        self.assertEqual(result["status"], "constrained")
        self.assertFalse(result["phase_c_candidate"])

    def test_conflict_preserves_model_and_enters_phase_c(self) -> None:
        result = resolve_severity(
            "small",
            (SeverityConstraint("floor", "large", "floor", "floor"),),
            (SeverityConstraint("ceiling", "medium", "ceiling", "ceiling"),),
        )
        self.assertEqual(result["severity"], "small")
        self.assertEqual(result["status"], "conflict")
        self.assertTrue(result["phase_c_candidate"])

    def test_missing_and_uncertain_facts_are_logged_separately_and_do_not_trigger(self) -> None:
        marker = {"_postprocess_state": {"s1_hook_boundaries": "repaired"}}
        missing = _derive_one(
            "S1",
            {
                **marker,
                "severity": "small",
                "creator_hook": {"landing_met": False},
                "benchmark_hook": {"landing_met": True},
            },
            facts={},
        )
        self.assertEqual(missing["status"], "model_preserved")
        landing_missing = next(item for item in missing["constraint_evaluations"] if item["rule"] == "S1_landing_floor")
        self.assertEqual(landing_missing["status"], "missing_field")

        uncertain = _derive_one(
            "S1",
            {
                **marker,
                "severity": "small",
                "creator_hook": {"landing_met": None, "evidence_ids": ["C1"]},
                "benchmark_hook": {"landing_met": True, "evidence_ids": ["B1"]},
            },
            facts={},
        )
        landing_uncertain = next(item for item in uncertain["constraint_evaluations"] if item["rule"] == "S1_landing_floor")
        self.assertEqual(landing_uncertain["status"], "uncertain_fact")
        self.assertEqual(uncertain["status"], "model_preserved")

    def test_s1_medium_floor_requires_direct_or_explicit_evidence_strength(self) -> None:
        stage = {
            "_postprocess_state": {"s1_hook_boundaries": "repaired"},
            "severity": "small",
            "creator_hook": {"landing_met": False, "evidence_ids": ["C1"]},
            "benchmark_hook": {"landing_met": True, "evidence_ids": ["B1"]},
        }
        inferred_facts = {
            "video_understanding": {
                "creator": {"evidence_units": [{"id": "C1", "evidence_strength": "inferred"}]},
                "benchmark": {"evidence_units": [{"id": "B1", "evidence_strength": "direct"}]},
            }
        }
        inferred = _derive_one("S1", stage, facts=inferred_facts)
        self.assertEqual(inferred["severity"], "small")
        self.assertFalse(inferred["constraints"])
        self.assertEqual(
            next(item for item in inferred["constraint_evaluations"] if item["rule"] == "S1_landing_floor")["status"],
            "uncertain_evidence_strength",
        )

        explicit_facts = {
            "video_understanding": {
                "creator": {"evidence_units": [{"id": "C1", "evidence_strength": "explicit"}]},
                "benchmark": {"evidence_units": [{"id": "B1", "evidence_strength": "direct"}]},
            }
        }
        explicit = _derive_one("S1", stage, facts=explicit_facts)
        self.assertEqual(explicit["severity"], "medium")
        self.assertEqual(explicit["constraints"][0]["rule"], "S1_landing_floor")

    def test_s1_hook_exists_floor_requires_repaired_marker(self) -> None:
        flags = {
            "creator_hook": {"exists": False, "evidence_ids": ["C1"]},
            "benchmark_hook": {"exists": True, "evidence_ids": ["B1"]},
            "severity": "small",
        }
        before_repair = _derive_one("S1", flags)
        self.assertEqual(before_repair["severity"], "small")
        self.assertEqual(
            next(item for item in before_repair["constraint_evaluations"] if item["rule"] == "S1_hook_exists_floor")["status"],
            "precondition_missing",
        )

        after_repair = _derive_one(
            "S1",
            {**flags, "_postprocess_state": {"s1_hook_boundaries": "repaired"}},
        )
        self.assertEqual(after_repair["severity"], "large")
        self.assertEqual(after_repair["constraints"][0]["rule"], "S1_hook_exists_floor")

    def test_normalization_preserves_missing_s3_process_framing(self) -> None:
        normalized = normalize_s3_flags({"usage_process_visible": True})
        self.assertIsNotNone(normalized)
        self.assertIsNone(normalized["process_framing_met"])

    def test_s5_source_status_preserves_missing_uncertain_and_explicit_absence(self) -> None:
        normalized = normalize_video_understanding(
            {
                "creator": {
                    "evidence_units": [
                        {"id": "MISSING"},
                        {"id": "ABSENT", "trust_source_signals": [], "trust_source_reference": ""},
                        {"id": "PRESENT", "trust_source_signals": ["authority"], "trust_source_reference": "KKM"},
                        {"id": "UNCERTAIN", "trust_source_signals": ["authority"]},
                    ]
                }
            }
        )
        statuses = {
            unit["id"]: unit["trust_source_status"]
            for unit in normalized["creator"]["evidence_units"]
        }
        self.assertEqual(
            statuses,
            {
                "MISSING": "missing",
                "ABSENT": "explicit_absent",
                "PRESENT": "explicit_present",
                "UNCERTAIN": "uncertain",
            },
        )

    def test_s5_unknown_source_does_not_trigger_ceiling_or_rewrite_flag(self) -> None:
        stage = {
            "stage": "S5 信任放大",
            "severity": "large",
            "creator_s5": {
                "exists": True,
                "trust_evidence_type": "soft",
                "trust_basis": "authority",
                "trust_claim_specific": True,
                "product_relevance_met": True,
                "independent_trust_purpose": True,
                "duplicates_other_stage": False,
                "evidence_ids": ["C5"],
            },
            "benchmark_s5": {
                "exists": True,
                "trust_evidence_type": "soft",
                "trust_basis": "authority",
                "trust_claim_specific": True,
                "product_relevance_met": True,
                "independent_trust_purpose": True,
                "duplicates_other_stage": False,
                "evidence_ids": ["B5"],
            },
        }
        result = {
            "stage_analysis": [{}, {}, {}, {}, stage],
            "video_understanding": {
                "creator": {"evidence_units": [{"id": "C5", "endorsement_verbal": False, "endorsement_visual": False}]},
                "benchmark": {"evidence_units": [{"id": "B5", "endorsement_verbal": False, "endorsement_visual": False}]},
            },
        }
        reconcile_s5_trust_sources(result, True)
        finalize_severity_after_repairs(result, {})
        self.assertEqual(stage["creator_s5"]["_s5_source_status"], "unknown")
        self.assertEqual(stage["benchmark_s5"]["_s5_source_status"], "unknown")
        self.assertEqual(stage["severity"], "large")
        self.assertEqual(stage["severity_derivation"]["status"], "model_preserved")

    def test_s5_explicit_absence_can_trigger_ceiling(self) -> None:
        stage = {
            "stage": "S5 信任放大",
            "severity": "large",
            "creator_s5": {
                "exists": False,
                "trust_evidence_type": "none",
                "trust_basis": "none",
                "independent_trust_purpose": False,
                "duplicates_other_stage": False,
            },
            "benchmark_s5": {
                "exists": False,
                "trust_evidence_type": "none",
                "trust_basis": "none",
                "independent_trust_purpose": False,
                "duplicates_other_stage": False,
            },
        }
        result = {
            "stage_analysis": [{}, {}, {}, {}, stage],
            "video_understanding": {
                "creator": {"evidence_units": [{"id": "C5", "endorsement_verbal": False, "endorsement_visual": False}]},
                "benchmark": {"evidence_units": [{"id": "B5", "endorsement_verbal": False, "endorsement_visual": False}]},
            },
        }
        reconcile_s5_trust_sources(result, True)
        finalize_severity_after_repairs(result, {})
        self.assertEqual(stage["severity"], "medium")
        self.assertEqual(stage["severity_derivation"]["constraints"][0]["kind"], "ceiling")

    def test_s5_source_basis_mismatch_is_uncertain_not_absent(self) -> None:
        stage = {
            "stage": "S5 信任放大",
            "severity": "large",
            "creator_s5": {
                "exists": False,
                "trust_evidence_type": "none",
                "trust_basis": "none",
                "independent_trust_purpose": False,
                "duplicates_other_stage": False,
                "evidence_ids": ["C5"],
            },
            "benchmark_s5": {
                "exists": False,
                "trust_evidence_type": "none",
                "trust_basis": "none",
                "independent_trust_purpose": False,
                "duplicates_other_stage": False,
                "evidence_ids": ["B5"],
            },
        }
        result = {
            "stage_analysis": [{}, {}, {}, {}, stage],
            "video_understanding": {
                "creator": {
                    "evidence_units": [{
                        "id": "C5",
                        "trust_source_signals": ["authority"],
                        "trust_source_reference": "来源存在",
                        "endorsement_verbal": False,
                        "endorsement_visual": False,
                    }]
                },
                "benchmark": {
                    "evidence_units": [{
                        "id": "B5",
                        "trust_source_signals": ["authority"],
                        "trust_source_reference": "来源存在",
                        "endorsement_verbal": False,
                        "endorsement_visual": False,
                    }]
                },
            },
        }
        reconcile_s5_trust_sources(result, True)
        finalize_severity_after_repairs(result, {})
        self.assertEqual(stage["creator_s5"]["_s5_source_status"], "unknown")
        self.assertEqual(stage["benchmark_s5"]["_s5_source_status"], "unknown")
        self.assertEqual(stage["severity"], "large")
        self.assertEqual(
            next(item for item in stage["severity_derivation"]["constraint_evaluations"] if item["rule"] == "S5_no_trust_ceiling")["status"],
            "uncertain_fact",
        )


if __name__ == "__main__":
    unittest.main()
