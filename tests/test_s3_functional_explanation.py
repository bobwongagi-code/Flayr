"""Spoken explanations participate without becoming visual-use evidence."""

import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from flayr_core.evidence_states import (
    S3_HARD_FACT_FIELDS,
    has_s3_functional_explanation,
    stage_flag_has_absent_evidence_state,
)
from flayr_core.llm.parse import normalize_s3_flags
from flayr_core.postprocess.derive import (
    _ConstraintCollector,
    _collect_s3_constraints,
    _s3_usage_exec,
)
from flayr_core.postprocess.proposition import _absolute_status
from flayr_core.postprocess.proposition import materialize_quality_audits
from flayr_core.postprocess.repair_evidence import validate_s3_s4_hard_fact_consistency
from flayr_core.postprocess.validate import (
    _validate_structured_flag_evidence_ids,
    validate_s3_usage_flags,
)
from flayr_core.stage_contract_registry import (
    required_stage_signals_satisfied,
    stage_evidence_contract,
)
from flayr_core.stage_evidence_contracts import (
    STAGE1_ACQUISITION_VERSION,
    STAGE_EVIDENCE_CONTRACT_VERSION,
    _stage_check_issues,
    freeze_stage_evidence,
    stage_codes,
)


def spoken_flag():
    flag = {key: False for key in S3_HARD_FACT_FIELDS}
    flag.update(
        exists=True,
        functional_explanation_met=True,
        usage_evidence_state="none",
        mouth_only_or_static=True,
        evidence_ids=["C1"],
        module_type="unknown",
        scene_mode="unknown",
        presentation_overlays=["none"],
        start_seconds=0,
        end_seconds=5,
        usage_reason="Explains use conditions and steps.",
    )
    for key in (
        "process_framing_met",
        "usage_context_fit",
        "continuity_met",
        "richness_met",
        "single_scene_continuity_met",
        "single_scene_variation_met",
        "multi_scene_logic_met",
        "multi_scene_transition_met",
        "multi_scene_role_adaptation_met",
        "role_design_met",
        "role_interaction_met",
        "distinct_personas_met",
        "steps_clear_met",
        "pov_immersive_met",
    ):
        flag[key] = False
    return flag


def active_stage1_side(
    *,
    mode="spoken",
    status="present",
    fake_functional_binding=False,
):
    """Build a frozen v7 side whose S3 readiness is code-owned and active."""
    physical_signals = ["target_contact", "real_action", "application_change"]
    if mode == "spoken":
        unit = {
            "id": "C1",
            "evidence_strength": "explicit",
            "voiceover": "Explains suitable surfaces and application steps.",
            "time_range": "0.0s - 5.0s",
        }
        observed_signals = ["functional_explanation"]
        missing_signals = physical_signals
        signal_bindings = {
            "functional_explanation": {
                "status": "supported",
                "evidence_ids": ["C1"],
            }
        }
        speech_mode = "spoken"
    else:
        unit = {
            "id": "C1",
            "evidence_strength": "direct",
            "visual_fact": "Applies the product to the target surface.",
            "time_range": "0.0s - 5.0s",
        }
        observed_signals = physical_signals
        missing_signals = ["functional_explanation"]
        signal_bindings = {
            signal: {
                "status": "supported",
                "evidence_ids": ["C1"],
            }
            for signal in physical_signals
        }
        if fake_functional_binding:
            signal_bindings["functional_explanation"] = {
                "status": "supported",
                "evidence_ids": ["C1"],
            }
        speech_mode = "visual_driven"

    if status == "unknown":
        observed_signals = []
        missing_signals = []
        signal_bindings = {}

    check = {
        "stage": "S3",
        "status": status,
        "coverage": "complete" if status == "present" else "unknown",
        "evidence_ids": ["C1"] if status == "present" else [],
        "observed_signals": observed_signals,
        "missing_signals": missing_signals,
        "signal_bindings": signal_bindings,
        "invalid_evidence_ids": [],
        "invalid_observed_signals": [],
        "invalid_missing_signals": [],
        "invalid_signal_bindings": [],
        "observed_disqualifiers": [],
        "invalid_observed_disqualifiers": [],
        "evidence_strength": "direct" if status == "present" else None,
        "reason": "fixture",
    }
    side = {
        "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
        "evidence_units": [unit],
        "stage_evidence_checks": [check],
        "stage1_acquisition": {
            "version": STAGE1_ACQUISITION_VERSION,
            "source": "pipeline",
            "status": "complete",
            "input_mode": "native_video",
            "speech_mode": speech_mode,
            "duration_seconds": 5.0,
            "channels": {
                "visual": {
                    "status": "ready",
                    "coverage": "full",
                    "count": 1,
                    "boundary_precision": "frame",
                },
                "voiceover": {
                    "status": "ready" if mode == "spoken" else "unknown",
                    "coverage": "full" if mode == "spoken" else "unknown",
                    "count": 1 if mode == "spoken" else 0,
                    "boundary_precision": "word",
                },
                "subtitle": {
                    "status": "unknown",
                    "coverage": "unknown",
                    "count": 0,
                    "boundary_precision": "word",
                },
                "audio": {
                    "status": "unknown",
                    "coverage": "unknown",
                    "count": 0,
                    "boundary_precision": "unknown",
                },
            },
            "visual_input_timestamps": [],
            "native_video_windows": [{"start_seconds": 0.0, "end_seconds": 5.0}],
            "stage_coverage": {
                stage: {"count": 1, "status": "observed"} for stage in stage_codes()
            },
            "provider_artifacts": [],
            "errors": [],
        },
        "stage1_recovery": {"source": "pipeline", "status": "not_needed"},
        "evidence_budget_exceeded": False,
    }
    freeze_stage_evidence(side)
    return side


