import hashlib
import itertools
import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.flayr_core.llm.parse import normalize_s3_flags, normalize_s4_flags, normalize_video_understanding
from scripts.flayr_core.postprocess.chain import finalize_severity_after_repairs
from scripts.flayr_core.postprocess.calibration import (
    summarize_floor_coverage,
    summarize_repeat_stability,
    load_s4_large_floor_activation_evidence,
    validate_derive_calibration_card,
    validate_derive_calibration_cards,
    validate_s4_large_floor_activation_evidence,
)
from scripts.flayr_core.postprocess.derive import (
    SeverityConstraint,
    _derive_one,
    resolve_severity,
)
from scripts.flayr_core.postprocess.repair_evidence import (
    reconcile_s5_trust_sources,
    validate_s2_hard_fact_consistency,
    validate_s3_s4_hard_fact_consistency,
)
from scripts.flayr_core.postprocess.validate import validate_s3_usage_flags, validate_s4_effect_flags
from scripts.flayr_core.validation_cohort import SOURCE_CONTRACT_FILES, _git_value, _worktree_identity, sha256_file


def _s3_flag(state: str, evidence_id: str, **overrides: object) -> dict[str, object]:
    fields = {
        "exists": state in {"partial", "complete"},
        "usage_process_visible": False,
        "real_usage_met": False,
        "core_selling_point_visible": False,
        "action_proof_met": False,
        "action_target_contact_met": False,
        "action_application_change_visible": False,
        "critical_action_continuity_met": False,
        "result_only_without_process": False,
        "mouth_only_or_static": False,
        "fake_or_staged": False,
    }
    if state == "partial":
        fields.update({"usage_process_visible": True, "real_usage_met": True})
    elif state == "complete":
        fields.update(
            {
                key: True
                for key in fields
                if key not in {"fake_or_staged", "result_only_without_process", "mouth_only_or_static"}
            }
        )
    fields.update(overrides)
    return {"usage_evidence_state": state, "evidence_ids": [evidence_id], **fields}


def _s4_flag(state: str, evidence_id: str, **overrides: object) -> dict[str, object]:
    fields = {
        "effect_visible": False,
        "effect_proposition_matched": False,
        "visual_difference_observed": False,
        "module_constraints_met": False,
        "effect_attribution_supported": False,
        "process_linked_effect": False,
        "requires_close_inspection": False,
        "tamper_or_cut_risk": False,
        "result_only_without_process": False,
        "effect_salience": "none",
        "effect_maximized": False,
    }
    if state == "result_only":
        fields.update(
            {
                "effect_visible": True,
                "effect_proposition_matched": True,
                "visual_difference_observed": True,
                "module_constraints_met": True,
                "effect_attribution_supported": True,
                "result_only_without_process": True,
                "effect_salience": "clear",
            }
        )
    elif state == "verified":
        fields.update(
            {
                "effect_visible": True,
                "effect_proposition_matched": True,
                "visual_difference_observed": True,
                "module_constraints_met": True,
                "effect_attribution_supported": True,
                "process_linked_effect": True,
                "effect_salience": "strong",
                "effect_maximized": True,
            }
        )
    fields.update(overrides)
    return {"effect_evidence_state": state, "evidence_ids": [evidence_id], **fields}