def s3_result(creator_side, benchmark_side, creator_flag=None, benchmark_flag=None):
    return {
        "video_understanding": {
            "creator": creator_side,
            "benchmark": benchmark_side,
        },
        "stage_analysis": [
            {},
            {},
            {
                "creator_s3": creator_flag or spoken_flag(),
                "benchmark_s3": benchmark_flag or spoken_flag(),
            },
        ],
    }


class FunctionalExplanationTest(unittest.TestCase):
    def test_alternative_does_not_weaken_physical_signal_group(self):
        contract = stage_evidence_contract("S3")
        self.assertTrue(
            required_stage_signals_satisfied(contract, ["functional_explanation"])
        )
        self.assertTrue(
            required_stage_signals_satisfied(
                contract, ["target_contact", "real_action", "application_change"]
            )
        )
        self.assertFalse(required_stage_signals_satisfied(contract, ["real_action"]))
        self.assertFalse(required_stage_signals_satisfied(contract, []))

    def test_spoken_qualification_requires_binding_channel_and_strength(self):
        check = dict(
            status="present",
            coverage="complete",
            evidence_ids=["C1"],
            observed_signals=["functional_explanation"],
            missing_signals=[],
            signal_bindings={
                "functional_explanation": dict(status="supported", evidence_ids=["C1"])
            },
        )
        units = {
            "C1": dict(
                evidence_strength="explicit",
                voiceover="Explains suitable surfaces and application steps.",
            )
        }
        contract = stage_evidence_contract("S3")
        self.assertEqual(_stage_check_issues(contract, check, units), [])
        for change, expected in (
            (
                {"voiceover": "", "visual_fact": "Holds product"},
                "signal_channel_missing",
            ),
            ({"evidence_strength": "inferred"}, "without_explicit_strength"),
        ):
            bad_units = copy.deepcopy(units)
            bad_units["C1"].update(change)
            self.assertTrue(
                any(
                    expected in item
                    for item in _stage_check_issues(contract, check, bad_units)
                )
            )
        check["signal_bindings"] = {}
        self.assertTrue(
            any(
                "binding" in item
                for item in _stage_check_issues(contract, check, units)
            )
        )

    def test_actual_use_cannot_be_bound_to_voice_only(self):
        signals = ["target_contact", "real_action", "application_change"]
        check = dict(
            status="present",
            coverage="complete",
            evidence_ids=["C1"],
            observed_signals=signals,
            missing_signals=[],
            signal_bindings={
                key: dict(status="supported", evidence_ids=["C1"]) for key in signals
            },
        )
        units = {"C1": dict(evidence_strength="explicit", voiceover="Says it is used.")}
        self.assertTrue(
            any(
                "signal_channel_missing" in item
                for item in _stage_check_issues(
                    stage_evidence_contract("S3"), check, units
                )
            )
        )

    def test_parse_validate_and_consistency_preserve_no_actual_use(self):
        flag = normalize_s3_flags(spoken_flag())
        self.assertTrue(has_s3_functional_explanation(flag))
        self.assertEqual(flag["usage_evidence_state"], "none")
        self.assertFalse(stage_flag_has_absent_evidence_state("S3", flag))
        result = {
            "stage_analysis": [
                {},
                {},
                {"creator_s3": flag, "benchmark_s3": copy.deepcopy(flag)},
                {},
            ]
        }
        validate_s3_usage_flags(result, {})
        validate_s3_s4_hard_fact_consistency(result)
        checks = result["stage_analysis"][2]["_postprocess_state"][
            "evidence_hard_fact_checks"
        ]
        self.assertEqual(checks["creator_s3"]["status"], "consistent")
        flag["real_usage_met"] = True
        validate_s3_s4_hard_fact_consistency(result)
        self.assertEqual(
            result["stage_analysis"][2]["_postprocess_state"][
                "evidence_hard_fact_checks"
            ]["creator_s3"]["status"],
            "state_conflict",
        )

    def test_missing_or_invalid_explanation_is_not_inferred(self):
        for value in (None, False):
            flag = spoken_flag()
            flag["functional_explanation_met"] = value
            self.assertFalse(has_s3_functional_explanation(flag))
            with self.assertRaises(SystemExit):
                validate_s3_usage_flags(
                    {
                        "stage_analysis": [
                            {},
                            {},
                            {"creator_s3": flag, "benchmark_s3": flag},
                        ]
                    },
                    {},
                )
        flag = spoken_flag()
        flag["evidence_ids"] = []
        self.assertFalse(has_s3_functional_explanation(flag))
        with self.assertRaises(SystemExit):
            validate_s3_usage_flags(
                {
                    "stage_analysis": [
                        {},
                        {},
                        {"creator_s3": flag, "benchmark_s3": flag},
                    ]
                },
                {},
            )

    def test_explanation_neither_gets_zero_score_nor_visual_gap_floor(self):
        stage = {"creator_s3": spoken_flag(), "benchmark_s3": spoken_flag()}
        self.assertIsNone(_s3_usage_exec(stage)["creator_exec"])
        self.assertEqual(_absolute_status("S3", stage["creator_s3"])[0], "unknown")
        stage["benchmark_s3"]["usage_evidence_state"] = "complete"
        collector = _ConstraintCollector()
        _collect_s3_constraints(collector, stage, {})
        self.assertEqual(collector.floors, [])
        self.assertEqual(len(collector.evaluations), 2)

    def test_physical_stage_rejects_visual_only_functional_binding(self):
        side = active_stage1_side(mode="physical", fake_functional_binding=True)
        issues = _stage_check_issues(
            stage_evidence_contract("S3"),
            side["stage_evidence_checks"][0],
            {"C1": side["evidence_units"][0]},
        )
        self.assertIn("S3:signal_channel_missing:functional_explanation", issues)

    def test_segmented_v7_rejects_visual_only_explanation(self):
        result = s3_result(
            active_stage1_side(mode="physical"),
            active_stage1_side(mode="physical"),
        )
        with self.assertRaisesRegex(SystemExit, "functional_explanation_met 必须引用"):
            validate_s3_usage_flags(
                result,
                {"s3_flags_required": False, "evidence_state_required": False},
            )

    def test_segmented_v7_accepts_qualified_spoken_explanation(self):
        result = s3_result(
            active_stage1_side(mode="spoken"),
            active_stage1_side(mode="spoken"),
        )
        validate_s3_usage_flags(
            result,
            {"s3_flags_required": False, "evidence_state_required": False},
        )

    def test_current_v7_non_unresolved_flag_requires_explicit_explanation_bool(self):
        flag = spoken_flag()
        flag.pop("functional_explanation_met")
        flag["usage_evidence_state"] = "partial"
        result = s3_result(
            active_stage1_side(mode="physical"),
            active_stage1_side(mode="physical"),
            flag,
            copy.deepcopy(flag),
        )
        with self.assertRaisesRegex(
            SystemExit, "functional_explanation_met 必须是 bool"
        ):
            validate_s3_usage_flags(
                result,
                {"s3_flags_required": False, "evidence_state_required": False},
            )

    def test_unresolved_v7_side_does_not_require_strong_explanation_evidence(self):
        result = s3_result(
            active_stage1_side(mode="spoken", status="unknown"),
            active_stage1_side(mode="spoken", status="unknown"),
        )
        validate_s3_usage_flags(
            result,
            {"s3_flags_required": False, "evidence_state_required": False},
        )

    def test_code_owned_absent_v7_side_may_omit_explanation_bool(self):
        flag = spoken_flag()
        flag.update(
            exists=False,
            evidence_ids=[],
            usage_evidence_state="none",
            usage_process_visible=False,
            real_usage_met=False,
            core_selling_point_visible=False,
            mouth_only_or_static=False,
        )
        flag.pop("functional_explanation_met")
        result = s3_result(
            active_stage1_side(mode="physical", status="absent"),
            active_stage1_side(mode="physical", status="absent"),
            flag,
            copy.deepcopy(flag),
        )
        validate_s3_usage_flags(
            result,
            {"s3_flags_required": False, "evidence_state_required": False},
        )

    def test_physical_use_with_explanation_keeps_execution_and_complete_status(self):
        def physical_explanation_flag():
            flag = spoken_flag()
            flag.update(
                usage_evidence_state="complete",
                usage_process_visible=True,
                real_usage_met=True,
                core_selling_point_visible=True,
                action_proof_met=True,
                action_target_contact_met=True,
                action_application_change_visible=True,
                critical_action_continuity_met=True,
                usage_context_fit=True,
                mouth_only_or_static=False,
                result_only_without_process=False,
                fake_or_staged=False,
                missing_selling_points=[],
                scene_mode="single_scene",
                single_scene_continuity_met=True,
                single_scene_variation_met=True,
                continuity_met=True,
                richness_met=True,
            )
            return flag

        stage = {
            "creator_s3": physical_explanation_flag(),
            "benchmark_s3": physical_explanation_flag(),
        }
        execution = _s3_usage_exec(stage)
        self.assertIsNotNone(execution["creator_exec"])
        self.assertGreater(execution["creator_exec"], 0)
        self.assertEqual(_absolute_status("S3", stage["creator_s3"])[0], "complete")

    def test_spoken_pair_materializes_unknown_delivery_without_changing_relation_or_gap(
        self,
    ):
        stage = {
            "creator_s3": spoken_flag(),
            "benchmark_s3": spoken_flag(),
            "stage_standard_delivery": "both",
            "relation": "equivalent",
            "model_gap_magnitude": "none",
            "model_severity": "none",
            "severity": "none",
        }
        result = {"stage_analysis": [{}, {}, stage]}
        materialize_quality_audits(result, {})
        self.assertEqual(stage["stage_standard_delivery"], "unknown")
        self.assertEqual(stage["computed_stage_standard_delivery"], "unknown")
        self.assertEqual(stage["relation"], "equivalent")
        for key in ("model_gap_magnitude", "model_severity", "severity"):
            self.assertEqual(stage[key], "none")

    def test_flag_cannot_relabel_visual_evidence_as_explanation(self):
        flag = spoken_flag()
        side = {
            "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "evidence_units": [{"id": "C1", "visual_fact": "Applies product"}],
            "stage_evidence_checks": [{"stage": "S3", "signal_bindings": {}}],
        }
        result = {"video_understanding": {"creator": side}}
        with patch(
            "flayr_core.postprocess.validate.qualified_stage_evidence_ids",
            return_value={"C1"},
        ):
            errors = _validate_structured_flag_evidence_ids(
                result, "creator", "s3", flag
            )
            self.assertTrue(any("functional_explanation" in error for error in errors))
            side["stage_evidence_checks"][0]["signal_bindings"] = {
                "functional_explanation": {
                    "status": "supported",
                    "evidence_ids": ["C1"],
                }
            }
            self.assertEqual(
                _validate_structured_flag_evidence_ids(result, "creator", "s3", flag),
                [],
            )

    def test_flag_cannot_drop_locked_explanation_as_absence(self):
        flag = spoken_flag()
        flag.update(exists=False, functional_explanation_met=False, evidence_ids=[])
        result = s3_result(
            active_stage1_side(mode="spoken"),
            active_stage1_side(mode="spoken"),
            flag,
            copy.deepcopy(flag),
        )
        for required in (True, False):
            with self.assertRaisesRegex(SystemExit, "不得丢弃"):
                validate_s3_usage_flags(result, {"evidence_state_required": required})


if __name__ == "__main__":
    unittest.main()