def _evidence_facts(creator_id: str, benchmark_id: str, strength: str = "direct") -> dict[str, object]:
    return {
        "video_understanding": {
            "creator": {"evidence_units": [{"id": creator_id, "evidence_strength": strength}]},
            "benchmark": {"evidence_units": [{"id": benchmark_id, "evidence_strength": strength}]},
        }
    }


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _file_identity(path: Path) -> dict[str, object]:
    return {
        "path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _synthetic_cohort_lock() -> dict[str, object]:
    """Build a structurally complete lock over stable repository files."""
    root = Path(__file__).resolve().parents[1]
    worktree = _worktree_identity(root)
    sample_ids = [f"blind-{index}" for index in range(12)]
    source_contract_files = {
        relative: _file_identity(root / relative)
        for relative in SOURCE_CONTRACT_FILES
    }
    locked_samples = []
    for index, sample_id in enumerate(sample_ids):
        creator_path = root / ("ARCHITECTURE.md" if index % 2 == 0 else "QA-RULES.md")
        benchmark_path = root / ("QA-RULES.md" if index % 2 == 0 else "ARCHITECTURE.md")
        locked_samples.append(
            {
                "id": sample_id,
                "product_category": f"category-{index % 4}",
                "target_market": f"market-{index % 2}",
                "gt_sha256": "0" * 64,
                "videos": {
                    "creator": _file_identity(creator_path),
                    "benchmark": _file_identity(benchmark_path),
                },
            }
        )
    return {
        "schema_version": 1,
        "status": "frozen",
        "created_at": "2026-07-24T00:00:00+00:00",
        "spent_at": None,
        "spent_reason": None,
        "code": {
            "repo_root": str(root.resolve()),
            "commit": _git_value(root, "rev-parse", "HEAD"),
            "worktree_clean": worktree["clean"],
            "worktree_status_sha256": worktree["status_sha256"],
            "worktree_diff_sha256": worktree["diff_sha256"],
            "untracked_files": worktree["untracked_files"],
            "worktree_fingerprint_sha256": worktree["fingerprint_sha256"],
        },
        "model_config": {"model": "test-model", "api_url": "https://example.invalid", "temperature": 0},
        "labels": _file_identity(root / "references/analysis-output-schema.json"),
        "manifest": _file_identity(root / "references/analysis-output-schema.json"),
        "source_contract_files": source_contract_files,
        "sample_ids": sample_ids,
        "samples": locked_samples,
    }


def _s4_activation_evidence() -> dict[str, object]:
    """Synthetic gate evidence used only to test the activation contract."""
    cards: list[dict[str, object]] = []
    states_by_stage = {
        "S3": ("none", "partial", "complete", "uncertain"),
        "S4": ("none", "result_only", "verified", "uncertain"),
    }
    for index in range(24):
        stage = "S3" if index < 12 else "S4"
        states = states_by_stage[stage]
        creator_state = states[index % len(states)]
        benchmark_state = states[(index + 1) % len(states)]
        annotation_a = {
            "annotator_id": "annotator_a",
            "creator_state": creator_state,
            "benchmark_state": benchmark_state,
            "creator_hard_fact_status": "consistent",
            "benchmark_hard_fact_status": "consistent",
            "creator_strength": "direct",
            "benchmark_strength": "direct",
        }
        annotation_b = {**annotation_a, "annotator_id": "annotator_b"}
        if stage == "S3" and creator_state == "none" and benchmark_state == "complete":
            expected_floor_outcome = "trigger_large"
        elif stage == "S4" and creator_state == "none" and benchmark_state == "verified":
            expected_floor_outcome = "audit_only_candidate"
        elif "uncertain" in {creator_state, benchmark_state}:
            expected_floor_outcome = "uncertain_no_trigger"
        else:
            expected_floor_outcome = "no_trigger_medium_kept"
        cards.append(
            {
                "sample_id": f"calibration-{index}",
                "partition": "calibration",
                "stage": stage,
                "annotation_a": annotation_a,
                "annotation_b": annotation_b,
                "expected_creator_state": creator_state,
                "expected_benchmark_state": benchmark_state,
                "expected_creator_hard_fact_status": "consistent",
                "expected_benchmark_hard_fact_status": "consistent",
                "expected_creator_strength": "direct",
                "expected_benchmark_strength": "direct",
                "expected_floor_outcome": expected_floor_outcome,
            }
        )
    repeat_observations = [
        {"action_target_contact_met": True, "critical_action_continuity_met": False}
        for _ in range(5)
    ]
    blind_samples = [
        {
            "sample_id": f"blind-{index}",
            "category": f"category-{index % 4}",
            "market": f"market-{index % 2}",
            "phase_c_regression": False,
        }
        for index in range(12)
    ]
    coverage_records = [
        {"sample_id": "coverage-1", "ground_truth_structural_gap": True, "floor_applied": True},
        {"sample_id": "coverage-2", "ground_truth_structural_gap": True, "floor_applied": False},
    ]
    calibration_cards_sha256 = hashlib.sha256(
        json.dumps(cards, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    cohort_lock = _synthetic_cohort_lock()
    lock_sha256 = _canonical_sha256(cohort_lock)
    source_contract_sha256 = _canonical_sha256(cohort_lock["source_contract_files"])
    return {
        "kind": "s4_large_floor_activation_v1",
        "schema_version": 1,
        "provenance": {
            "producer": "test-fixture",
            "created_at": "2026-07-24T00:00:00Z",
            "source_contract_sha256": source_contract_sha256,
            "calibration_cards_sha256": calibration_cards_sha256,
            "blind_cohort_lock_sha256": lock_sha256,
            "annotator_ids": ["annotator_a", "annotator_b"],
        },
        "calibration": {
            "cards": cards,
            "s3_boundary_status": "passed",
            "s4_boundary_status": "passed",
            "excluded_from_blind": True,
        },
        "repeat_stability": {
            "observations": repeat_observations,
            "fields": ["action_target_contact_met", "critical_action_continuity_met"],
            "status": "measured",
            "stable": True,
        },
        "blind_cohort": {
            "status": "passed",
            "locked": True,
            "fresh": True,
            "lock_sha256": lock_sha256,
            "cohort_lock": cohort_lock,
            "samples": blind_samples,
        },
        "floor_coverage": {
            "records": coverage_records,
            "status": "measured",
            "derive_regressions": 0,
        },
    }


def _load_trusted_s4_activation_evidence(payload: dict[str, object]) -> object:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
        handle.write(raw)
        path = Path(handle.name)
    try:
        return load_s4_large_floor_activation_evidence(path, hashlib.sha256(raw).hexdigest())
    finally:
        path.unlink(missing_ok=True)


def _validated_stage_pair(creator: dict[str, object], benchmark: dict[str, object], stage_index: int) -> dict[str, object]:
    stages: list[dict[str, object]] = [{} for _ in range(6)]
    stages[stage_index] = {
        f"creator_s{stage_index + 1}": creator,
        f"benchmark_s{stage_index + 1}": benchmark,
    }
    result: dict[str, object] = {"stage_analysis": stages}
    validate_s3_s4_hard_fact_consistency(result)
    return result


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

    def test_s2_contract_floor_requires_read_only_hard_fact_marker(self) -> None:
        creator = {
            "exists": True,
            "merged_with_s3": False,
            "handoff_met": False,
            "product_identity_clear": False,
            "product_role_clear": False,
            "evidence_ids": ["C2"],
        }
        benchmark = {
            "exists": True,
            "merged_with_s3": False,
            "handoff_met": True,
            "product_identity_clear": True,
            "product_role_clear": True,
            "evidence_ids": ["B2"],
        }
        stage = {"severity": "small", "creator_s2": creator, "benchmark_s2": benchmark}
        facts = _evidence_facts("C2", "B2")
        without_marker = _derive_one("S2", stage, facts=facts)
        evaluation = next(item for item in without_marker["constraint_evaluations"] if item["rule"] == "S2_contract_floor")
        self.assertEqual(without_marker["severity"], "small")
        self.assertEqual(evaluation["status"], "precondition_missing")
        self.assertEqual(evaluation["reason_code"], "repair_incomplete")

        result = {"stage_analysis": [{}, {"creator_s2": creator, "benchmark_s2": benchmark}]}
        validate_s2_hard_fact_consistency(result)
        with_marker = _derive_one("S2", result["stage_analysis"][1], facts=facts)
        self.assertEqual(with_marker["severity"], "medium")
        self.assertEqual(
            next(item for item in with_marker["constraint_evaluations"] if item["rule"] == "S2_contract_floor")["status"],
            "triggered",
        )

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
        self.assertEqual(after_repair["severity"], "small")
        hook_evaluation = next(
            item for item in after_repair["constraint_evaluations"] if item["rule"] == "S1_hook_exists_floor"
        )
        self.assertEqual(hook_evaluation["status"], "missing_field")
        self.assertEqual(hook_evaluation["reason_code"], "missing_field")

        explicit_facts = {
            "video_understanding": {
                "creator": {"evidence_units": [{"id": "C1", "evidence_strength": "direct"}]},
                "benchmark": {"evidence_units": [{"id": "B1", "evidence_strength": "explicit"}]},
            }
        }
        enabled = _derive_one(
            "S1",
            {**flags, "_postprocess_state": {"s1_hook_boundaries": "repaired"}},
            facts=explicit_facts,
        )
        self.assertEqual(enabled["severity"], "large")
        self.assertEqual(enabled["constraints"][0]["rule"], "S1_hook_exists_floor")

    def test_evidence_state_normalization_preserves_four_state_values(self) -> None:
        s3 = normalize_s3_flags({"usage_evidence_state": "partial"})
        self.assertEqual(s3["usage_evidence_state"], "partial")
        s4 = normalize_s4_flags({"effect_evidence_state": "result_only"})
        self.assertEqual(s4["effect_evidence_state"], "result_only")
        invalid = normalize_s4_flags({"effect_evidence_state": "not-a-state"})
        self.assertIsNone(invalid["effect_evidence_state"])

    def test_structured_comparison_requires_s3_s4_evidence_states(self) -> None:
        with self.assertRaisesRegex(SystemExit, "usage_evidence_state"):
            validate_s3_usage_flags(
                {"stage_analysis": [{}, {}, {"creator_s3": {}, "benchmark_s3": {}}]},
                {"evidence_state_required": True},
            )
        with self.assertRaisesRegex(SystemExit, "effect_evidence_state"):
            validate_s4_effect_flags(
                {"stage_analysis": [{}, {}, {}, {"creator_s4": {}, "benchmark_s4": {}}]},
                {"evidence_state_required": True},
            )

    def test_s3_partial_state_preserves_model_and_does_not_become_large(self) -> None:
        creator = _s3_flag("partial", "C3")
        benchmark = _s3_flag("complete", "B3")
        result = _validated_stage_pair(creator, benchmark, 2)
        stage = result["stage_analysis"][2]
        stage["severity"] = "medium"
        trace = _derive_one("S3", stage, facts=_evidence_facts("C3", "B3"))
        self.assertEqual(trace["severity"], "medium")
        self.assertFalse(any(item["rule"] == "S3_real_usage_floor" and item["status"] == "triggered" for item in trace["constraint_evaluations"]))

    def test_s3_and_s4_thin_floors_require_hard_fact_validation_marker(self) -> None:
        s3_result = _validated_stage_pair(
            _s3_flag("partial", "C3", process_framing_met=False),
            _s3_flag("complete", "B3", process_framing_met=True),
            2,
        )
        s3_stage = s3_result["stage_analysis"][2]
        s3_stage["severity"] = "small"
        s3_stage.pop("_postprocess_state", None)
        s3_trace = _derive_one("S3", s3_stage, facts=_evidence_facts("C3", "B3"))
        s3_evaluation = next(
            item for item in s3_trace["constraint_evaluations"] if item["rule"] == "S3_thin_presentation_floor"
        )
        self.assertEqual(s3_trace["severity"], "small")
        self.assertEqual(s3_evaluation["status"], "precondition_missing")
        self.assertEqual(s3_evaluation["reason_code"], "repair_incomplete")

        s4_result = _validated_stage_pair(
            _s4_flag("result_only", "C4"),
            _s4_flag("verified", "B4"),
            3,
        )
        s4_stage = s4_result["stage_analysis"][3]
        s4_stage["severity"] = "small"
        s4_stage.pop("_postprocess_state", None)
        s4_trace = _derive_one("S4", s4_stage, facts=_evidence_facts("C4", "B4"))
        s4_evaluation = next(
            item for item in s4_trace["constraint_evaluations"] if item["rule"] == "S4_thin_effect_floor"
        )
        self.assertEqual(s4_trace["severity"], "small")
        self.assertEqual(s4_evaluation["status"], "precondition_missing")
        self.assertEqual(s4_evaluation["reason_code"], "repair_incomplete")

    def test_finalizer_validates_hard_facts_before_consuming_thin_floors(self) -> None:
        s3_result = _validated_stage_pair(
            _s3_flag("partial", "C3", process_framing_met=False),
            _s3_flag("complete", "B3", process_framing_met=True),
            2,
        )
        s3_stage = s3_result["stage_analysis"][2]
        s3_stage["stage"] = "S3 使用过程"
        s3_stage.pop("_postprocess_state", None)
        s3_stage["severity"] = "small"
        s3_result["video_understanding"] = {
            "creator": {"evidence_units": [{"id": "C3", "evidence_strength": "direct"}]},
            "benchmark": {"evidence_units": [{"id": "B3", "evidence_strength": "explicit"}]},
        }
        finalize_severity_after_repairs(s3_result, {})
        self.assertEqual(s3_stage["severity"], "medium")
        self.assertEqual(
            next(item for item in s3_stage["severity_derivation"]["constraint_evaluations"] if item["rule"] == "S3_thin_presentation_floor")["status"],
            "triggered",
        )

        s4_result = _validated_stage_pair(
            _s4_flag("verified", "C4", effect_salience="clear", effect_maximized=False),
            _s4_flag("verified", "B4", effect_salience="strong", effect_maximized=True),
            3,
        )
        s4_stage = s4_result["stage_analysis"][3]
        s4_stage["stage"] = "S4 效果呈现"
        s4_stage.pop("_postprocess_state", None)
        s4_stage["severity"] = "small"
        s4_result["video_understanding"] = {
            "creator": {"evidence_units": [{"id": "C4", "evidence_strength": "direct"}]},
            "benchmark": {"evidence_units": [{"id": "B4", "evidence_strength": "explicit"}]},
        }
        finalize_severity_after_repairs(s4_result, {})
        self.assertEqual(s4_stage["severity"], "medium")
        self.assertEqual(
            next(item for item in s4_stage["severity_derivation"]["constraint_evaluations"] if item["rule"] == "S4_thin_effect_floor")["status"],
            "triggered",
        )

    def test_s3_uncertain_state_is_not_logged_as_missing_or_predicate_failure(self) -> None:
        creator = _s3_flag("uncertain", "C3")
        benchmark = _s3_flag("complete", "B3")
        result = _validated_stage_pair(creator, benchmark, 2)
        trace = _derive_one("S3", result["stage_analysis"][2], facts=_evidence_facts("C3", "B3"))
        evaluation = next(item for item in trace["constraint_evaluations"] if item["rule"] == "S3_real_usage_floor")
        self.assertEqual(evaluation["status"], "uncertain_fact")
        self.assertEqual(evaluation["reason_code"], "creator_usage_uncertain")

        missing_creator = dict(creator)
        missing_creator["usage_evidence_state"] = None
        result = _validated_stage_pair(missing_creator, benchmark, 2)
        trace = _derive_one("S3", result["stage_analysis"][2], facts=_evidence_facts("C3", "B3"))
        evaluation = next(item for item in trace["constraint_evaluations"] if item["rule"] == "S3_real_usage_floor")
        self.assertEqual(evaluation["status"], "uncertain_fact")
        self.assertEqual(evaluation["reason_code"], "evidence_state_missing")

    def test_s3_none_vs_complete_with_explicit_evidence_triggers_large_floor(self) -> None:
        creator = _s3_flag("none", "C3")
        benchmark = _s3_flag("complete", "B3")
        result = _validated_stage_pair(creator, benchmark, 2)
        stage = result["stage_analysis"][2]
        stage["severity"] = "small"
        trace = _derive_one("S3", stage, facts=_evidence_facts("C3", "B3"))
        self.assertEqual(trace["severity"], "large")
        self.assertEqual(trace["constraints"][0]["rule"], "S3_real_usage_floor")
        self.assertEqual(trace["constraint_evaluations"][0]["reason_code"], "constraint_applied")

    def test_s3_complete_result_only_or_static_conflict_preserves_model(self) -> None:
        for conflict in ("result_only_without_process", "mouth_only_or_static"):
            creator = _s3_flag("none", "C3")
            benchmark = _s3_flag("complete", "B3", **{conflict: True})
            result = _validated_stage_pair(creator, benchmark, 2)
            stage = result["stage_analysis"][2]
            stage["severity"] = "medium"
            trace = _derive_one("S3", stage, facts=_evidence_facts("C3", "B3"))
            evaluation = next(
                item for item in trace["constraint_evaluations"] if item["rule"] == "S3_real_usage_floor"
            )
            self.assertEqual(trace["severity"], "medium")
            self.assertEqual(evaluation["status"], "uncertain_fact")
            self.assertEqual(evaluation["reason_code"], "state_hard_fact_conflict")

    def test_s3_complete_but_inferred_evidence_does_not_trigger_large_floor(self) -> None:
        creator = _s3_flag("none", "C3")
        benchmark = _s3_flag("complete", "B3")
        result = _validated_stage_pair(creator, benchmark, 2)
        stage = result["stage_analysis"][2]
        stage["severity"] = "small"
        trace = _derive_one("S3", stage, facts=_evidence_facts("C3", "B3", "inferred"))
        self.assertEqual(trace["severity"], "small")
        evaluation = next(item for item in trace["constraint_evaluations"] if item["rule"] == "S3_real_usage_floor")
        self.assertEqual(evaluation["status"], "uncertain_evidence_strength")
        self.assertEqual(evaluation["reason_code"], "insufficient_strength")

    def test_s3_hard_fact_conflict_preserves_model_without_rewriting_source_flags(self) -> None:
        creator = _s3_flag("complete", "C3", action_target_contact_met=False)
        benchmark = _s3_flag("complete", "B3")
        result = _validated_stage_pair(creator, benchmark, 2)
        stage = result["stage_analysis"][2]
        stage["severity"] = "medium"
        self.assertTrue(stage["creator_s3"]["action_target_contact_met"] is False)
        self.assertEqual(stage["_postprocess_state"]["evidence_hard_fact_checks"]["creator_s3"]["status"], "state_conflict")
        trace = _derive_one("S3", stage, facts=_evidence_facts("C3", "B3"))
        self.assertEqual(trace["severity"], "medium")
        evaluation = next(item for item in trace["constraint_evaluations"] if item["rule"] == "S3_real_usage_floor")
        self.assertEqual(evaluation["reason_code"], "state_hard_fact_conflict")

    def test_partial_and_result_only_hard_fact_conflicts_are_not_rewritten(self) -> None:
        partial = _s3_flag("partial", "C3", exists=False)
        complete = _s3_flag("complete", "B3")
        result = _validated_stage_pair(partial, complete, 2)
        self.assertEqual(
            result["stage_analysis"][2]["_postprocess_state"]["evidence_hard_fact_checks"]["creator_s3"]["status"],
            "state_conflict",
        )
        self.assertEqual(result["stage_analysis"][2]["creator_s3"]["usage_evidence_state"], "partial")

        result_only = _s4_flag("result_only", "C4", effect_visible=False)
        verified = _s4_flag("verified", "B4")
        result = _validated_stage_pair(result_only, verified, 3)
        self.assertEqual(
            result["stage_analysis"][3]["_postprocess_state"]["evidence_hard_fact_checks"]["creator_s4"]["status"],
            "state_conflict",
        )
        self.assertEqual(result["stage_analysis"][3]["creator_s4"]["effect_evidence_state"], "result_only")

    def test_s4_result_only_preserves_model_and_s4_large_floor_is_audit_only(self) -> None:
        creator = _s4_flag("result_only", "C4", effect_maximized=False)
        benchmark = _s4_flag("verified", "B4", effect_maximized=False, effect_salience="clear")
        result = _validated_stage_pair(creator, benchmark, 3)
        stage = result["stage_analysis"][3]
        stage["severity"] = "small"
        trace = _derive_one("S4", stage, facts=_evidence_facts("C4", "B4"))
        self.assertEqual(trace["severity"], "small")
        self.assertEqual(
            next(item for item in trace["constraint_evaluations"] if item["rule"] == "S4_visible_effect_floor")["status"],
            "predicate_not_met",
        )

        creator = _s4_flag("none", "C4")
        benchmark = _s4_flag("verified", "B4")
        result = _validated_stage_pair(creator, benchmark, 3)
        stage = result["stage_analysis"][3]
        stage["severity"] = "small"
        trace = _derive_one("S4", stage, facts=_evidence_facts("C4", "B4"))
        self.assertEqual(trace["severity"], "small")
        evaluation = next(item for item in trace["constraint_evaluations"] if item["rule"] == "S4_visible_effect_floor")
        self.assertEqual(evaluation["status"], "audit_only")
        self.assertEqual(evaluation["reason_code"], "activation_gate_closed")

    def test_s4_uncertain_state_is_not_logged_as_missing_or_predicate_failure(self) -> None:
        creator = _s4_flag("uncertain", "C4")
        benchmark = _s4_flag("verified", "B4")
        result = _validated_stage_pair(creator, benchmark, 3)
        trace = _derive_one("S4", result["stage_analysis"][3], facts=_evidence_facts("C4", "B4"))
        evaluation = next(item for item in trace["constraint_evaluations"] if item["rule"] == "S4_visible_effect_floor")
        self.assertEqual(evaluation["status"], "uncertain_fact")
        self.assertEqual(evaluation["reason_code"], "creator_effect_uncertain")

        missing_creator = dict(creator)
        missing_creator["effect_evidence_state"] = None
        result = _validated_stage_pair(missing_creator, benchmark, 3)
        trace = _derive_one("S4", result["stage_analysis"][3], facts=_evidence_facts("C4", "B4"))
        evaluation = next(item for item in trace["constraint_evaluations"] if item["rule"] == "S4_visible_effect_floor")
        self.assertEqual(evaluation["status"], "uncertain_fact")
        self.assertEqual(evaluation["reason_code"], "evidence_state_missing")

    def test_s4_large_floor_can_be_enabled_only_after_explicit_activation(self) -> None:
        creator = _s4_flag("none", "C4")
        benchmark = _s4_flag("verified", "B4")
        result = _validated_stage_pair(creator, benchmark, 3)
        stage = result["stage_analysis"][3]
        stage["severity"] = "small"
        activation_evidence = _load_trusted_s4_activation_evidence(_s4_activation_evidence())
        self.assertEqual(validate_s4_large_floor_activation_evidence(activation_evidence), [])
        trace = _derive_one(
            "S4",
            stage,
            facts=_evidence_facts("C4", "B4"),
            activation_evidence=activation_evidence,
        )
        self.assertEqual(trace["severity"], "large")
        self.assertEqual(trace["constraints"][0]["rule"], "S4_visible_effect_floor")

    def test_s4_large_floor_rejects_incomplete_activation_evidence(self) -> None:
        evidence = _load_trusted_s4_activation_evidence(_s4_activation_evidence())
        evidence.payload["repeat_stability"] = {"status": "measured", "stable": True, "runs": 1}
        self.assertTrue(validate_s4_large_floor_activation_evidence(evidence))

    def test_s4_activation_rejects_loaded_payload_mutation(self) -> None:
        evidence = _load_trusted_s4_activation_evidence(_s4_activation_evidence())
        evidence.payload["blind_cohort"]["fresh"] = False
        errors = validate_s4_large_floor_activation_evidence(evidence)
        self.assertIn("modified after loading", errors[0])

    def test_s4_activation_rejects_result_artifact_dict_even_when_shape_is_valid(self) -> None:
        self.assertTrue(validate_s4_large_floor_activation_evidence(_s4_activation_evidence()))

    def test_finalizer_does_not_trust_activation_metadata_from_result_artifact(self) -> None:
        result = _validated_stage_pair(_s4_flag("none", "C4"), _s4_flag("verified", "B4"), 3)
        stage = result["stage_analysis"][3]
        stage["stage"] = "S4 效果呈现"
        stage["severity"] = "small"
        result.update(_evidence_facts("C4", "B4"))
        result["derive_activation_evidence"] = _s4_activation_evidence()

        # Passing the result as analysis reproduces the old evaluator call shape.
        # The raw artifact field must remain inert because activation is an explicit
        # trusted argument, not data derive may discover inside the result.
        finalize_severity_after_repairs(result, result)

        self.assertEqual(stage["severity"], "small")
        evaluation = next(
            item for item in stage["severity_derivation"]["constraint_evaluations"]
            if item["rule"] == "S4_visible_effect_floor"
        )
        self.assertEqual(evaluation["status"], "audit_only")

    def test_calibration_cards_require_role_specific_floor_boundaries(self) -> None:
        evidence = _s4_activation_evidence()
        cards = [dict(card) for card in evidence["calibration"]["cards"]]
        for card in cards:
            if card["stage"] == "S3":
                card["expected_creator_state"] = "none"
        errors = validate_derive_calibration_cards(cards)
        self.assertTrue(any("S3 creator boundary coverage missing: partial" in error for error in errors))

        cards = [dict(card) for card in evidence["calibration"]["cards"]]
        for card in cards:
            if card["stage"] == "S4":
                card["expected_benchmark_state"] = "verified"
        errors = validate_derive_calibration_cards(cards)
        self.assertTrue(any("S4 benchmark boundary coverage missing: result_only" in error for error in errors))

    def test_benchmark_result_only_has_closed_audit_reason_code(self) -> None:
        result = _validated_stage_pair(
            _s4_flag("verified", "C4"),
            _s4_flag("result_only", "B4"),
            3,
        )
        trace = _derive_one("S4", result["stage_analysis"][3], facts=_evidence_facts("C4", "B4"))
        evaluation = next(item for item in trace["constraint_evaluations"] if item["rule"] == "S4_visible_effect_floor")
        self.assertEqual(evaluation["reason_code"], "benchmark_effect_result_only")

    def test_repeat_stability_and_floor_coverage_are_reported_without_inference(self) -> None:
        stable = summarize_repeat_stability(
            [
                {"action_target_contact_met": True, "critical_action_continuity_met": False},
                {"action_target_contact_met": True, "critical_action_continuity_met": False},
                {"action_target_contact_met": True, "critical_action_continuity_met": False},
                {"action_target_contact_met": True, "critical_action_continuity_met": False},
                {"action_target_contact_met": True, "critical_action_continuity_met": False},
            ],
            ("action_target_contact_met", "critical_action_continuity_met"),
        )
        self.assertTrue(stable["stable"])
        self.assertEqual(stable["status"], "measured")
        one_run = summarize_repeat_stability([{"action_target_contact_met": True}], ("action_target_contact_met",))
        self.assertFalse(one_run["stable"])
        self.assertEqual(one_run["status"], "insufficient_runs")
        unstable = summarize_repeat_stability(
            [{"action_target_contact_met": True}, {"action_target_contact_met": False}],
            ("action_target_contact_met",),
        )
        self.assertFalse(unstable["stable"])
        self.assertEqual(unstable["unstable_fields"], ["action_target_contact_met"])
        missing = summarize_repeat_stability(
            [{"action_target_contact_met": None}, {"action_target_contact_met": None}],
            ("action_target_contact_met",),
        )
        self.assertFalse(missing["stable"])
        self.assertEqual(missing["fields"]["action_target_contact_met"]["missing_runs"], 2)

        coverage = summarize_floor_coverage(
            [
                {"ground_truth_structural_gap": True, "floor_applied": True},
                {"ground_truth_structural_gap": True, "floor_applied": False},
                {"ground_truth_structural_gap": True, "floor_applied": True},
                {"ground_truth_structural_gap": False, "floor_applied": True, "derive_regression": True},
            ]
        )
        self.assertEqual(coverage["eligible_structural_gap_cases"], 3)
        self.assertEqual(coverage["captured_by_floor"], 2)
        self.assertAlmostEqual(coverage["coverage"], 2 / 3)
        self.assertEqual(coverage["derive_regressions"], 1)

        valid_card = dict(_s4_activation_evidence()["calibration"]["cards"][1])
        self.assertEqual(validate_derive_calibration_card(valid_card), [])
        invalid_card = dict(valid_card)
        invalid_card["partition"] = "blind"
        self.assertTrue(validate_derive_calibration_card(invalid_card))

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

    def test_s5_invalid_source_status_is_uncertain_not_absent(self) -> None:
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
                "creator": {"evidence_units": [{
                    "id": "C5",
                    "trust_source_status": "invalid-status",
                    "endorsement_verbal": False,
                    "endorsement_visual": False,
                }]},
                "benchmark": {"evidence_units": [{
                    "id": "B5",
                    "trust_source_status": "invalid-status",
                    "endorsement_verbal": False,
                    "endorsement_visual": False,
                }]},
            },
        }
        reconcile_s5_trust_sources(result, True)
        finalize_severity_after_repairs(result, {})
        self.assertEqual(stage["creator_s5"]["_s5_source_status"], "unknown")
        self.assertEqual(stage["benchmark_s5"]["_s5_source_status"], "unknown")
        self.assertEqual(stage["severity"], "large")


if __name__ == "__main__":
    unittest.main()
