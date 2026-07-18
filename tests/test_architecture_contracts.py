"""当前主链的架构契约回归：不调用模型、不读取真实视频。"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import flayr
from flayr_core import proposal_video, subtitle_track, translation, utils, video, voice_clone
from flayr_core.report import executive_summary, render_global_cause_note, render_global_diagnosis, render_improvement_meta, stage_skipped
from flayr_core.llm import api as llm_api
from flayr_core.llm import media as llm_media
from flayr_core.llm import pipeline
from flayr_core.llm.analysis_contract import (
    AnalysisContractError,
    validate_normalized_analysis_contract,
    validate_raw_analysis_envelope,
)
from flayr_core.llm.json_codec import parse_json_text
from flayr_core.llm.product_profile import normalize_product_profile, normalize_proof_contract
from flayr_core.llm.s4_visual_verifier import (
    _visual_verifier_skip_reason,
    _visual_verifier_scope_rule,
    apply_s4_visual_verifier_result,
    build_s4_visual_verifier_payload,
)
from flayr_core.llm.payload import (
    build_comparison_eligibility_payload,
    build_improvement_reconciliation_payload,
    build_llm_comparison_payload,
    build_llm_repair_payload,
    build_stage_review_payload,
    build_video_fact_payload,
    full_analysis_output_budget,
    build_video_identity_payload,
    load_brand_proposition,
    resolve_brand_key,
)
from flayr_core.llm.parse import (
    normalize_analysis_result,
    normalize_comparison_contract,
    normalize_comparison_eligibility,
    normalize_hook_flags,
    normalize_multimodal_assessment,
    normalize_module_id,
    normalize_s3_flags,
    normalize_video_fact_result,
    normalize_video_understanding,
)
from flayr_core.multimodal import channel_requirement_for, multimodal_execution
from flayr_core.llm.pipeline import apply_llm_json_patches, preserve_valid_repair_sections
from flayr_core.postprocess.proposition import materialize_cross_stage_inputs, materialize_quality_audits
from flayr_core.postprocess.chain import stamp_comparison_eligibility
from flayr_core.postprocess.derive import _derive_one, _s3_usage_exec, _s4_effect_exec, _s6_cta_exec
from flayr_core.postprocess.global_diagnosis import materialize_global_diagnosis
from flayr_core.postprocess.repair import (
    align_stage_flag_evidence,
    apply_comparison_eligibility,
    prune_multimodal_evidence_to_stage,
    reconcile_s3_s4_evidence_coherence,
    reconcile_unsupported_cta,
    reconcile_s5_trust_sources,
    stabilize_improvement_priorities,
)
from flayr_core.postprocess.repair_stages import infer_s1_boundary_candidate
from flayr_core.postprocess.health_rewrite import is_child_toothpaste_context
from flayr_core.postprocess.validate import (
    validate_analysis_dimensions,
    validate_chain_relationships,
    validate_multimodal_assessments,
    validate_required_stage_narratives,
    validate_s1_hook_flags,
    validate_s3_usage_flags,
    validate_s6_cta_flags,
    validate_stage_time_coherence,
)
from flayr_core.prompt import write_analysis_input
from flayr_core.proposition_contract import build_product_proposition_contract
from flayr_core.stage_catalog import DEFAULT_STAGES, fallback_artifact_ranges, stage_tuples
from flayr_core.stage_ownership import CERTIFICATION_OWNERSHIP_PROMPT


class ArchitectureContractTests(unittest.TestCase):
    @staticmethod
    def _multimodal(
        role_prefix: str,
        *,
        visual: str = "strong_positive",
        speech: str = "neutral",
        text: str = "positive",
        sound_rhythm: str = "neutral",
        dominant: str = "visual",
        relation: str = "complementary",
        effect: str = "strong",
        compensation: bool = True,
    ) -> dict[str, object]:
        evidence_id = "C1" if role_prefix == "creator" else "B1"
        return {
            "channel_impacts": {
                "visual": visual,
                "speech": speech,
                "text": text,
                "sound_rhythm": sound_rhythm,
            },
            "channel_evidence_ids": {
                "visual": [evidence_id],
                "speech": [] if speech == "absent" else [evidence_id],
                "text": [] if text == "absent" else [evidence_id],
                "sound_rhythm": [] if sound_rhythm == "absent" else [evidence_id],
            },
            "dominant_channel": dominant,
            "cross_channel_relation": relation,
            "integrated_effect": effect,
            "compensation_applied": compensation,
            "integration_reason": "强视觉承担核心任务，其他渠道只作补充或保持中性。",
        }

    def test_multimodal_normalization_keeps_closed_decision_space(self) -> None:
        normalized = normalize_multimodal_assessment(
            {
                "channel_impacts": {"visual": "STRONG_POSITIVE", "speech": "invented"},
                "channel_evidence_ids": {"visual": ["C1"]},
                "dominant_channel": "visual",
                "cross_channel_relation": "complementary",
                "integrated_effect": "strong",
                "compensation_applied": True,
                "integration_reason": "视觉主导。",
            }
        )
        self.assertEqual(normalized["channel_impacts"]["visual"], "strong_positive")
        self.assertEqual(normalized["channel_impacts"]["speech"], "unknown")
        self.assertEqual(normalized["channel_evidence_ids"]["visual"], ["C1"])

    def test_multimodal_normalization_repairs_mechanical_contradictions(self) -> None:
        normalized = normalize_multimodal_assessment(
            {
                "channel_impacts": {
                    "visual": "positive",
                    "speech": "neutral",
                    "text": "absent",
                    "sound_rhythm": "neutral",
                },
                "dominant_channel": "speech",
                "integrated_effect": "effective",
                "compensation_applied": True,
            }
        )
        self.assertEqual(normalized["dominant_channel"], "visual")
        self.assertFalse(normalized["compensation_applied"])

        no_positive = normalize_multimodal_assessment(
            {
                "channel_impacts": {
                    "visual": "neutral",
                    "speech": "negative",
                    "text": "absent",
                    "sound_rhythm": "neutral",
                },
                "dominant_channel": "visual",
                "integrated_effect": "strong",
            }
        )
        self.assertEqual(no_positive["integrated_effect"], "weak")

    def test_channel_requirement_axis_is_canonical(self) -> None:
        self.assertEqual(channel_requirement_for("S1")["level"], "any_channel_sufficient")
        self.assertEqual(channel_requirement_for("S2")["level"], "any_channel_sufficient")
        self.assertEqual(channel_requirement_for("S3")["required_signal"], "visible_usage_process")
        self.assertEqual(channel_requirement_for("S4")["required_signal"], "visible_effect")
        self.assertEqual(channel_requirement_for("S5")["level"], "source_grounded")
        self.assertEqual(channel_requirement_for("S6")["required_signal"], "explicit_purchase_action")

    def test_s1_strong_visual_can_compensate_weak_or_absent_speech(self) -> None:
        creator = self._multimodal("creator", speech="absent")
        benchmark = self._multimodal("benchmark", compensation=False)
        stage = {
            "stage": "S1 Hook",
            "creator_execution": 0.5,
            "benchmark_execution": 2.0,
            "creator_multimodal": creator,
            "benchmark_multimodal": benchmark,
        }
        trace = _derive_one("S1", stage, {"S1": 1.0}, [])
        self.assertEqual(trace["derived_creator_execution"], 2.0)
        self.assertEqual(trace["derived_benchmark_execution"], 2.0)
        self.assertEqual(trace["severity"], "small")

    def test_multimodal_s1_is_not_reinflated_by_legacy_anchor_gap(self) -> None:
        stage = {
            "stage": "S1 Hook",
            "creator_execution": 0.5,
            "benchmark_execution": 2.0,
            "creator_multimodal": self._multimodal("creator", effect="effective", compensation=False),
            "benchmark_multimodal": self._multimodal("benchmark", effect="strong", compensation=False),
            "creator_hook": {"anchors_proposition": False},
            "benchmark_hook": {"anchors_proposition": True},
            "painpoint_relevance": "benchmark_only",
        }
        trace = _derive_one("S1", stage, {"S1": 1.5}, [], allow_legacy_text_fallback=False)
        self.assertEqual(trace["derived_creator_execution"], 1.0)
        self.assertEqual(trace["derived_benchmark_execution"], 2.0)
        self.assertEqual(trace["E"], 1.0)
        self.assertEqual(trace["severity"], "medium")

    def test_multimodal_gate_rejects_strong_effect_with_strong_conflict(self) -> None:
        creator = self._multimodal(
            "creator",
            speech="strong_negative",
            relation="conflicting",
            effect="strong",
            compensation=False,
        )
        benchmark = self._multimodal("benchmark", compensation=False)
        result = {
            "stage_analysis": [{
                "stage": "S1 Hook",
                "creator_evidence_ids": ["C1"],
                "benchmark_evidence_ids": ["B1"],
                "creator_multimodal": creator,
                "benchmark_multimodal": benchmark,
            }]
        }
        with self.assertRaises(SystemExit):
            validate_multimodal_assessments(result, {"multimodal_assessment_required": True})

    def test_multimodal_gate_accepts_locked_same_stage_unit_not_selected_as_summary(self) -> None:
        creator = self._multimodal("creator", compensation=False)
        creator["channel_evidence_ids"] = {
            "visual": ["C2"],
            "speech": ["C2"],
            "text": ["C2"],
            "sound_rhythm": ["C2"],
        }
        result = {
            "video_understanding": {
                "creator": {"evidence_units": [{"id": "C2", "functions": ["S1_hook"]}]},
                "benchmark": {"evidence_units": [{"id": "B1", "functions": ["S1_hook"]}]},
            },
            "stage_analysis": [{
                "stage": "S1 Hook",
                "creator_evidence_ids": ["C1"],
                "benchmark_evidence_ids": ["B1"],
                "creator_multimodal": creator,
                "benchmark_multimodal": self._multimodal("benchmark", compensation=False),
            }],
        }
        validate_multimodal_assessments(result, {"multimodal_assessment_required": True})

    def test_s3_multimodal_can_enhance_complete_process_but_not_replace_it(self) -> None:
        assessment = self._multimodal("creator", compensation=False)
        complete = {
            "creator_multimodal": assessment,
            "creator_s3": {
                "usage_process_visible": True,
                "core_selling_point_visible": True,
                "action_proof_met": True,
                "action_target_contact_met": True,
                "action_application_change_visible": True,
                "critical_action_continuity_met": True,
                "missing_selling_points": [],
            },
        }
        missing = {
            "creator_multimodal": assessment,
            "creator_s3": {
                "usage_process_visible": False,
                "core_selling_point_visible": False,
            },
        }
        self.assertEqual(multimodal_execution("S3", complete, "creator", 1.0), 2.0)
        self.assertEqual(multimodal_execution("S3", missing, "creator", 0.0), 0.0)

    def test_evidence_stages_cannot_be_rescued_by_atmosphere(self) -> None:
        for stage_id in ("S4", "S5", "S6"):
            stage = {"creator_multimodal": self._multimodal("creator", effect="strong")}
            self.assertEqual(multimodal_execution(stage_id, stage, "creator", 0.0), 0.0)

    def test_s4_thin_effect_floor_runs_when_coarse_execution_scores_tie(self) -> None:
        def effect_flag(salience: str, maximized: bool) -> dict[str, object]:
            return {
                "effect_type": "before_after",
                "effect_visible": True,
                "effect_salience": salience,
                "effect_proposition_matched": True,
                "comparison_control_met": False,
                "closeup_or_focus_met": True,
                "visual_difference_observed": True,
                "module_constraints_met": True,
                "effect_maximized": maximized,
                "requires_close_inspection": False,
                "effect_attribution_supported": True,
                "result_only_without_process": False,
                "process_linked_effect": True,
                "tamper_or_cut_risk": False,
            }

        stage = {
            "stage": "S4 效果呈现",
            "creator_s4": effect_flag("clear", False),
            "benchmark_s4": effect_flag("strong", True),
            "creator_multimodal": self._multimodal("creator", effect="effective", compensation=False),
            "benchmark_multimodal": self._multimodal("benchmark", effect="strong", compensation=False),
        }
        trace = _derive_one("S4", stage, {"S4": 1.0}, [])
        self.assertEqual(trace["derived_creator_execution"], 1.0)
        self.assertEqual(trace["derived_benchmark_execution"], 1.0)
        self.assertEqual(trace["severity"], "medium")

    def test_multimodal_evidence_alignment_only_adds_same_stage_locked_units(self) -> None:
        result = {
            "video_understanding": {
                "creator": {
                    "evidence_units": [
                        {"id": "C1", "time_range": "0s - 3s"},
                        {"id": "C2", "time_range": "3s - 6s"},
                        {"id": "C9", "time_range": "20s - 24s"},
                    ]
                },
                "benchmark": {"evidence_units": []},
            },
            "stage_analysis": [
                {
                    "stage": "S1 Hook",
                    "creator_time_range": "0s - 6s",
                    "creator_evidence_ids": ["C1"],
                    "creator_multimodal": {
                        "channel_evidence_ids": {"visual": ["C2", "C9", "UNKNOWN"]}
                    },
                }
            ],
        }
        align_stage_flag_evidence(result)
        self.assertEqual(result["stage_analysis"][0]["creator_evidence_ids"], ["C1", "C2"])

    def test_variant_attribution_uses_visual_threshold_and_explicit_comparison(self) -> None:
        understanding = normalize_video_understanding(
            {
                "creator": {
                    "evidence_units": [
                        {
                            "id": "C1",
                            "variant_ids": ["black", "silver"],
                            "variant_visual_shares": {"black": 0.70, "silver": 0.30},
                            "variant_speech_shares": {"black": 0.20, "silver": 0.80},
                            "variant_relation_mode": "single_focus",
                            "comparison_purpose_explicit": False,
                        },
                        {
                            "id": "C2",
                            "variant_ids": ["black", "silver"],
                            "variant_visual_shares": {"black": 0.69, "silver": 0.31},
                            "variant_speech_shares": {},
                            "variant_relation_mode": "single_focus",
                            "comparison_purpose_explicit": False,
                        },
                        {
                            "id": "C3",
                            "variant_ids": ["black", "silver"],
                            "variant_visual_shares": {"black": 0.5, "silver": 0.5},
                            "variant_speech_shares": {"black": 0.5, "silver": 0.5},
                            "variant_relation_mode": "explicit_comparison",
                            "comparison_purpose_explicit": True,
                        },
                    ]
                }
            }
        )
        units = understanding["creator"]["evidence_units"]
        self.assertEqual(units[0]["primary_variant_id"], "black")
        self.assertTrue(units[0]["variant_attribution_confident"])
        self.assertEqual(units[1]["primary_variant_id"], "")
        self.assertFalse(units[1]["variant_attribution_confident"])
        self.assertEqual(units[2]["primary_variant_id"], "")
        self.assertTrue(units[2]["variant_attribution_confident"])

    def test_invalid_variant_share_keys_cannot_create_primary_variant(self) -> None:
        understanding = normalize_video_understanding(
            {
                "creator": {
                    "gate_observation_status": {"variant_focus": "complete"},
                    "variant_decision_rule": {},
                    "evidence_units": [
                        {
                            "id": "C1",
                            "variant_ids": ["black", "silver"],
                            "variant_visual_shares": {"gold": 0.8},
                            "variant_speech_shares": {},
                            "variant_relation_mode": "single_focus",
                            "comparison_purpose_explicit": False,
                        }
                    ],
                }
            }
        )
        unit = understanding["creator"]["evidence_units"][0]
        self.assertFalse(unit["variant_data_valid"])
        self.assertFalse(unit["variant_attribution_confident"])
        self.assertEqual(understanding["creator"]["gate_observation_status"]["variant_focus"], "unknown")

    def test_temporal_comparison_allows_one_variant_per_evidence_unit(self) -> None:
        understanding = normalize_video_understanding(
            {
                "creator": {
                    "gate_observation_status": {"variant_focus": "complete"},
                    "variant_decision_rule": {"speech_explains_choice": True},
                    "evidence_units": [
                        {
                            "id": "C1",
                            "variant_ids": ["black"],
                            "variant_visual_shares": {"black": 1.0},
                            "variant_speech_shares": {"black": 1.0},
                            "variant_relation_mode": "explicit_comparison",
                            "comparison_purpose_explicit": True,
                        },
                        {
                            "id": "C2",
                            "variant_ids": ["silver"],
                            "variant_visual_shares": {"silver": 1.0},
                            "variant_speech_shares": {"silver": 1.0},
                            "variant_relation_mode": "explicit_comparison",
                            "comparison_purpose_explicit": True,
                        },
                    ],
                }
            }
        )
        self.assertTrue(all(unit["variant_data_valid"] for unit in understanding["creator"]["evidence_units"]))
        self.assertEqual(understanding["creator"]["gate_observation_status"]["variant_focus"], "complete")

    def test_postprocess_placeholder_does_not_invalidate_gate_observation(self) -> None:
        understanding = normalize_video_understanding(
            {
                "creator": {
                    "gate_observation_status": {"variant_focus": "complete"},
                    "variant_decision_rule": {},
                    "evidence_units": [
                        {
                            "id": "C1",
                            "variant_ids": [],
                            "variant_visual_shares": {},
                            "variant_speech_shares": {},
                            "variant_relation_mode": "none",
                            "comparison_purpose_explicit": False,
                        },
                        {"id": "C_NO_CTA", "information": "结尾未识别到明确购买指令。"},
                    ],
                }
            }
        )
        self.assertFalse(understanding["creator"]["evidence_units"][1]["variant_data_valid"])
        self.assertEqual(understanding["creator"]["gate_observation_status"]["variant_focus"], "complete")

    def test_attention_scan_requires_audit_and_competitor_detail(self) -> None:
        base = {
            "creator": {
                "gate_observation_status": {"attention_scan": "complete"},
                "attention_competitors": [],
                "evidence_units": [],
            }
        }
        without_audit = normalize_video_understanding(base)
        self.assertEqual(without_audit["creator"]["gate_observation_status"]["attention_scan"], "unknown")

        base["creator"]["attention_scan_audit"] = {
            "recording_equipment_visible": True,
            "foreground_non_task_object_visible": True,
            "evidence_ids": [],
        }
        missing_detail = normalize_video_understanding(base)
        self.assertEqual(missing_detail["creator"]["gate_observation_status"]["attention_scan"], "unknown")

        base["creator"]["attention_competitors"] = [
            {
                "id": "AC1",
                "object_label": "手持录音设备",
                "persistent_motion": True,
                "high_salience": True,
                "participates_in_product_task": False,
            }
        ]
        complete = normalize_video_understanding(base)
        self.assertEqual(complete["creator"]["gate_observation_status"]["attention_scan"], "complete")

    def test_global_priority_orders_route_block_before_attention_major(self) -> None:
        result = self._global_result()
        result["product_profile"] = {
            "proof_contract_source": "operator",
            "short_video_proof_plan": {
                "valid": True,
                "s4_anchor_candidate_id": "oil_control",
                "selection_source": "operator_priority",
                "anchor_confidence": "high",
            }
        }
        creator = result["video_understanding"]["creator"]
        creator["selling_point_observations"] = [
            {
                "candidate_id": "cooling",
                "text": "冰凉感",
                "visual_share": 0.8,
                "speech_share": 0.7,
                "proof_signal_present": False,
                "evidence_ids": ["C1"],
            }
        ]
        creator["attention_competitors"] = [
            {
                "object_label": "手持麦克风",
                "time_ranges": ["0.0s - 5.0s"],
                "persistent_motion": True,
                "high_salience": True,
                "participates_in_product_task": False,
                "occludes_proof_area": False,
                "evidence_ids": ["C1"],
            }
        ]
        result["stage_analysis"][2]["creator_absolute_status"] = "weak"
        result["stage_analysis"][3]["creator_absolute_status"] = "missing"
        materialize_global_diagnosis(result, {})
        priorities = result["commercial_priorities"]
        self.assertEqual((priorities[0]["id"], priorities[0]["tier"]), ("global:selling_point_route", "P0"))
        self.assertIn(("global:attention_cleanliness", "P2"), [(item["id"], item["tier"]) for item in priorities])

        result["product_profile"]["proof_contract_source"] = "inferred"
        materialize_global_diagnosis(result, {})
        route = next(item for item in result["global_diagnosis"]["findings"] if item["id"] == "selling_point_route")
        self.assertEqual(route["impact"], "major")

    def test_operator_primary_selling_point_creates_trusted_route(self) -> None:
        foundation = {
            "product_profile": {
                "short_video_proof_plan": {
                    "valid": True,
                    "primary_candidate_id": "cooling",
                    "selection_source": "model_category_default",
                    "anchor_confidence": "low",
                    "candidates": [
                        {"id": "cooling", "selling_point": "冰凉肤感"},
                        {"id": "oil_control", "selling_point": "控油"},
                    ],
                }
            }
        }
        pipeline._stamp_proof_contract_source(
            foundation,
            {"product": {"primary_selling_point": "控油"}},
        )
        profile = foundation["product_profile"]
        self.assertEqual(profile["proof_contract_source"], "operator")
        self.assertEqual(profile["short_video_proof_plan"]["primary_candidate_id"], "oil_control")
        self.assertEqual(profile["short_video_proof_plan"]["selection_source"], "operator_priority")

    def test_focus_block_precedes_s4_large_and_adds_causal_labels(self) -> None:
        result = self._global_result()
        creator = result["video_understanding"]["creator"]
        creator["variant_decision_rule"] = {"speech_explains_choice": False, "evidence_ids": ["C1"]}
        creator["evidence_units"][0].update(
            {
                "variant_ids": ["black", "silver"],
                "variant_relation_mode": "ambiguous",
                "variant_attribution_confident": False,
                "functions": ["S4_effect"],
            }
        )
        result["stage_analysis"][3]["severity"] = "large"
        materialize_global_diagnosis(result, {})
        self.assertEqual([item["id"] for item in result["commercial_priorities"][:2]], ["global:focus_coherence", "stage:S4"])
        self.assertIn("focus_coherence", result["stage_analysis"][3]["affected_by_global_issues"])
        self.assertIn("focus_coherence", result["improvements"][0]["root_cause_ids"])

    def test_attention_gate_ignores_product_task_motion_and_respects_asymmetric_temporal_mode(self) -> None:
        result = self._global_result()
        creator = result["video_understanding"]["creator"]
        creator["attention_competitors"] = [
            {
                "object_label": "粉扑",
                "time_ranges": ["1.0s - 6.0s"],
                "persistent_motion": True,
                "high_salience": True,
                "participates_in_product_task": True,
                "occludes_proof_area": False,
                "evidence_ids": ["C1"],
            }
        ]
        result["video_understanding"]["benchmark"]["temporal_evidence_mode"] = "static_only"
        materialize_global_diagnosis(result, {})
        attention = next(item for item in result["global_diagnosis"]["findings"] if item["id"] == "attention_cleanliness")
        self.assertEqual(attention["impact"], "pass")
        self.assertEqual(attention["comparative_status"], "unknown")

        creator["attention_competitors"][0]["participates_in_product_task"] = False
        materialize_global_diagnosis(result, {})
        attention = next(item for item in result["global_diagnosis"]["findings"] if item["id"] == "attention_cleanliness")
        self.assertEqual(attention["impact"], "major")
        self.assertEqual(attention["comparative_status"], "unknown")

    def test_old_video_understanding_degrades_global_gates_to_unknown(self) -> None:
        normalized = normalize_video_understanding(
            {"creator": {"evidence_units": [{"id": "C1", "information": "旧 facts"}]}}
        )
        self.assertEqual(normalized["creator"]["temporal_evidence_mode"], "unknown")
        result = self._global_result()
        result["video_understanding"] = normalized
        materialize_global_diagnosis(result, {})
        self.assertNotEqual(result["global_diagnosis"]["overall_status"], "blocking")

    def test_report_renders_root_findings_and_uses_commercial_summary(self) -> None:
        result = self._global_result()
        result["global_diagnosis"] = {
            "temporal_capability": {"comparative": "full_temporal"},
            "findings": [
                {
                    "id": "selling_point_route",
                    "impact": "blocking",
                    "summary": "主卖点路线错误。",
                    "downstream_impact": "S2-S4 围绕错误价值展开。",
                    "suggested_action": "先改主卖点。",
                    "affected_stages": ["S2", "S3", "S4"],
                }
            ],
        }
        result["commercial_priority_summary"] = "先修正主卖点路线。"
        result["commercial_priorities"] = [
            {"tier": "P0", "title": "主卖点路线", "summary": "先修正主卖点路线。"}
        ]
        self.assertEqual(executive_summary(result), "先修正主卖点路线。")
        rendered = render_global_diagnosis(result)
        self.assertIn("根本性问题", rendered)
        self.assertIn("主卖点路线错误", rendered)
        self.assertIn("商业处理顺序", rendered)
        self.assertIn("先处理根因", render_global_cause_note(["selling_point_route"]))
        self.assertIn(
            "根因关联",
            render_improvement_meta({"root_cause_ids": ["selling_point_route"]}, "0.0s - 3.0s"),
        )

    def test_finalized_result_copies_global_commercial_fields(self) -> None:
        normalized = {
            "executive_summary": "旧摘要",
            "one_line_summary": "旧摘要",
            "one_line_verdict": "结论",
            "holistic_assessment": {},
            "key_conclusions": [],
            "comparison_contract": {},
            "comparison_eligibility": {},
            "product_visibility": {},
            "loop_closure": {},
            "video_understanding": {},
            "stage_analysis": [],
            "improvements": [],
            "global_diagnosis": {"overall_status": "major"},
            "commercial_priorities": [{"id": "global:focus_coherence"}],
            "commercial_priority_summary": "先统一产品焦点。",
        }
        analysis: dict[str, object] = {}
        pipeline.apply_finalized_analysis_result(analysis, normalized, Path("analysis_result.json"))
        self.assertEqual(analysis["global_diagnosis"], {"overall_status": "major"})
        self.assertEqual(analysis["commercial_priority_summary"], "先统一产品焦点。")

    def test_s1_srt_boundary_does_not_backproject_later_evidence_cue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            role_dir = Path(tmp)
            (role_dir / "transcript.srt").write_text(
                "1\n00:00:00,000 --> 00:00:04,000\n毛孔粗大、油皮和混油皮注意\n\n"
                "2\n00:00:04,000 --> 00:00:08,000\n下午出油又不知道怎么补妆\n\n"
                "3\n00:00:08,000 --> 00:00:10,000\n推荐用这个解决\n",
                encoding="utf-8",
            )
            result = {
                "video_understanding": {
                    "benchmark": {
                        "evidence_units": [
                            {
                                "id": "B1",
                                "time_range": "0.0s - 10.0s",
                                "information": "痛点后推荐产品。",
                                "voiceover_zh": "毛孔粗大、下午出油，推荐用这个解决。",
                                "functions": ["S1_hook"],
                            },
                            {
                                "id": "B2",
                                "time_range": "10.0s - 13.0s",
                                "information": "介绍产品名称。",
                                "functions": ["S2_intro"],
                            },
                        ]
                    }
                }
            }
            candidate = infer_s1_boundary_candidate(
                "benchmark", result, {"videos": {"benchmark": {"work_dir": str(role_dir)}}}
            )
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["seconds"], 10.0)
        self.assertEqual(candidate["source"], "evidence")

    def test_promise_chain_allows_generic_conversion_chain_when_only_s1_to_s4_are_named(self) -> None:
        result = {
            "s3_s4_relationship": {
                "creator_relationship": "process_without_effect",
                "benchmark_relationship": "process_creates_effect",
                "creator_reason": "达人展示了使用过程，但没有放大结果。",
                "benchmark_reason": "标杆将过程与效果连续展示。",
            },
            "promise_chain": {
                "s1_promise": "解决出油和毛孔问题。",
                "s2_answer": "粉饼作为解决方案出现。",
                "s3_proof_target": "按压上脸的使用过程。",
                "s4_outcome": "半脸效果对比。",
                "chain_closed": False,
                "broken_at": "S4",
                "break_reason": "效果验证不足，转化链条在 S4 断裂。",
            },
        }
        validate_chain_relationships(result, {"s3_flags_required": True, "s4_flags_required": True})

    def test_full_multimodal_analysis_is_the_cli_default(self) -> None:
        args = flayr.build_parser().parse_args(["compare"])
        self.assertTrue(args.llm_include_images)
        legacy = flayr.build_parser().parse_args(["--no-llm-include-images", "compare"])
        self.assertFalse(legacy.llm_include_images)

    def test_module_id_uses_structure_library_as_the_only_enum_source(self) -> None:
        self.assertEqual(normalize_module_id("S4-F", 4), "S4-F")
        self.assertEqual(normalize_module_id("S4-G", 4), "unknown")
        self.assertEqual(normalize_module_id("S3-A", 4), "unknown")

    def test_repair_keeps_omitted_stage_fields_and_canonicalizes_preserved_module(self) -> None:
        original = {
            "stage_analysis": [
                {
                    "stage": f"S{index}",
                    "creator_module_id": "S4-G" if index == 4 else "unknown",
                    "benchmark_summary": f"标杆 {index}",
                    "creator_summary": f"达人 {index}",
                    "gap": f"差距 {index}",
                    "creator_s4": {"effect_visible": True} if index == 4 else {},
                }
                for index in range(1, 7)
            ],
            "improvements": [{"target_stage": "S4", "title": "保留建议"}],
        }
        repaired = {
            "stage_analysis": [{"stage": f"S{index}"} for index in range(1, 7)],
            "improvements": [],
        }
        merged = preserve_valid_repair_sections(original, repaired)
        s4 = merged["stage_analysis"][3]
        self.assertEqual(s4["creator_module_id"], "unknown")
        self.assertEqual(s4["benchmark_summary"], "标杆 4")
        self.assertEqual(s4["creator_s4"], {"effect_visible": True})
        self.assertEqual(merged["improvements"], original["improvements"])

    def test_llm_stream_retries_cleanup_sensitive_artifacts_and_accept_only_completed_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload_path = root / "request.json"
            raw_path = root / "response.json"
            payload_path.write_text(json.dumps({"model": "test", "messages": []}), encoding="utf-8")
            calls: list[list[str]] = []

            def fake_run(command: list[str]) -> SimpleNamespace:
                calls.append(command)
                destination = Path(command[command.index("-o") + 1])
                if len(calls) == 1:
                    destination.write_text('data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n', encoding="utf-8")
                else:
                    destination.write_text(
                        'data: {"choices":[{"delta":{"content":"{}"},"finish_reason":"stop"}]}\n\n'
                        'data: [DONE]\n',
                        encoding="utf-8",
                    )
                return SimpleNamespace(returncode=0, stderr="", stdout="")

            with mock.patch.object(llm_api, "run_command", side_effect=fake_run), mock.patch.object(llm_api.time, "sleep"):
                raw = llm_api.call_llm_api("https://example.test/v1/chat/completions", "secret", payload_path, raw_path)

            self.assertEqual(len(calls), 2)
            self.assertIn("--speed-limit", calls[0])
            self.assertIn("--speed-time", calls[0])
            self.assertEqual(calls[0][calls[0].index("--max-time") + 1], "1800")
            self.assertIn('"finish_reason": "stop"', raw)
            self.assertTrue(raw_path.is_file())
            self.assertEqual(
                sorted(path.name for path in root.iterdir()),
                ["request.json", "response.json"],
            )

    def test_small_json_request_can_set_a_shorter_transport_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload_path = root / "request.json"
            response_path = root / "response.json"
            payload_path.write_text("{}", encoding="utf-8")
            raw = json.dumps({"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})
            args = SimpleNamespace(llm_api_url="https://example.test/v1/chat/completions")
            with mock.patch.object(pipeline, "call_llm_api", return_value=raw) as call:
                output = pipeline.fetch_json_completion(
                    args,
                    "secret",
                    payload_path,
                    response_path,
                    max_attempts=1,
                    request_max_time_seconds=240,
                )
            self.assertEqual(output, "{}")
            self.assertEqual(call.call_args.kwargs["max_time_seconds"], 240)

    def test_fetch_json_completion_retries_a_failed_complete_transport_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload_path = root / "request.json"
            response_path = root / "response.json"
            payload_path.write_text("{}", encoding="utf-8")
            raw = json.dumps({"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]})
            args = SimpleNamespace(llm_api_url="https://example.test/v1/chat/completions")
            with (
                mock.patch.object(pipeline, "call_llm_api", side_effect=[SystemExit("incomplete stream"), raw]) as call,
                mock.patch.object(pipeline.time, "sleep"),
            ):
                output = pipeline.fetch_json_completion(args, "secret", payload_path, response_path, max_attempts=2)
            self.assertEqual(output, "{}")
            self.assertEqual(call.call_count, 2)

    def test_reuse_preprocessing_reuses_existing_product_foundation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cached = {
                "category_profile": {"category": "散粉"},
                "product_profile": {"proof_contract": {"valid": True, "mode": "instant_visual"}},
            }
            (root / "product_foundation.json").write_text(json.dumps(cached), encoding="utf-8")
            args = SimpleNamespace(reuse_preprocessing=True, llm_model="test")
            analysis = {"product": {"category": "散粉"}}
            with mock.patch.object(pipeline, "fetch_json_completion") as request:
                foundation = pipeline.establish_product_foundation(args, analysis, root, "secret")
            self.assertEqual(foundation["category_profile"]["category"], "散粉")
            request.assert_not_called()

    def test_ocr_uses_short_single_request_timeout_with_outer_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frame = root / "frame.jpg"
            frame.write_bytes(b"not-a-real-jpeg")
            with mock.patch.object(subtitle_track, "call_llm_api", side_effect=SystemExit("timeout")) as call:
                lines, status = subtitle_track.ocr_frame_with_retry(
                    frame,
                    "secret",
                    "https://example.test/v1/chat/completions",
                    "vision-test",
                    root,
                    0,
                )
            self.assertEqual(lines, [])
            self.assertTrue(status.startswith("ocr_request_failed:"))
            self.assertEqual(call.call_count, 2)
            self.assertEqual(call.call_args.kwargs["max_time_seconds"], 90)
            self.assertEqual(call.call_args.kwargs["low_speed_time_seconds"], 45)
            self.assertEqual(call.call_args.kwargs["retries"], 0)

    def test_agent_plan_capabilities_use_embedded_video_audio(self) -> None:
        url = "https://ark.cn-beijing.volces.com/api/plan/v3/chat/completions"
        self.assertTrue(llm_api.is_agent_plan_api_url(url))
        self.assertFalse(llm_api.supports_standalone_audio(url))
        self.assertTrue(llm_api.supports_standalone_audio("https://example.test/v1/chat/completions"))
        self.assertEqual(full_analysis_output_budget("doubao-seed-2.0-lite"), 32768)
        self.assertEqual(full_analysis_output_budget("other-model"), 16384)

    def test_agent_plan_stage2_uses_native_clips_not_input_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "creator.mp4"
            video.write_bytes(b"placeholder")
            analysis = {
                "videos": {
                    "creator": {
                        "path": str(video),
                        "work_dir": str(root),
                        "duration_seconds": 5.0,
                    }
                }
            }
            facts = {"creator": {"evidence_units": [{"id": "C1", "time_range": "0.0s - 2.0s"}]}}
            with mock.patch.object(llm_media, "video_to_data_url", return_value="data:video/mp4;base64,AA=="):
                content = llm_media.build_evidence_sensory_inputs(
                    analysis,
                    facts,
                    api_url="https://ark.cn-beijing.volces.com/api/plan/v3/chat/completions",
                )
            types = [item.get("type") for item in content]
            self.assertIn("video_url", types)
            self.assertNotIn("input_audio", types)

    def test_agent_plan_stage2_merges_evidence_to_provider_video_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            videos: dict[str, object] = {}
            facts: dict[str, object] = {}
            for role in ("benchmark", "creator"):
                video = root / f"{role}.mp4"
                video.write_bytes(b"placeholder")
                videos[role] = {
                    "path": str(video),
                    "work_dir": str(root),
                    "duration_seconds": 12.0,
                }
                facts[role] = {
                    "evidence_units": [
                        {"id": f"{role[0].upper()}{index}", "time_range": f"{index}.0s - {index + 1}.0s"}
                        for index in range(6)
                    ]
                }
            with mock.patch.object(llm_media, "video_to_data_url", return_value="data:video/mp4;base64,AA==") as encode:
                content = llm_media.build_evidence_sensory_inputs(
                    {"videos": videos},
                    facts,
                    api_url="https://ark.cn-beijing.volces.com/api/plan/v3/chat/completions",
                )
            video_blocks = [item for item in content if item.get("type") == "video_url"]
            labels = "\n".join(str(item.get("text") or "") for item in content if item.get("type") == "text")
            self.assertEqual(len(video_blocks), 10)
            self.assertEqual(encode.call_count, 10)
            for role_prefix in ("B", "C"):
                for index in range(6):
                    self.assertIn(f"{role_prefix}{index}", labels)

    def test_agent_plan_stage1_fallback_does_not_emit_unsupported_audio(self) -> None:
        analysis = {"videos": {"creator": {"path": "", "work_dir": ""}}}
        payload = build_video_fact_payload(
            "doubao-seed-2.0-lite",
            "creator",
            analysis,
            [],
            api_url="https://ark.cn-beijing.volces.com/api/plan/v3/chat/completions",
        )
        content = payload["messages"][1]["content"]
        self.assertNotIn("input_audio", [item.get("type") for item in content])

    def test_llm_transfer_closed_error_is_retryable(self) -> None:
        self.assertTrue(
            llm_api.is_retryable_error(
                "curl: (18) transfer closed with outstanding read data remaining"
            )
        )

    def test_imported_structured_s5_result_enables_source_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "analysis_result.json"
            result_path.write_text(
                json.dumps(
                    {
                        "stage_analysis": [
                            {"stage": "S5 信任放大", "creator_s5": {}, "benchmark_s5": {}}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            analysis: dict[str, object] = {}
            captured: dict[str, object] = {}

            def fake_finalize(result: dict[str, object], incoming: dict[str, object], _: str) -> dict[str, object]:
                captured.update(incoming)
                return result

            with (
                mock.patch.object(pipeline, "finalize_analysis_result", side_effect=fake_finalize),
                mock.patch.object(pipeline, "apply_finalized_analysis_result"),
            ):
                pipeline.merge_analysis_result(analysis, result_path, "")

            self.assertIs(captured["s5_source_signals_required"], True)

    def test_fact_summary_falls_back_to_locked_multimodal_evidence(self) -> None:
        normalized = normalize_video_fact_result(
            "benchmark",
            {
                "evidence_units": [
                    {
                        "id": "B3",
                        "time_range": "3.0s - 5.0s",
                        "information": "",
                        "visual_fact": "镜头展示果汁流入杯中。",
                        "voiceover": "Mesin ini senang cuci.",
                        "voiceover_zh": "这台机器容易清洗。",
                        "subtitle_fact": "SENANG CUCI",
                        "audio_fact": "水流声。",
                    }
                ]
            },
            {"videos": {"benchmark": {}, "creator": {}}},
        )
        information = normalized["evidence_units"][0]["information"]
        self.assertIn("果汁流入杯中", information)
        self.assertIn("这台机器容易清洗", information)
        self.assertIn("SENANG CUCI", information)

    @staticmethod
    def _proposition_foundation() -> dict[str, object]:
        return {
            "category_profile": {"painpoints": ["油光", "脱妆"]},
            "product_profile": {
                "physical_task": "把面部油光变成哑光定妆",
                "hook_proposition": "油光变哑光",
                "core_selling_points": ["控油定妆"],
                "short_video_proof_plan": {
                    "candidates": [
                        {"id": "P1", "selling_point": "控油定妆", "delivery_stage": "S4"}
                    ],
                    "s4_anchor_candidate_id": "P1",
                },
                "visual_proof_points": [
                    {
                        "priority": "primary",
                        "proof_target": "油光变哑光",
                        "related_selling_points": ["控油定妆"],
                    }
                ],
                "trust_multipliers": ["持妆记录"],
            },
        }

    def test_product_proposition_contract_is_stable_and_stage_scoped(self) -> None:
        foundation = self._proposition_foundation()
        brand = {"propositions": ["出油后快速哑光"], "painpoints": ["油光"]}
        first = build_product_proposition_contract(foundation, brand)
        second = build_product_proposition_contract(foundation, brand)

        self.assertEqual(first, second)
        ids = {item["id"] for item in first["propositions"]}
        self.assertTrue({"hook.1", "pain.1", "role.1", "selling.1", "proof.1"}.issubset(ids))
        self.assertIn("proof.1", first["stages"]["S4"]["allowed_ids"])
        self.assertIn("selling.1", first["stages"]["S4"]["allowed_ids"])
        self.assertIn("selling.1", first["stages"]["S2"]["allowed_ids"])
        self.assertNotIn("trust.1", first["stages"]["S5"]["allowed_ids"])
        self.assertIn("trust.1", first["stages"]["S5"]["trust_evidence_ids"])

    def test_product_proposition_contract_reaches_comparison_and_repair(self) -> None:
        analysis = {
            "product_foundation": self._proposition_foundation(),
            "brand_proposition": {"propositions": ["出油后快速哑光"], "painpoints": ["油光"]},
            "videos": {},
        }
        comparison = build_llm_comparison_payload("test", "input", {}, analysis)
        comparison_content = comparison["messages"][1]["content"]
        comparison_text = comparison_content[0]["text"] if isinstance(comparison_content, list) else comparison_content
        repair = build_llm_repair_payload("test", "{}", "error", "input", analysis=analysis)
        repair_text = repair["messages"][1]["content"]

        self.assertIn("本品命题引用合同", comparison_text)
        self.assertIn('"hook.1"', comparison_text)
        self.assertIn("本品命题引用合同", repair_text)
        self.assertIn('"proof.1"', repair_text)
        self.assertIn("具体未解问题", comparison_text)
        self.assertIn("答案可在 S2 承接", comparison_text)

    def test_brand_proposition_resolves_validation_run_names(self) -> None:
        self.assertEqual(resolve_brand_key("validation-are_xie"), "are_xie")
        self.assertEqual(resolve_brand_key("scope-probe-carslan-b0"), "carslan")
        self.assertEqual(resolve_brand_key("sample-youkoubo-c2"), "juicer")
        brand = load_brand_proposition(Path("/tmp/validation-are_xie"))
        self.assertIsNotNone(brand)
        self.assertIn("经期腹痛", brand["painpoints"])

    def test_explicit_proposition_key_does_not_depend_on_run_directory(self) -> None:
        online_run = Path("/tmp/tenant-42/run-019f1e50")
        brand = load_brand_proposition(online_run, "are_xie")
        self.assertIsNotNone(brand)
        self.assertIn("经期腹痛", brand["painpoints"])
        self.assertIsNone(load_brand_proposition(online_run))

    def test_product_skus_under_one_brand_keep_distinct_proposition_keys(self) -> None:
        lip = load_brand_proposition(Path("/tmp/tenant-42/run-019f1e50"), "colorkey_lip_mud")
        mask = load_brand_proposition(Path("/tmp/tenant-42/run-019f1e50"), "colorkey")
        self.assertIsNotNone(lip)
        self.assertIsNotNone(mask)
        self.assertIn("丝绒奶油哑光妆效", lip["propositions"])
        self.assertNotIn("敷后水润通透", lip["propositions"])
        self.assertIn("敷后水润通透", mask["propositions"])

    def test_new_validation_products_bind_explicit_proposition_keys(self) -> None:
        manifest = json.loads((ROOT / "references" / "validation-inputs.json").read_text(encoding="utf-8"))
        sample_items = manifest.get("samples", []) if isinstance(manifest, dict) else manifest
        samples = {item["id"]: item for item in sample_items if isinstance(item, dict) and item.get("id")}
        expected_keys = {
            "colorblu-c0": "colorblu_waterproof_sealant",
            "colorblu-c1": "colorblu_waterproof_sealant",
            "carslan-powder-c0": "carslan",
            "carslan-powder-c1": "carslan",
        }
        for sample_id, proposition_key in expected_keys.items():
            self.assertEqual(samples[sample_id].get("proposition_key"), proposition_key)
            self.assertIsNotNone(load_brand_proposition(Path("/tmp/online-run"), proposition_key))

    def test_invalid_new_proof_contract_cannot_fallback_to_legacy_visual_claim(self) -> None:
        foundation = self._proposition_foundation()
        profile = foundation["product_profile"]
        profile["visual_proof_points"] = []
        profile["core_visual_proposition"] = "旧字段视觉结果"
        profile["proof_contract"] = {"valid": False, "validation_reason": "invalid"}
        contract = build_product_proposition_contract(foundation)
        self.assertFalse(any(item["kind"] == "proof" for item in contract["propositions"]))

        stages = [{"stage": f"S{index}"} for index in range(1, 7)]
        for role in ("creator", "benchmark"):
            stages[3][f"{role}_s4"] = {
                "effect_visible": False if role == "benchmark" else True,
                "effect_salience": "strong",
                "effect_proposition_matched": True,
                "effect_attribution_supported": True,
            }
            stages[5][f"{role}_s6"] = {"module_type": "D", "depends_on_valid_s4": True}
        result = {
            "category_profile": foundation["category_profile"],
            "product_profile": profile,
            "stage_analysis": stages,
        }
        materialize_cross_stage_inputs(result, {})
        self.assertFalse(result["cross_stage_state"]["roles"]["creator"]["s4_output_available"])
        self.assertFalse(stages[5]["creator_s6"]["computed_depends_on_valid_s4"])

    def test_proof_contract_allows_natural_outcome_but_rejects_compound_signal(self) -> None:
        base = {
            "anchor_candidate_id": "P1",
            "mode": "instant_visual",
            "consumer_outcome": "原有状态被覆盖并呈现目标效果",
            "signal_type": "state_change",
            "observable_signal": "目标区域色彩从暗淡变为饱和",
            "before_state": "未覆盖",
            "after_state": "均匀覆盖",
            "proof_condition": "同一光线近景拍摄",
        }
        self.assertTrue(normalize_proof_contract(base)["valid"])

        compound = {**base, "observable_dimension": "色彩覆盖度与纹理平滑度"}
        normalized = normalize_proof_contract(compound)
        self.assertFalse(normalized["valid"])
        self.assertIn("一个可观察维度", normalized["validation_reason"])

        same_object_state = {**base, "observable_dimension": "刷头完整性与存在状态"}
        normalized = normalize_proof_contract(same_object_state)
        self.assertTrue(normalized["valid"])
        self.assertEqual(normalized["observable_dimension"], "刷头状态")

    def test_inferred_s4_contract_cannot_authoritatively_override_stage_analysis(self) -> None:
        profile = normalize_product_profile(
            {
                "proof_contract_source": "inferred",
                "proof_contract": {"valid": True, "mode": "instant_visual"},
            }
        )
        self.assertEqual(profile["proof_contract_source"], "inferred")
        reason = _visual_verifier_skip_reason({"product_profile": profile})
        self.assertIn("模型推断", reason)

    def test_operator_selling_points_do_not_upgrade_model_selected_proof_contract(self) -> None:
        """产品卖点来自运营，不等于运营确认了唯一 S4 视觉合同。"""
        foundation = {
            "product_profile": normalize_product_profile(
                {
                    "proof_contract_source": "curated",
                    "proof_contract": {"valid": True, "mode": "instant_visual"},
                }
            )
        }
        pipeline._stamp_proof_contract_source(
            foundation,
            {"product": {"core_selling_points": "显色、柔雾、持妆"}},
        )
        self.assertEqual(foundation["product_profile"]["proof_contract_source"], "inferred")

    def test_derived_execution_is_written_to_severity_trace(self) -> None:
        from flayr_core.postprocess.derive import derive_severity_from_facts

        stage = {
            "stage": "S2 产品引出",
            "severity": "small",
            "creator_execution": 0.5,
            "benchmark_execution": 1.0,
            "creator_s2": {"exists": True, "handoff_met": True, "s1_s2_compatible": True,
                           "product_identity_clear": True, "product_role_clear": False},
            "benchmark_s2": {"exists": True, "handoff_met": True, "s1_s2_compatible": True,
                              "product_identity_clear": True, "product_role_clear": True},
        }
        result = {"stage_analysis": [stage]}
        derive_severity_from_facts(result)
        trace = stage["severity_derivation"]
        self.assertEqual(trace["derived_creator_execution"], 1.0)
        self.assertEqual(trace["derived_benchmark_execution"], 2.0)

    def test_video_identity_and_comparison_scope_survive_normalization(self) -> None:
        facts = normalize_video_fact_result(
            "benchmark",
            {
                "product_identity": {
                    "brand_or_product_name": "Simplus",
                    "product_category": "榨汁机",
                    "form_factor": "慢速榨汁机",
                    "identity_basis": "visible",
                    "confidence": "high",
                },
                "evidence_units": [{"id": "B1", "time_range": "0.0s - 1.0s", "information": "展示产品"}],
            },
            {"videos": {"benchmark": {}, "creator": {}}},
        )
        self.assertEqual(facts["product_identity"]["form_factor"], "慢速榨汁机")

        normalized = normalize_analysis_result(
            {
                "comparison_eligibility": {
                    "scope": "cross_product",
                    "direct_product_stages": ["S1", "S3", "bad"],
                    "reason": "产品形态不同",
                },
                "stage_analysis": [{"stage": f"S{index}"} for index in range(1, 7)],
                "improvements": [{"title": "测试建议", "time_range": "0.0s - 1.0s"}],
            }
        )
        self.assertEqual(normalized["comparison_eligibility"]["scope"], "cross_product")
        self.assertEqual(normalized["comparison_eligibility"]["direct_product_stages"], [])
        self.assertEqual(normalized["comparison_contract"]["overall_status"], "not_comparable")

    def test_same_product_family_forces_all_stages_direct(self) -> None:
        contract = normalize_comparison_contract(
            {
                "identity_relation": "same_product_family",
                "substitution_relation": "uncertain",
                "stage_eligibility": {"S3": {"status": "not_comparable"}},
                "reason": "同系列粉饼，仅黑色与银色包装不同。",
            }
        )
        self.assertEqual(contract["overall_status"], "full_direct")
        self.assertEqual(contract["comparable_stages"], ["S1", "S2", "S3", "S4", "S5", "S6"])
        self.assertTrue(all(item["status"] == "direct" for item in contract["stage_eligibility"].values()))

    def test_strong_substitute_requires_all_shared_job_gates(self) -> None:
        contract = normalize_comparison_contract(
            {
                "identity_relation": "different_product",
                "substitution_relation": "strong_substitute",
                "shared_job": {
                    "same_consumer_job": True,
                    "same_target_object": True,
                    "same_desired_outcome": True,
                    "same_purchase_decision": False,
                    "complement_or_dependency": False,
                },
                "stage_eligibility": {"S3": {"status": "structural"}},
            }
        )
        self.assertEqual(contract["substitution_relation"], "partial_substitute")
        self.assertEqual(contract["overall_status"], "selective_structural")

    def test_unrelated_products_have_no_comparable_stages(self) -> None:
        contract = normalize_comparison_contract(
            {
                "identity_relation": "different_product",
                "substitution_relation": "none",
                "stage_eligibility": {"S1": {"status": "structural"}, "S6": {"status": "structural"}},
                "reason": "防水胶与粉饼不共享任务。",
            }
        )
        self.assertEqual(contract["overall_status"], "not_comparable")
        self.assertEqual(contract["comparable_stages"], [])
        self.assertTrue(all(item["status"] == "not_comparable" for item in contract["stage_eligibility"].values()))

    def test_non_comparable_gate_stops_before_stage_analysis(self) -> None:
        contract = normalize_comparison_contract(
            {"identity_relation": "different_product", "substitution_relation": "none", "reason": "任务不同。"}
        )
        analysis = {"stage_analysis": [{"stage": "S1"}], "improvements": [{"target_stage": "S1"}]}
        with tempfile.TemporaryDirectory() as tmp:
            pipeline._apply_non_comparable_result(analysis, {"benchmark": {}, "creator": {}}, contract, Path(tmp))
            self.assertTrue((Path(tmp) / "comparison_rejection.json").is_file())
        self.assertEqual(analysis["analysis_status"], "not_comparable")
        self.assertEqual(analysis["stage_analysis"], [])
        self.assertEqual(analysis["improvements"], [])

    def test_stage1_event_checks_keep_only_catalog_events_and_real_evidence(self) -> None:
        facts = normalize_video_fact_result(
            "creator",
            {
                "evidence_units": [
                    {"id": "C1", "time_range": "0.0s - 2.0s", "information": "真实涂抹动作"},
                ],
                "structure_event_checks": [
                    {"module_id": "S3-A", "present": True, "evidence_ids": ["C1", "fake"]},
                    {"module_id": "S4-Z", "present": True, "evidence_ids": ["C1"]},
                ],
            },
            {"videos": {"benchmark": {}, "creator": {}}},
        )
        self.assertEqual(facts["structure_event_checks"][0], {"module_id": "S3-A", "present": True, "evidence_ids": ["C1"]})
        self.assertEqual(len(facts["structure_event_checks"]), 11)
        self.assertNotIn("S4-Z", [item["module_id"] for item in facts["structure_event_checks"]])

    def test_final_video_understanding_preserves_locked_stage1_audit_fields(self) -> None:
        normalized = normalize_analysis_result(
            {
                "video_understanding": {
                    "benchmark": {
                        "evidence_units": [{"id": "B1", "time_range": "0.0s - 2.0s", "information": "涂抹裂缝"}],
                        "evidence_checklist": [
                            {"item": "proposition: 裂缝修补", "covered": True, "evidence_ids": ["B1", "missing"], "channels": ["visual"]},
                        ],
                        "structure_event_checks": [
                            {"module_id": "S3-A", "present": True, "evidence_ids": ["B1", "missing"]},
                        ],
                    },
                    "creator": {"evidence_units": []},
                },
                "stage_analysis": [{"stage": f"S{index}"} for index in range(1, 7)],
                "improvements": [{"title": "测试建议", "time_range": "0.0s - 1.0s"}],
            }
        )
        benchmark = normalized["video_understanding"]["benchmark"]
        self.assertEqual(benchmark["evidence_checklist"][0]["evidence_ids"], ["B1"])
        self.assertEqual(benchmark["structure_event_checks"][0], {"module_id": "S3-A", "present": True, "evidence_ids": ["B1"]})
        self.assertEqual(len(benchmark["structure_event_checks"]), 11)

    def test_locked_comparison_scope_reaches_and_overrides_main_analysis(self) -> None:
        facts = {
            "benchmark": {
                "product_identity": {"product_category": "挂烫机", "form_factor": "手持挂烫机"},
                "evidence_units": [{"id": "B1", "time_range": "0-2s", "information": "展示熨烫过程"}],
            },
            "creator": {"product_identity": {"product_category": "地面清洁机", "form_factor": "吸尘清洁机"}},
        }
        scope_payload = build_comparison_eligibility_payload("test", facts)
        scope_text = scope_payload["messages"][1]["content"][0]["text"]
        self.assertIn("手持挂烫机", scope_text)
        self.assertIn("包装、颜色、色号", scope_text)
        self.assertIn("同一次购买决策可二选一", scope_text)
        self.assertIn("S5 需共享信任问题", scope_text)
        self.assertIn("展示熨烫过程", scope_text)

        stages = [{"stage": f"S{index}"} for index in range(1, 7)]
        result = {"stage_analysis": stages, "comparison_eligibility": {"scope": "same_product"}}
        analysis = {"comparison_eligibility": {"scope": "cross_product", "direct_product_stages": ["S1"], "reason": "形态不同"}}
        stamp_comparison_eligibility(result, analysis)
        self.assertEqual(result["comparison_eligibility"]["scope"], "cross_product")

    def test_scope_identity_payload_excludes_video_and_audio(self) -> None:
        analysis = {
            "product": {"name": "测试产品"},
            "videos": {"creator": {"work_dir": "", "duration_seconds": 10}},
        }
        payload = build_video_identity_payload(
            "test",
            "creator",
            analysis,
            [{"label": "creator frame", "data_url": "data:image/jpeg;base64,AA=="}],
        )
        content = payload["messages"][1]["content"]
        types = [item.get("type") for item in content if isinstance(item, dict)]
        self.assertEqual(payload["max_tokens"], 1024)
        self.assertNotIn("video_url", types)
        self.assertNotIn("input_audio", types)
        self.assertIn("image_url", types)

    def test_unrelated_products_exclude_all_stages_and_improvements(self) -> None:
        stages = [{"stage": f"S{index}", "severity": "large"} for index in range(1, 7)]
        result = {
            "comparison_eligibility": {
                "scope": "cross_product",
                "direct_product_stages": ["S1", "S6"],
                "reason": "产品关键形态不同",
            },
            "stage_analysis": stages,
            "improvements": [
                {"target_stage": "S3", "title": "使用过程建议", "priority": 1},
                {"target_stage": "S6", "title": "CTA 建议", "priority": 2},
            ],
        }
        apply_comparison_eligibility(result)
        stabilize_improvement_priorities(result)

        self.assertEqual(stages[0]["comparison_status"], "not_directly_comparable")
        self.assertEqual(stages[1]["comparison_status"], "not_directly_comparable")
        self.assertEqual(stages[4]["comparison_status"], "not_directly_comparable")
        self.assertEqual(result["improvements"], [])
        self.assertEqual(result["comparison_contract"]["overall_status"], "not_comparable")
        skipped, reason = stage_skipped(stages[2])
        self.assertTrue(skipped)
        self.assertIn("不输出差距判断", reason)

    def test_strong_substitute_uses_stage_level_contract(self) -> None:
        eligibility = normalize_comparison_eligibility(
            {
                "identity_relation": "different_product",
                "substitution_relation": "strong_substitute",
                "shared_job": {
                    "same_consumer_job": True,
                    "same_target_object": True,
                    "same_desired_outcome": True,
                    "same_purchase_decision": True,
                    "complement_or_dependency": False,
                },
                "stage_eligibility": {
                    **{stage: {"status": "structural", "basis": "共同任务"} for stage in ("S1", "S2", "S3", "S4", "S6")},
                    "S5": {"status": "not_applicable", "basis": "双方均无背书"},
                },
                "reason": "运营确认同类同任务。",
                "scope_origin": "operator_certified",
                "facts_scope": "cross_product",
                "facts_reason": "关键形态不同。",
            }
        )
        self.assertEqual(eligibility["direct_product_stages"], ["S1", "S2", "S3", "S4", "S6"])
        self.assertEqual(eligibility["scope_origin"], "operator_certified")
        self.assertEqual(eligibility["facts_scope"], "cross_product")

        stages = [{"stage": f"S{index}", "severity": "large"} for index in range(1, 7)]
        result = {
            "comparison_eligibility": eligibility,
            "stage_analysis": stages,
            "improvements": [
                {"target_stage": "S3", "title": "使用过程建议", "priority": 1},
                {"target_stage": "S5", "title": "背书建议", "priority": 2},
            ],
        }
        apply_comparison_eligibility(result)
        stabilize_improvement_priorities(result)
        self.assertEqual(stages[2]["comparison_basis"], "structure_execution")
        self.assertEqual(stages[4]["comparison_status"], "not_applicable")
        self.assertEqual([item["target_stage"] for item in result["improvements"]], ["S3"])
        self.assertIn("仅在共同消费者任务下比较内容执行", result["comparison_scope_note"])

    def test_same_task_structure_override_preserves_facts_audit(self) -> None:
        eligibility = pipeline._apply_operator_scope_override(
            {"scope": "cross_product", "direct_product_stages": ["S1"], "reason": "关键形态不同。"},
            "same_task_structure",
        )
        self.assertEqual(eligibility["scope"], "same_task_structure")
        self.assertEqual(eligibility["direct_product_stages"], ["S1", "S2", "S3", "S4", "S6"])
        self.assertEqual(eligibility["scope_origin"], "operator_certified")
        self.assertEqual(eligibility["facts_scope"], "cross_product")

    def test_same_task_structure_override_makes_stage_contract_consistent(self) -> None:
        eligibility = pipeline._apply_operator_scope_override(
            {
                "identity_relation": "different_product",
                "substitution_relation": "none",
                "shared_job": {
                    "same_consumer_job": False,
                    "same_target_object": False,
                    "same_desired_outcome": False,
                    "same_purchase_decision": False,
                    "complement_or_dependency": False,
                },
                "stage_eligibility": {
                    **{
                        stage: {"status": "not_comparable", "basis": "产品身份不同"}
                        for stage in ("S1", "S2", "S3", "S4", "S6")
                    },
                    "S5": {"status": "not_applicable", "basis": "双方均无背书"},
                },
                "reason": "产品身份不同。",
            },
            "same_task_structure",
        )
        for stage in ("S1", "S2", "S3", "S4", "S6"):
            self.assertEqual(eligibility["stage_eligibility"][stage]["status"], "structural")
        self.assertEqual(eligibility["stage_eligibility"]["S5"]["status"], "not_applicable")
        self.assertTrue(eligibility["shared_job"]["same_consumer_job"])
        self.assertIn("运营确认", eligibility["shared_job"]["reason"])

    def test_facts_eligibility_keeps_scope_and_audit_fields_consistent(self) -> None:
        eligibility = pipeline._stamp_facts_eligibility(
            {
                "scope": "same_product",
                "direct_product_stages": ["S1", "S2", "S3", "S4", "S5", "S6"],
                "reason": "品牌、品类及关键形态一致。",
                "scope_origin": "operator_certified",
                "facts_scope": "uncertain",
            }
        )
        self.assertEqual(eligibility["scope_origin"], "facts")
        self.assertEqual(eligibility["facts_scope"], "same_product")
        self.assertEqual(eligibility["facts_reason"], "品牌、品类及关键形态一致。")

    def test_improvement_placeholder_is_not_reportable(self) -> None:
        result = {
            "stage_analysis": [{"stage": f"S{index}", "severity": "medium"} for index in range(1, 7)],
            "improvements": [
                {"target_stage": "S1", "title": "有效建议", "problem": "明确问题", "priority": 2},
                {"target_stage": "", "title": "（LLM 未填写 title，需人工补充）", "problem": "（LLM 未填写 problem，需人工补充）", "priority": 1},
            ],
        }
        stabilize_improvement_priorities(result)
        self.assertEqual([item["title"] for item in result["improvements"]], ["有效建议"])

    def test_report_summary_does_not_render_legacy_cross_product_claim(self) -> None:
        analysis = {
            "comparison_eligibility": {
                "scope": "cross_product",
                "direct_product_stages": ["S1", "S6"],
                "reason": "产品形态不同",
            },
            "one_line_summary": "达人效果弱于标杆。",
        }
        summary = executive_summary(analysis)
        self.assertIn("不输出 S1-S6 差距结论", summary)
        self.assertNotIn("达人效果弱于标杆", summary)

    def test_report_summary_marks_same_task_structure_limit_without_dropping_conclusion(self) -> None:
        analysis = {
            "comparison_eligibility": {
                "scope": "same_task_structure",
                "direct_product_stages": ["S1", "S2", "S3", "S4", "S6"],
                "reason": "运营确认同类同任务。",
            },
            "one_line_summary": "达人缺少完整的使用与效果证据。",
        }
        summary = executive_summary(analysis)
        self.assertIn("存在强替代关系", summary)
        self.assertIn("达人缺少完整的使用与效果证据", summary)

    def test_child_toothpaste_scope_ignores_prompt_examples(self) -> None:
        generic_input = """## 产品信息

- 产品名：MMX吸尘清洗机
- 品类：家电清洁

## 通用规则

儿童牙膏和 toothpaste 只是在这里作为示例出现。
"""
        child_input = """## 产品信息

- 产品名：儿童牙膏
- 品类：toothpaste

## 通用规则

无关示例。
"""
        self.assertFalse(is_child_toothpaste_context(generic_input))
        self.assertTrue(is_child_toothpaste_context(child_input))

    def test_s6_module_mismatch_is_not_counted_as_standard_delivery(self) -> None:
        stages = [{"stage": f"S{index}"} for index in range(1, 7)]
        stages[5].update(
            {
                "creator_s6": {
                    "exists": True,
                    "direct_order_met": True,
                    "ending_position_met": True,
                    "module_fit_met": False,
                },
                "benchmark_s6": {
                    "exists": True,
                    "direct_order_met": True,
                    "ending_position_met": True,
                    "module_fit_met": True,
                },
            }
        )
        result = {"stage_analysis": stages}
        materialize_quality_audits(result, {})
        self.assertEqual(stages[5]["creator_absolute_status"], "weak")
        self.assertEqual(stages[5]["computed_stage_standard_delivery"], "benchmark_only")

    def test_structured_flags_replace_stale_model_stage_delivery(self) -> None:
        stages = [{"stage": f"S{index}"} for index in range(1, 7)]
        stages[5].update(
            {
                "stage_standard_delivery": "benchmark_only",
                "creator_s6": {"exists": False},
                "benchmark_s6": {"exists": False},
            }
        )
        result = {"stage_analysis": stages}

        materialize_quality_audits(result, {})

        self.assertEqual(stages[5]["stage_standard_delivery"], "none")
        self.assertEqual(stages[5]["model_stage_standard_delivery"], "benchmark_only")
        self.assertFalse(any("[Q20] S6" in warning for warning in result.get("qa_warnings", [])))

    def test_s6_clean_path_does_not_require_offer_or_urgency_for_full_execution(self) -> None:
        strong = {
            "exists": True,
            "ending_position_met": True,
            "direct_order_met": True,
            "action_path_clear": True,
            "offer_or_incentive_clear": False,
            "urgency_met": False,
            "product_value_recalled": True,
            "module_fit_met": True,
            "compliance_risk": False,
            "module_type": "A",
        }
        weak = {
            **strong,
            "action_path_clear": False,
            "module_fit_met": False,
            "urgency_met": True,
        }
        scores = _s6_cta_exec({"creator_s6": weak, "benchmark_s6": strong})
        self.assertEqual(scores, {"creator_exec": 0.5, "bench_exec": 2.0})

    def test_s6_soft_invitation_with_offer_is_not_absent_cta(self) -> None:
        complete = {
            "exists": True,
            "ending_position_met": True,
            "direct_order_met": True,
            "action_path_clear": True,
            "soft_purchase_invitation_met": False,
            "offer_or_incentive_clear": True,
            "urgency_met": False,
            "product_value_recalled": True,
            "module_fit_met": True,
            "compliance_risk": False,
            "module_type": "B",
        }
        soft = {
            **complete,
            "direct_order_met": False,
            "action_path_clear": False,
            "soft_purchase_invitation_met": True,
        }
        self.assertEqual(
            _s6_cta_exec({"creator_s6": soft, "benchmark_s6": complete}),
            {"creator_exec": 1.5, "bench_exec": 2.0},
        )

    def test_s6_effect_summary_dependency_does_not_downgrade_completed_purchase_action(self) -> None:
        complete_cta = {
            "exists": True,
            "ending_position_met": True,
            "direct_order_met": True,
            "action_path_clear": True,
            "offer_or_incentive_clear": True,
            "urgency_met": False,
            "product_value_recalled": True,
            "module_fit_met": True,
            "compliance_risk": False,
            "module_type": "D",
            "computed_depends_on_valid_s4": False,
        }
        self.assertEqual(
            _s6_cta_exec({"creator_s6": complete_cta, "benchmark_s6": complete_cta}),
            {"creator_exec": 2.0, "bench_exec": 2.0},
        )

    def test_s3_contact_application_change_and_continuity_are_real_usage_hard_gates(self) -> None:
        complete = {
            "exists": True,
            "usage_process_visible": True,
            "result_only_without_process": False,
            "mouth_only_or_static": False,
            "real_usage_met": True,
            "core_selling_point_visible": True,
            "process_framing_met": True,
            "action_proof_met": True,
            "action_target_contact_met": True,
            "action_application_change_visible": True,
            "critical_action_continuity_met": True,
            "usage_context_fit": True,
            "continuity_met": True,
            "richness_met": False,
            "single_scene_continuity_met": True,
            "single_scene_variation_met": False,
            "multi_scene_logic_met": False,
            "multi_scene_transition_met": False,
            "multi_scene_role_adaptation_met": False,
            "role_design_met": False,
            "role_interaction_met": False,
            "missing_selling_points": [],
            "scene_mode": "single_scene",
            "fake_or_staged": False,
        }
        no_contact = {**complete, "action_target_contact_met": False}
        no_application_change = {**complete, "action_application_change_visible": False}
        jump_to_result = {**complete, "critical_action_continuity_met": False}
        self.assertEqual(
            _s3_usage_exec({"creator_s3": no_contact, "benchmark_s3": complete}),
            {"creator_exec": 0.0, "bench_exec": 1.0},
        )
        self.assertEqual(
            _s3_usage_exec({"creator_s3": no_application_change, "benchmark_s3": complete}),
            {"creator_exec": 0.0, "bench_exec": 1.0},
        )
        self.assertEqual(
            _s3_usage_exec({"creator_s3": jump_to_result, "benchmark_s3": complete}),
            {"creator_exec": 0.0, "bench_exec": 1.0},
        )

    def test_s3_visual_verifier_uses_stage_video_when_s4_contract_cannot_override(self) -> None:
        stages = [{"stage": f"S{index}"} for index in range(1, 7)]
        stages[2].update(
            {
                "creator_time_range": "2.0s - 6.0s",
                "benchmark_time_range": "1.0s - 5.0s",
                "creator_s3": {"evidence_ids": ["C1"]},
                "benchmark_s3": {"evidence_ids": ["B1"]},
            }
        )
        stages[3].update(
            {
                "creator_s4": {"evidence_ids": ["C2"]},
                "benchmark_s4": {"evidence_ids": ["B2"]},
            }
        )
        result = {
            "stage_analysis": stages,
            "video_understanding": {
                "creator": {"evidence_units": [{"id": "C1", "time_range": "2.0s - 6.0s"}]},
                "benchmark": {"evidence_units": [{"id": "B1", "time_range": "1.0s - 5.0s"}]},
            },
            "product_profile": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            creator = Path(tmp) / "creator.mp4"
            benchmark = Path(tmp) / "benchmark.mp4"
            creator.touch()
            benchmark.touch()
            analysis = {
                "videos": {
                    "creator": {"path": str(creator), "duration_seconds": 10.0, "frames": []},
                    "benchmark": {"path": str(benchmark), "duration_seconds": 10.0, "frames": []},
                }
            }
            with mock.patch(
                "flayr_core.llm.s4_visual_verifier.video_to_data_url",
                return_value="data:video/mp4;base64,AAAA",
            ):
                payload = build_s4_visual_verifier_payload("test", result, analysis, review_s4=False)
        self.assertIsNotNone(payload)
        content = payload["messages"][1]["content"]
        self.assertEqual(sum(item.get("type") == "video_url" for item in content), 2)
        text = "\n".join(str(item.get("text") or "") for item in content if item.get("type") == "text")
        self.assertIn("S3 原片短片", text)
        self.assertIn("两侧 s4 必须填 null", text)
        self.assertNotIn("S4 原片短片", text)

    def test_s3_s4_coherence_does_not_allow_result_to_backfill_missing_process(self) -> None:
        stages = [{"stage": f"S{index}"} for index in range(1, 7)]
        stages[2].update(
            {
                "creator_s3": {
                    "usage_process_visible": True,
                    "real_usage_met": True,
                    "core_selling_point_visible": True,
                    "action_proof_met": True,
                    "action_target_contact_met": False,
                    "action_application_change_visible": False,
                    "critical_action_continuity_met": False,
                    "usage_reason": "空中比划后跳到完成态",
                },
                "benchmark_s3": {
                    "usage_process_visible": True,
                    "real_usage_met": True,
                    "core_selling_point_visible": True,
                    "action_proof_met": True,
                    "action_target_contact_met": True,
                    "action_application_change_visible": True,
                    "critical_action_continuity_met": True,
                    "usage_reason": "材料贴到裂缝后立即按压",
                },
            }
        )
        stages[3].update(
            {
                "creator_s4": {
                    "effect_visible": True,
                    "result_only_without_process": False,
                    "process_linked_effect": True,
                    "effect_reason": "盆底未漏水",
                },
                "benchmark_s4": {
                    "effect_visible": True,
                    "result_only_without_process": False,
                    "process_linked_effect": True,
                    "effect_reason": "修补后承重",
                },
            }
        )
        result = {"stage_analysis": stages}
        reconcile_s3_s4_evidence_coherence(result)
        creator_s3 = stages[2]["creator_s3"]
        creator_s4 = stages[3]["creator_s4"]
        self.assertFalse(creator_s3["usage_process_visible"])
        self.assertFalse(creator_s3["real_usage_met"])
        self.assertFalse(creator_s3["action_proof_met"])
        self.assertFalse(stages[2]["creator_has_usage_demo"])
        self.assertTrue(stages[2]["benchmark_has_usage_demo"])
        self.assertTrue(creator_s4["result_only_without_process"])
        self.assertFalse(creator_s4["process_linked_effect"])
        self.assertTrue(stages[3]["benchmark_s4"]["process_linked_effect"])

    def test_s4_strong_benchmark_vs_result_only_creator_is_large(self) -> None:
        creator = {
            "effect_visible": True,
            "visual_difference_observed": True,
            "module_constraints_met": True,
            "effect_salience": "strong",
            "effect_attribution_supported": True,
            "requires_close_inspection": False,
            "tamper_or_cut_risk": False,
            "result_only_without_process": True,
            "process_linked_effect": False,
            "effect_proposition_matched": True,
        }
        benchmark = {
            **creator,
            "result_only_without_process": False,
            "process_linked_effect": True,
        }
        trace = _derive_one(
            "S4",
            {"creator_s4": creator, "benchmark_s4": benchmark},
            {"S4": 1.0},
            [],
        )
        self.assertEqual(trace["severity"], "large")
        self.assertIn("效果归因断层", trace["reason"])

    def test_structural_scope_s4_visual_review_does_not_require_same_sku_contract(self) -> None:
        result = {
            "comparison_contract": {
                "identity_relation": "different_product",
                "substitution_relation": "strong_substitute",
                "shared_job": {
                    "same_consumer_job": True,
                    "same_target_object": True,
                    "same_desired_outcome": True,
                    "same_purchase_decision": True,
                    "complement_or_dependency": False,
                },
                "stage_eligibility": {"S4": {"status": "structural"}},
            },
            "product_profile": {
                "proof_contract_source": "inferred",
                "proof_contract": {"valid": False, "mode": "trust_substituted"},
            },
        }
        self.assertEqual(_visual_verifier_skip_reason(result), "")

    def test_structural_scope_visual_rule_does_not_leak_product_contract(self) -> None:
        rule = _visual_verifier_scope_rule(
            {"proof_contract": {"anchor": "waterproof"}, "short_video_proof_plan": {"primary": "waterproof"}},
            True,
        )
        self.assertNotIn("waterproof", rule)
        self.assertIn("不要求两侧证明相同的具体功效", rule)

    def test_quantified_test_effect_can_be_complete_without_ab_comparison(self) -> None:
        quantified_test = {
            "effect_visible": True,
            "effect_salience": "strong",
            "effect_proposition_matched": True,
            "effect_attribution_supported": True,
            "process_linked_effect": True,
            "comparison_control_met": False,
            "closeup_or_focus_met": True,
            "visual_difference_observed": True,
            "module_constraints_met": True,
            "effect_maximized": True,
            "requires_close_inspection": False,
            "tamper_or_cut_risk": False,
            "effect_type": "quantified_test",
        }
        self.assertEqual(
            _s4_effect_exec({"creator_s4": quantified_test, "benchmark_s4": quantified_test}),
            {"creator_exec": 2.0, "bench_exec": 2.0},
        )

    def test_s3_and_s4_complete_proof_vs_explicit_absence_are_large_gaps(self) -> None:
        complete_s3 = {
            "usage_process_visible": True,
            "real_usage_met": True,
            "core_selling_point_visible": True,
            "action_proof_met": True,
            "action_target_contact_met": True,
            "action_application_change_visible": True,
            "critical_action_continuity_met": True,
        }
        missing_s3 = {**complete_s3, "usage_process_visible": False, "real_usage_met": False,
                      "action_target_contact_met": False, "action_application_change_visible": False,
                      "critical_action_continuity_met": False}
        s3_trace = _derive_one(
            "S3",
            {"creator_execution": 0.0, "benchmark_execution": 1.0,
             "creator_s3": missing_s3, "benchmark_s3": complete_s3},
            {"S3": 1.0},
            [],
        )
        self.assertEqual(s3_trace["severity"], "large")
        self.assertEqual(s3_trace["E"], 2)
        self.assertIn("使用过程完整性断层", s3_trace["reason"])
        complete_s4 = {
            "effect_visible": True,
            "visual_difference_observed": True,
            "module_constraints_met": True,
            "effect_salience": "strong",
            "effect_attribution_supported": True,
            "requires_close_inspection": False,
            "tamper_or_cut_risk": False,
        }
        missing_s4 = {**complete_s4, "effect_visible": False, "visual_difference_observed": False,
                      "effect_salience": "none"}
        s4_trace = _derive_one(
            "S4",
            {"creator_execution": 0.0, "benchmark_execution": 2.0,
             "creator_s4": missing_s4, "benchmark_s4": complete_s4},
            {"S4": 1.0},
            [],
        )
        self.assertEqual(s4_trace["severity"], "large")
        self.assertEqual(s4_trace["E"], 2)
        self.assertIn("效果说服力断层", s4_trace["reason"])

    def test_s3_s4_visual_verifier_applies_nested_usage_review_without_breaking_old_s4_fields(self) -> None:
        stages = [{"stage": f"S{index}", "severity": "small"} for index in range(1, 7)]
        for role in ("creator", "benchmark"):
            stages[2][f"{role}_s3"] = {
                "usage_process_visible": True,
                "real_usage_met": True,
                "core_selling_point_visible": True,
                "action_proof_met": True,
                "action_target_contact_met": True,
                "action_application_change_visible": True,
                "critical_action_continuity_met": True,
                "usage_reason": "原始判断",
            }
            stages[3][f"{role}_s4"] = {
                "effect_visible": True,
                "effect_proposition_matched": True,
                "effect_salience": "strong",
                "comparison_control_met": True,
                "closeup_or_focus_met": True,
                "visual_difference_observed": True,
                "module_constraints_met": True,
                "effect_maximized": True,
                "requires_close_inspection": False,
                "effect_attribution_supported": True,
                "result_only_without_process": False,
                "process_linked_effect": True,
                "tamper_or_cut_risk": False,
                "effect_reason": "原始判断",
            }
        result = {"stage_analysis": stages, "improvements": []}
        applied = apply_s4_visual_verifier_result(
            result,
            {
                "creator": {
                    "s3": {
                        "evidence_sufficient": True,
                        "action_target_contact_met": False,
                        "action_application_change_visible": False,
                        "critical_action_continuity_met": False,
                        "reason": "只看到准备和完成态。",
                    },
                    "s4": {
                        "evidence_sufficient": True,
                        "effect_proposition_matched": True,
                        "visual_difference_observed": True,
                        "module_constraints_met": True,
                        "effect_salience": "clear",
                        "requires_close_inspection": False,
                        "effect_maximized": False,
                        "reason": "只见结果，未见关键作用过程。",
                    },
                },
                "benchmark": {
                    "s3": {
                        "evidence_sufficient": True,
                        "action_target_contact_met": True,
                        "action_application_change_visible": True,
                        "critical_action_continuity_met": True,
                        "reason": "关键动作和目标状态均可见。",
                    },
                    "s4": {
                        "evidence_sufficient": True,
                        "effect_proposition_matched": True,
                        "visual_difference_observed": True,
                        "module_constraints_met": True,
                        "effect_salience": "strong",
                        "requires_close_inspection": False,
                        "effect_maximized": True,
                        "reason": "效果与过程均清楚。",
                    },
                },
            },
            {},
        )
        self.assertTrue(applied)
        self.assertFalse(stages[2]["creator_s3"]["real_usage_met"])
        self.assertTrue(stages[3]["creator_s4"]["result_only_without_process"])
        self.assertFalse(stages[3]["creator_s4"]["process_linked_effect"])
        self.assertTrue(stages[2]["benchmark_s3"]["real_usage_met"])
        self.assertTrue(stages[3]["benchmark_s4"]["effect_visible"])

    def test_visual_verifier_does_not_overwrite_positive_facts_when_static_coverage_is_insufficient(self) -> None:
        stages = [{"stage": f"S{index}", "severity": "small"} for index in range(1, 7)]
        stages[2]["creator_s3"] = {
            "action_target_contact_met": True,
            "action_application_change_visible": True,
            "critical_action_continuity_met": True,
        }
        stages[3]["creator_s4"] = {
            "effect_visible": True,
            "effect_proposition_matched": True,
            "effect_salience": "clear",
            "visual_difference_observed": True,
            "module_constraints_met": True,
            "effect_maximized": False,
        }
        stages[3]["benchmark_s4"] = dict(stages[3]["creator_s4"])
        result = {"stage_analysis": stages, "improvements": []}

        apply_s4_visual_verifier_result(
            result,
            {
                "creator": {
                    "s3": {
                        "evidence_sufficient": False,
                        "action_target_contact_met": False,
                        "action_application_change_visible": False,
                        "critical_action_continuity_met": False,
                    },
                    "s4": {
                        "evidence_sufficient": False,
                        "visual_difference_observed": False,
                        "effect_proposition_matched": False,
                        "effect_salience": "none",
                    },
                },
                "benchmark": {"s4": {"evidence_sufficient": True, "visual_difference_observed": True}},
            },
            {},
        )

        self.assertTrue(stages[2]["creator_s3"]["action_target_contact_met"])
        self.assertTrue(stages[2]["creator_s3"]["action_application_change_visible"])
        self.assertTrue(stages[3]["creator_s4"]["effect_visible"])
        self.assertTrue(stages[3]["creator_s4"]["visual_difference_observed"])
        self.assertEqual(stages[2]["creator_s3"]["visual_verifier_coverage"], "insufficient_for_negative_override")
        self.assertEqual(stages[3]["creator_s4"]["visual_verifier_coverage"], "insufficient_for_negative_override")

    def test_pending_s3_s6_observations_do_not_change_execution(self) -> None:
        base_s3 = {
            "exists": True,
            "usage_process_visible": True,
            "result_only_without_process": False,
            "mouth_only_or_static": False,
            "real_usage_met": True,
            "core_selling_point_visible": True,
            "process_framing_met": True,
            "action_proof_met": True,
            "usage_context_fit": True,
            "continuity_met": True,
            "richness_met": True,
            "single_scene_continuity_met": True,
            "single_scene_variation_met": True,
            "multi_scene_logic_met": False,
            "multi_scene_transition_met": False,
            "multi_scene_role_adaptation_met": False,
            "role_design_met": True,
            "role_interaction_met": True,
            "missing_selling_points": [],
            "scene_mode": "multi_person",
            "fake_or_staged": False,
        }
        with_observations = {
            **base_s3,
            "distinct_personas_met": True,
            "steps_clear_met": True,
            "pov_immersive_met": True,
        }
        without_observations = {
            **base_s3,
            "distinct_personas_met": False,
            "steps_clear_met": False,
            "pov_immersive_met": False,
            "process_framing_met": False,
        }
        with_trace = _derive_one("S3", {"creator_s3": with_observations, "benchmark_s3": with_observations}, {"S3": 1.0}, [])
        without_trace = _derive_one("S3", {"creator_s3": without_observations, "benchmark_s3": without_observations}, {"S3": 1.0}, [])
        self.assertEqual(with_trace["derived_creator_execution"], without_trace["derived_creator_execution"])
        self.assertEqual(with_trace["severity"], without_trace["severity"])
        self.assertIn("s3_presentation_observations", with_trace)

        base_s6 = {
            "exists": True,
            "ending_position_met": True,
            "direct_order_met": True,
            "action_path_clear": True,
            "offer_or_incentive_clear": True,
            "urgency_met": True,
            "product_value_recalled": True,
            "module_fit_met": True,
            "depends_on_valid_s4": True,
            "compliance_risk": False,
            "module_type": "B",
        }
        strong_observation = {**base_s6, "urgency_evidence_met": True}
        weak_observation = {**base_s6, "urgency_evidence_met": False}
        self.assertEqual(
            _s6_cta_exec({"creator_s6": weak_observation, "benchmark_s6": strong_observation}),
            {"creator_exec": 2.0, "bench_exec": 2.0},
        )

    def test_s5_source_reconciliation_requires_locked_source_evidence(self) -> None:
        stages = [{"stage": f"S{index}"} for index in range(1, 7)]
        stages[4] = {
            "stage": "S5 信任放大",
            "creator_s5": {
                "exists": True,
                "trust_basis": "offer_or_spec",
                "independent_trust_purpose": True,
                "duplicates_other_stage": False,
                "evidence_ids": ["C5"],
            },
            "benchmark_s5": {
                "exists": True,
                "trust_basis": "authority",
                "independent_trust_purpose": True,
                "duplicates_other_stage": False,
                "trust_claim_specific": True,
                "product_relevance_met": True,
                "evidence_ids": ["B5"],
                "trust_source_evidence_ids": [],
            },
            "creator_summary": "达人提到套装数量。",
            "benchmark_summary": "标杆展示可核验认证。",
            "gap": "标杆背书更强。",
        }
        result = {
            "stage_analysis": stages,
            "improvements": [
                {"target_stage": "S5 信任放大", "title": "补强信任"},
                {"target_stage": "S1 Hook", "title": "保留项"},
            ],
            "video_understanding": {
                "creator": {"evidence_units": [{"id": "C5", "trust_source_signals": [], "trust_source_reference": ""}]},
                "benchmark": {"evidence_units": [{"id": "B5", "trust_source_signals": ["authority"], "trust_source_reference": "KKM 页面"}]},
            },
        }
        reconcile_s5_trust_sources(result, True)
        self.assertFalse(stages[4]["creator_s5"]["exists"])
        self.assertTrue(stages[4]["benchmark_s5"]["exists"])
        self.assertEqual(stages[4]["benchmark_s5"]["trust_source_evidence_ids"], ["B5"])
        self.assertIn("标杆提供了可核验", stages[4]["gap"])
        self.assertEqual([item["target_stage"] for item in result["improvements"]], ["S5 信任放大", "S1 Hook"])

        stages[4]["benchmark_s5"]["trust_source_evidence_ids"] = []
        result["video_understanding"]["benchmark"]["evidence_units"][0]["trust_source_signals"] = []
        reconcile_s5_trust_sources(result, True)
        self.assertFalse(stages[4]["benchmark_s5"]["exists"])
        self.assertEqual([item["target_stage"] for item in result["improvements"]], ["S1 Hook"])
        self.assertEqual(stages[4]["severity"], "small")

    def test_s5_source_reconciliation_rejects_vague_or_irrelevant_testimonial(self) -> None:
        stages = [{"stage": f"S{index}"} for index in range(1, 7)]
        stages[4] = {
            "stage": "S5 信任放大",
            "creator_s5": {
                "exists": False,
                "trust_basis": "none",
            },
            "benchmark_s5": {
                "exists": True,
                "trust_basis": "independent_user",
                "independent_trust_purpose": True,
                "duplicates_other_stage": False,
                "trust_claim_specific": False,
                "product_relevance_met": True,
                "evidence_ids": ["B3"],
            },
            "creator_summary": "达人没有独立背书。",
            "benchmark_summary": "评论询问产品是什么。",
            "gap": "标杆有评论背书。",
        }
        result = {
            "stage_analysis": stages,
            "video_understanding": {
                "creator": {"evidence_units": []},
                "benchmark": {
                    "evidence_units": [
                        {
                            "id": "B3",
                            "trust_source_signals": ["independent_user"],
                            "trust_source_reference": "评论：这是牙膏吗？",
                        }
                    ]
                },
            },
        }

        reconcile_s5_trust_sources(result, True)

        self.assertFalse(stages[4]["benchmark_s5"]["exists"])
        self.assertEqual(stages[4]["severity"], "small")
        self.assertIn("双方均未提供", stages[4]["gap"])

    def test_subtitle_driven_cta_is_not_overwritten_by_no_cta_placeholder(self) -> None:
        stages = [{"stage": f"S{index}"} for index in range(1, 7)]
        stages[5].update(
            {
                "benchmark_evidence_ids": ["B_NO_CTA"],
                "benchmark_quote": "",
                "benchmark_s6": {
                    "exists": True,
                    "direct_order_met": True,
                    "evidence_ids": ["B8"],
                },
            }
        )
        result = {
            "stage_analysis": stages,
            "video_understanding": {
                "benchmark": {
                    "evidence_units": [
                        {"id": "B8", "information": "提示点击下方购物车下单", "subtitle_fact": "กดตะกร้าด้านล่าง"},
                        {"id": "B_NO_CTA", "information": "结尾未识别到明确购买指令"},
                    ]
                },
                "creator": {"evidence_units": []},
            },
        }
        reconcile_unsupported_cta(result)
        self.assertEqual(stages[5]["benchmark_evidence_ids"], ["B8"])
        ids = [unit["id"] for unit in result["video_understanding"]["benchmark"]["evidence_units"]]
        self.assertNotIn("B_NO_CTA", ids)

    def test_effect_summary_cannot_become_soft_cta_without_invitation_and_offer(self) -> None:
        stages = [{"stage": f"S{index}"} for index in range(1, 7)]
        stages[5].update(
            {
                "benchmark_s6": {
                    "exists": True,
                    "direct_order_met": False,
                    "action_path_clear": False,
                    "soft_purchase_invitation_met": True,
                    "offer_or_incentive_clear": False,
                    "module_fit_met": True,
                    "ending_position_met": True,
                    "cta_reason": "结尾总结妆效，暗示值得购买。",
                    "evidence_ids": ["B8"],
                },
            }
        )
        result = {
            "stage_analysis": stages,
            "video_understanding": {
                "benchmark": {
                    "evidence_units": [
                        {"id": "B8", "information": "展示上妆后的柔雾效果", "voiceover_zh": "妆效很轻薄。"},
                    ]
                },
                "creator": {"evidence_units": []},
            },
        }

        reconcile_unsupported_cta(result)

        flag = stages[5]["benchmark_s6"]
        self.assertFalse(flag["exists"])
        self.assertFalse(flag["soft_purchase_invitation_met"])
        self.assertEqual(flag["evidence_ids"], [])
        self.assertEqual(stages[5]["benchmark_evidence_ids"], ["B_NO_CTA"])
        self.assertIn("按无 CTA 处理", flag["cta_reason"])
        self.assertIn("双方结尾均未出现", stages[5]["gap"])

    def test_stage_flag_evidence_uses_same_stage_validated_references(self) -> None:
        stages = [{"stage": f"S{index}"} for index in range(1, 7)]
        stages[1].update(
            {
                "benchmark_evidence_ids": ["B2"],
                "benchmark_s2": {"exists": True, "evidence_ids": []},
            }
        )
        result = {"stage_analysis": stages}

        align_stage_flag_evidence(result)

        self.assertEqual(stages[1]["benchmark_s2"]["evidence_ids"], ["B2"])

    def test_stage_flag_evidence_restores_primary_reference_before_placeholder(self) -> None:
        result = {
            "video_understanding": {
                "creator": {
                    "evidence_units": [
                        {
                            "id": "C3",
                            "time_range": "10.0s - 20.0s",
                            "information": "半脸前后对比展示哑光效果",
                            "visual_fact": "同侧脸颊前后对比",
                            "voiceover": "",
                            "voiceover_zh": "",
                        }
                    ]
                },
                "benchmark": {"evidence_units": []},
            },
            "stage_analysis": [{"stage": f"S{index}"} for index in range(1, 4)] + [
                {
                    "stage": "S4 效果呈现",
                    "creator_time_range": "10.0s - 20.0s",
                    "creator_evidence_ids": ["C_NO_STAGE_4"],
                    "creator_summary": "（LLM 未填写 creator_summary，需人工补充）",
                    "creator_s4": {"evidence_ids": ["C3"]},
                }
            ] + [{"stage": f"S{index}"} for index in range(5, 7)],
        }

        align_stage_flag_evidence(result)

        stage = result["stage_analysis"][3]
        self.assertEqual(stage["creator_evidence_ids"], ["C3"])
        self.assertEqual(stage["creator_summary"], "半脸前后对比展示哑光效果")
        self.assertEqual(stage["creator_support_status"], "visual_only")

    def test_repair_preserves_original_improvements_when_model_drops_them(self) -> None:
        original = {"improvements": [{"title": "保留的合法建议"}], "stage_analysis": [{"stage": "旧"}]}
        repaired = {"improvements": [], "stage_analysis": [{"stage": "修复后"}]}

        merged = pipeline.preserve_valid_repair_sections(original, repaired)

        self.assertEqual(merged["improvements"], original["improvements"])
        self.assertEqual(merged["stage_analysis"], repaired["stage_analysis"])

    def test_large_stage_without_improvement_is_reported(self) -> None:
        result = {
            "one_line_verdict": "结论",
            "holistic_assessment": {},
            "product_visibility": {"first_appearance_sec": 0, "ratio": 0.5},
            "loop_closure": {"note": "已完成"},
            "stage_analysis": [
                {
                    "stage": f"S{index}",
                    "severity": "large" if index == 6 else "small",
                    "gap_summary": ["差距"],
                    "module_fit_reason": "已判断",
                }
                for index in range(1, 7)
            ],
            "improvements": [{"target_stage": "S4"}],
        }
        validate_analysis_dimensions(result)
        self.assertTrue(any("[Q13] S6" in warning for warning in result.get("qa_warnings", [])))

    def test_improvement_reconciliation_only_targets_uncovered_large_stage(self) -> None:
        result = {
            "product_profile": {"hook_proposition": "本品命题"},
            "video_understanding": {
                "creator": {"evidence_units": [{"id": "C6", "time_range": "20s - 25s"}]},
                "benchmark": {"evidence_units": [{"id": "B6", "time_range": "18s - 22s"}]},
            },
            "stage_analysis": [
                {
                    "stage": f"S{index}",
                    "severity": "large" if index == 6 else "small",
                    "creator_evidence_ids": ["C6"] if index == 6 else [],
                    "benchmark_evidence_ids": ["B6"] if index == 6 else [],
                }
                for index in range(1, 7)
            ],
            "improvements": [{"target_stage": "S4", "title": "原建议", "priority": 1}],
        }
        missing = pipeline.uncovered_large_stage_codes(result)
        self.assertEqual(missing, ["S6"])

        payload = build_improvement_reconciliation_payload("test", result, missing, {"product": {"name": "测试品"}})
        payload_text = payload["messages"][1]["content"][0]["text"]
        self.assertIn('"missing_large_stages": [\n    "S6"', payload_text)
        self.assertIn('"id": "C6"', payload_text)

        merged = pipeline.merge_reconciled_improvements(
            result,
            [{"target_stage": "S6", "title": "补购买路径", "priority": 1}],
            missing,
        )
        self.assertEqual(pipeline.uncovered_large_stage_codes(merged), [])
        self.assertEqual(merged["improvements"][0]["target_stage"], "S6")

    def test_finalized_analysis_writes_canonical_trace_back_to_main_analysis(self) -> None:
        normalized = {
            "executive_summary": "结论",
            "one_line_summary": "结论",
            "one_line_verdict": "结论",
            "holistic_assessment": {},
            "key_conclusions": [],
            "product_visibility": {},
            "loop_closure": {"source": "proposition_trace"},
            "video_understanding": {},
            "stage_analysis": [],
            "improvements": [],
            "product_profile": {"proof_contract": {"valid": True}},
            "proposition_trace": {"version": "1.0"},
            "absolute_quality": {"S1": {"creator": {"status": "weak"}}},
            "absolute_execution_shadow": {"status": "completed", "roles": {}},
            "computed_loop_closure": {"audit_status": "closed"},
            "qa_warnings": ["warning"],
        }
        analysis = {}
        pipeline.apply_finalized_analysis_result(analysis, normalized, Path("result.json"))
        self.assertEqual(analysis["proposition_trace"], {"version": "1.0"})
        self.assertEqual(analysis["computed_loop_closure"]["audit_status"], "closed")
        self.assertEqual(analysis["product_profile"]["proof_contract"]["valid"], True)
        self.assertEqual(analysis["absolute_quality"]["S1"]["creator"]["status"], "weak")
        self.assertEqual(analysis["absolute_execution_shadow"]["status"], "completed")

    def test_stage_flag_normalization_preserves_proposition_ids(self) -> None:
        normalized = normalize_s3_flags({"proposition_ids": ["selling.1", "selling.1"], "evidence_ids": ["C1"]})
        self.assertEqual(normalized["proposition_ids"], ["selling.1"])

    def test_s3_normalization_defaults_only_non_applicable_mode_flags(self) -> None:
        normalized = normalize_s3_flags(
            {
                "scene_mode": "single_scene",
                "presentation_overlays": [],
                "multi_scene_logic_met": True,
                "steps_clear_met": True,
            }
        )
        self.assertIsNone(normalized["single_scene_continuity_met"])
        self.assertIsNone(normalized["single_scene_variation_met"])
        self.assertFalse(normalized["multi_scene_logic_met"])
        self.assertFalse(normalized["role_design_met"])
        self.assertFalse(normalized["steps_clear_met"])
        self.assertFalse(normalized["pov_immersive_met"])

    def test_doubao_patch_repair_updates_stage_by_code_and_locks_facts(self) -> None:
        base = {
            "video_understanding": {"creator": {"evidence_units": [{"id": "C1"}]}},
            "stage_analysis": [
                {
                    "stage": "S3 使用过程",
                    "severity": "medium",
                    "creator_s3": {"evidence_ids": ["C1"]},
                }
            ],
        }
        repaired = apply_llm_json_patches(
            base,
            {"patches": [{"path": "/stage_analysis/S3/creator_s3/multi_scene_logic_met", "value": False}]},
        )
        self.assertFalse(repaired["stage_analysis"][0]["creator_s3"]["multi_scene_logic_met"])
        self.assertNotIn("multi_scene_logic_met", base["stage_analysis"][0]["creator_s3"])

        for path in (
            "/video_understanding/creator/evidence_units",
            "/stage_analysis/S3/creator_s3/evidence_ids",
            "/stage_analysis/S3/creator_evidence_ids",
            "/stage_analysis/S3/creator_multimodal/channel_evidence_ids/visual",
            "/stage_analysis/S3/severity",
        ):
            with self.assertRaises(SystemExit):
                apply_llm_json_patches(base, {"patches": [{"path": path, "value": []}]})

    def test_multimodal_evidence_is_pruned_after_stage_specific_repair(self) -> None:
        result = {
            "video_understanding": {
                "creator": {
                    "evidence_units": [
                        {"id": "C3", "functions": ["S4_effect"]},
                        {"id": "C4", "functions": ["S5_trust"]},
                    ]
                }
            },
            "stage_analysis": [
                {}, {}, {}, {},
                {
                    "creator_evidence_ids": ["C4"],
                    "creator_multimodal": {
                        "channel_evidence_ids": {
                            "visual": ["C4"],
                            "speech": ["C3", "C4"],
                        }
                    },
                },
            ],
        }
        prune_multimodal_evidence_to_stage(result)
        refs = result["stage_analysis"][4]["creator_multimodal"]["channel_evidence_ids"]
        self.assertEqual(refs["visual"], ["C4"])
        self.assertEqual(refs["speech"], ["C4"])

    def test_provider_detection_keeps_doubao_repair_provider_specific(self) -> None:
        self.assertTrue(llm_api.is_doubao_model("doubao-seed-2.0-lite"))
        self.assertFalse(llm_api.is_doubao_model("qwen-omni-turbo"))

    def test_proposition_trace_links_s3_s4_and_does_not_change_severity(self) -> None:
        foundation = self._proposition_foundation()
        stages = [
            {"stage": "S1 Hook", "severity": "small", "creator_hook": {"anchors_proposition": True, "proposition_ids": ["hook.1"]}, "benchmark_hook": {"anchors_proposition": True, "proposition_ids": ["hook.1"]}},
            {"stage": "S2 产品引出", "severity": "small", "creator_s2": {"handoff_met": True, "product_identity_clear": True, "product_role_clear": True, "proposition_ids": ["hook.1"]}, "benchmark_s2": {"handoff_met": True, "product_identity_clear": True, "product_role_clear": True, "proposition_ids": ["hook.1"]}},
            {"stage": "S3 使用过程", "severity": "medium", "creator_s3": {"usage_process_visible": True, "core_selling_point_visible": True, "process_framing_met": True, "proposition_ids": ["selling.1"]}, "benchmark_s3": {"usage_process_visible": True, "core_selling_point_visible": True, "process_framing_met": True, "proposition_ids": ["selling.1"]}},
            {"stage": "S4 效果呈现", "severity": "large", "creator_s4": {"effect_visible": True, "effect_salience": "strong", "effect_proposition_matched": True, "effect_attribution_supported": True, "process_linked_effect": True, "proposition_ids": ["proof.1"]}, "benchmark_s4": {"effect_visible": True, "effect_salience": "strong", "effect_proposition_matched": True, "effect_attribution_supported": True, "process_linked_effect": True, "proposition_ids": ["proof.1"]}},
            {"stage": "S5 信任放大", "severity": "small", "creator_s5": {"exists": True, "product_relevance_met": True, "proposition_ids": ["selling.1"]}, "benchmark_s5": {"exists": True, "product_relevance_met": True, "proposition_ids": ["selling.1"]}},
            {"stage": "S6 CTA", "severity": "medium", "creator_s6": {"exists": True, "direct_order_met": True, "ending_position_met": True, "product_value_recalled": True, "proposition_ids": ["selling.1"]}, "benchmark_s6": {"exists": True, "direct_order_met": True, "ending_position_met": True, "product_value_recalled": True, "proposition_ids": ["selling.1"]}},
        ]
        result = {
            "category_profile": foundation["category_profile"],
            "product_profile": foundation["product_profile"],
            "stage_analysis": stages,
            "s3_s4_relationship": {
                "creator_relationship": "process_creates_effect",
                "benchmark_relationship": "process_creates_effect",
            },
        }
        before = [stage["severity"] for stage in stages]
        materialize_cross_stage_inputs(result, {"brand_proposition": {}})
        shadow = {
            "status": "completed",
            "errors": [],
            "roles": {
                "creator": {
                    "stages": {
                        "S1": {"score": 0.5, "status": "weak", "reason": "shadow", "evidence_ids": ["C1"]},
                    },
                },
            },
        }
        materialize_quality_audits(result, {"absolute_execution_shadow": shadow})

        self.assertEqual([stage["severity"] for stage in stages], before)
        self.assertEqual(stages[0]["creator_absolute_execution_shadow"]["score"], 0.5)
        self.assertEqual(result["absolute_execution_shadow"]["status"], "completed")
        creator_edges = result["proposition_trace"]["roles"]["creator"]["edges"]
        self.assertEqual(creator_edges["S3_to_S4"]["status"], "same_claim_proven")
        self.assertEqual(creator_edges["S6_recall"]["status"], "value_recalled")
        self.assertEqual(result["computed_loop_closure"]["audit_status"], "closed")
        self.assertEqual(result["loop_closure"]["source"], "proposition_trace")
        validate_analysis_dimensions(result)
        self.assertFalse(any("缺少第二步槽位间闭环校验" in warning for warning in result.get("qa_warnings", [])))
        self.assertEqual(stages[5]["computed_stage_standard_delivery"], "both")

    def test_stage_time_coherence_allows_functional_overlap(self) -> None:
        ranges = ["0s - 4s", "3s - 7s", "5s - 12s", "6s - 12s", "1s - 2s", "11s - 13s"]
        result = {
            "stage_analysis": [
                {
                    "stage": f"S{index}",
                    "creator_time_range": value,
                    "benchmark_time_range": value,
                    **(
                        {
                            "creator_s2": {"merged_with_s3": True},
                            "benchmark_s2": {"merged_with_s3": True},
                        }
                        if index == 2
                        else {}
                    ),
                }
                for index, value in enumerate(ranges, start=1)
            ]
        }
        validate_stage_time_coherence(result)
        self.assertFalse(result.get("qa_warnings"))

        result["stage_analysis"][1]["creator_s2"]["merged_with_s3"] = False
        validate_stage_time_coherence(result)
        warnings = result.get("qa_warnings") or []
        self.assertEqual(len(warnings), 1)
        self.assertIn("creator S2/S3", warnings[0])

    def test_json_codec_stays_compatible_with_parse_facade(self) -> None:
        from flayr_core.llm.parse import parse_json_text as parse_facade

        malformed = '{"value":"他说 "好"",}'
        self.assertEqual(parse_json_text(malformed), {"value": '他说 "好"'})
        self.assertEqual(parse_facade(malformed), {"value": '他说 "好"'})

    def test_product_profile_stays_compatible_with_parse_facade(self) -> None:
        from flayr_core.llm.parse import normalize_product_profile as parse_facade
        from flayr_core.llm.product_profile import normalize_product_profile as direct

        self.assertIs(parse_facade, direct)

    def test_command_timeout_is_returned_as_normal_failure(self) -> None:
        with mock.patch.object(
            utils.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["slow-tool"], 12, stderr="slow"),
        ):
            completed = utils.run_command(["slow-tool"], timeout_seconds=12)
        self.assertEqual(completed.returncode, 124)
        self.assertIn("timed out after 12s", completed.stderr)

    def test_atomic_write_preserves_existing_artifact_when_replace_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "analysis.json"
            path.write_text("old", encoding="utf-8")
            with mock.patch.object(utils.os, "replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    utils.write_text(path, "new")
            self.assertEqual(path.read_text(encoding="utf-8"), "old")
            self.assertFalse(list(path.parent.glob(".analysis.json.*.tmp")))

    def test_frame_extraction_clears_stale_frames_before_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frames_dir = root / "frames"
            focus_dir = root / "focus"
            frames_dir.mkdir()
            focus_dir.mkdir()
            (frames_dir / "frame_9999.jpg").write_bytes(b"old")
            (focus_dir / "hook_9999.jpg").write_bytes(b"old")
            result = {"errors": [], "duration_seconds": 0.0}
            with mock.patch.object(video, "run_command", return_value=SimpleNamespace(returncode=1, stderr="ffmpeg failed")):
                video.extract_frames(root / "source.mp4", frames_dir, focus_dir, result)
            self.assertFalse((frames_dir / "frame_9999.jpg").exists())
            self.assertFalse((focus_dir / "hook_9999.jpg").exists())

    def test_analysis_contract_rejects_invalid_raw_envelope(self) -> None:
        with self.assertRaises(AnalysisContractError):
            validate_raw_analysis_envelope({"stage_analysis": [], "improvements": []})

        accepted = validate_raw_analysis_envelope(
            {"stage_analysis": [{"stage": f"S{index}"} for index in range(1, 7)], "improvements": [{"title": "one"}]}
        )
        self.assertEqual(len(accepted["stage_analysis"]), 6)

    def test_analysis_contract_rejects_normalized_stage_order_drift(self) -> None:
        result = {
            "one_line_summary": "summary",
            "executive_summary": "summary",
            "holistic_assessment": {},
            "product_visibility": {},
            "loop_closure": {},
            "video_understanding": {},
            "stage_analysis": [{"stage": f"S{index} stage"} for index in range(1, 7)],
            "improvements": [{"title": "one"}],
        }
        validate_normalized_analysis_contract(result)
        result["stage_analysis"][2]["stage"] = "S4 stage"
        with self.assertRaises(AnalysisContractError):
            validate_normalized_analysis_contract(result)

    def test_quality_contract_rejects_missing_stage_narrative(self) -> None:
        incomplete = {
            "stage_analysis": [
                {
                    "stage": "S1 Hook",
                    "benchmark_summary": "标杆表现",
                    "creator_summary": "达人表现",
                    "gap": "（LLM 未填写 gap，需人工补充）",
                }
            ]
        }
        with self.assertRaises(SystemExit):
            validate_required_stage_narratives(incomplete)

        complete = json.loads(json.dumps(incomplete, ensure_ascii=False))
        complete["stage_analysis"][0]["gap"] = "达人未承接标杆的核心痛点。"
        validate_required_stage_narratives(complete)

    def test_stage_catalog_is_shared_by_parse_and_artifact_fallback(self) -> None:
        from flayr_core.llm.parse import STAGES

        ranges = fallback_artifact_ranges(20.0)
        self.assertEqual(STAGES, stage_tuples())
        self.assertEqual([item[0] for item in ranges], [stage.name for stage in DEFAULT_STAGES])
        self.assertEqual(ranges[-1][2:], (15.0, 20.0))

    def test_certification_policy_reaches_all_analysis_prompts(self) -> None:
        comparison = build_llm_comparison_payload("test", "input", {}, {"videos": {}})
        comparison_content = comparison["messages"][1]["content"]
        comparison_text = comparison_content[0]["text"] if isinstance(comparison_content, list) else comparison_content
        repair_text = build_llm_repair_payload("test", "{}", "error", "input")["messages"][0]["content"]
        review = build_stage_review_payload(
            "test",
            {"videos": {}},
            {"benchmark": {"evidence_units": []}, "creator": {"evidence_units": []}},
            {"stage_analysis": [{"stage": "S1 Hook", "creator_time_range": "0s - 3s", "benchmark_time_range": "0s - 3s"}]},
            ["S1"],
        )
        review_text = review["messages"][1]["content"][0]["text"]

        with tempfile.TemporaryDirectory() as tmp:
            analysis_input = write_analysis_input(
                Path(tmp),
                {
                    "analysis_scope": {"label": "视频证据分析", "missing_context": [], "boundary": "仅按视频事实判断"},
                    "product": {"name": "测试品", "category": "", "price": "", "target_market": "auto", "core_selling_points": "", "target_user": "", "purchase_motivation": "", "creator_profile": "", "notes": ""},
                    "videos": {},
                },
            ).read_text(encoding="utf-8")

        self.assertIn(CERTIFICATION_OWNERSHIP_PROMPT, comparison_text)
        self.assertIn(CERTIFICATION_OWNERSHIP_PROMPT, repair_text)
        self.assertIn(CERTIFICATION_OWNERSHIP_PROMPT, analysis_input)
        self.assertNotIn("开头的背书/认证类内容按钩子算", comparison_text)
        self.assertNotIn("只归入 S2", repair_text)
        self.assertNotIn("产品名/卖点/认证", review_text)

    def test_multimodal_contract_reaches_all_analysis_prompts(self) -> None:
        comparison = build_llm_comparison_payload("test", "input", {}, {"videos": {}})
        repair = build_llm_repair_payload("test", "{}", "error", "input")
        review = build_stage_review_payload(
            "test",
            {"videos": {}},
            {"benchmark": {"evidence_units": []}, "creator": {"evidence_units": []}},
            {"stage_analysis": [{"stage": "S1 Hook", "creator_time_range": "0s - 3s", "benchmark_time_range": "0s - 3s"}]},
            ["S1"],
        )
        for payload in (comparison, repair, review):
            payload_text = json.dumps(payload, ensure_ascii=False)
            self.assertIn("S1-S6 跨模态综合合同", payload_text)
            self.assertIn("禁止按最弱渠道一票否决", payload_text)
            self.assertIn("S3", payload_text)
            self.assertIn("真实使用过程与关键动作可见是硬条件", payload_text)

    def test_phase_c_does_not_reuse_stale_multimodal_assessment(self) -> None:
        current = {
            "stage_analysis": [
                {
                    "stage": "S1 Hook",
                    "creator_multimodal": {"integrated_effect": "strong"},
                    "benchmark_multimodal": {"integrated_effect": "strong"},
                }
            ],
            "improvements": [],
        }
        review = {"stage_updates": [{"stage": "S1 Hook"}]}
        with mock.patch.object(pipeline, "_process_llm_result", side_effect=lambda result, *_: result):
            merged = pipeline.apply_stage_review_updates(current, review, {}, "", {})
        stage = merged["stage_analysis"][0]
        self.assertNotIn("creator_multimodal", stage)
        self.assertNotIn("benchmark_multimodal", stage)

    def test_pending_s3_s6_fields_reach_all_contract_surfaces(self) -> None:
        comparison = build_llm_comparison_payload("test", "input", {}, {"videos": {}})
        comparison_text = json.dumps(comparison, ensure_ascii=False)
        repair_text = json.dumps(build_llm_repair_payload("test", "{}", "error", "input"), ensure_ascii=False)
        review = build_stage_review_payload(
            "test",
            {"videos": {}},
            {"benchmark": {"evidence_units": []}, "creator": {"evidence_units": []}},
            {
                "stage_analysis": [
                    {"stage": "S3 使用过程", "creator_time_range": "8s - 18s", "benchmark_time_range": "8s - 18s"},
                    {"stage": "S6 CTA", "creator_time_range": "25s - 30s", "benchmark_time_range": "25s - 30s"},
                ]
            },
            ["S3", "S6"],
        )
        review_text = json.dumps(review, ensure_ascii=False)
        schema_text = (ROOT / "references" / "analysis-output-schema.json").read_text(encoding="utf-8")
        for field in (
            "action_application_change_visible",
            "soft_purchase_invitation_met",
            "distinct_personas_met",
            "steps_clear_met",
            "pov_immersive_met",
            "price_anchor_met",
            "urgency_evidence_met",
            "gift_stack_met",
            "guarantee_clear_met",
        ):
            self.assertIn(field, comparison_text)
            self.assertIn(field, repair_text)
            self.assertIn(field, review_text)
            self.assertIn(field, schema_text)
        self.assertIn("distinct_personas_met", inspect.getsource(validate_s3_usage_flags))
        self.assertIn("action_application_change_visible", inspect.getsource(validate_s3_usage_flags))
        self.assertIn("soft_purchase_invitation_met", inspect.getsource(validate_s6_cta_flags))
        self.assertIn("price_anchor_met", inspect.getsource(validate_s6_cta_flags))

    def test_all_result_entries_delegate_to_one_finalizer(self) -> None:
        self.assertIn("finalize_analysis_result", inspect.getsource(pipeline.merge_analysis_result))
        self.assertIn("finalize_analysis_result", inspect.getsource(pipeline._process_llm_result))
        self.assertEqual(inspect.getsource(pipeline.finalize_analysis_result).count("validate_analysis_dimensions"), 1)

    def test_preprocess_cache_requires_matching_video_and_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "source.mp4"
            video.write_bytes(b"first-video")
            role_dir = root / "creator"
            frames = role_dir / "frames"
            frames.mkdir(parents=True)
            transcript = role_dir / "transcript.txt"
            transcript.write_text("cached transcript", encoding="utf-8")
            args = self._cache_args()
            deps = self._cache_deps()
            fingerprint = flayr.build_preprocess_fingerprint(video, deps, args)
            (role_dir / "_preprocess.json").write_text(
                json.dumps({"frames_dir": str(frames), "transcript_path": str(transcript), "preprocess_fingerprint": fingerprint}),
                encoding="utf-8",
            )
            self.assertIsNotNone(flayr.load_existing_video_result(role_dir, fingerprint))

            video.write_bytes(b"changed-video")
            self.assertIsNone(flayr.load_existing_video_result(role_dir, flayr.build_preprocess_fingerprint(video, deps, args)))

            args.whisper_language = "th"
            self.assertIsNone(flayr.load_existing_video_result(role_dir, flayr.build_preprocess_fingerprint(video, deps, args)))

    def test_default_run_dir_is_unique(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(flayr, "DEFAULT_RUNS_DIR", Path(tmp)):
            args = SimpleNamespace(output_dir=None, mode="improve")
            first = flayr.create_run_dir(args)
            second = flayr.create_run_dir(args)
        self.assertNotEqual(first, second)

    def test_translation_failure_is_nonfatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            role_dir = Path(tmp)
            (role_dir / "transcript.txt").write_text("Ini contoh", encoding="utf-8")
            result = {"errors": []}
            args = SimpleNamespace(
                translation_model="test-model", llm_model="", product_name="", product_notes="",
                llm_dry_run=False, llm_api_url="https://example.invalid",
            )
            with mock.patch.object(translation, "read_llm_api_key", return_value="test-key"), mock.patch.object(
                translation, "call_llm_api", side_effect=SystemExit("network failed")
            ):
                translation.translate_transcript_with_llm(args, "creator", role_dir, result)
        self.assertEqual(result["translation_status"], "failed")
        self.assertTrue(any("network failed" in str(item) for item in result["errors"]))

    def test_optional_provider_curl_commands_do_not_expose_key(self) -> None:
        proposal_commands = self._capture_curl(
            proposal_video,
            lambda: proposal_video.curl_json("POST", "https://example.invalid", "secret-key", {}, False),
        )
        voice_commands = self._capture_curl(
            voice_clone,
            lambda: voice_clone._curl_json(["https://example.invalid"], "secret-key"),
        )
        for command in [*proposal_commands, *voice_commands]:
            rendered = " ".join(str(item) for item in command)
            self.assertNotIn("secret-key", rendered)
            self.assertTrue(any(str(item).startswith("@") for item in command))

    @staticmethod
    def _global_result() -> dict[str, object]:
        stages = []
        for index in range(1, 7):
            stages.append(
                {
                    "stage": f"S{index}",
                    "severity": "small",
                    "comparison_status": "direct",
                    "gap": f"S{index} gap",
                    "creator_evidence_ids": ["C1"],
                    "creator_absolute_status": "complete",
                }
            )
        return {
            "product_profile": {},
            "video_understanding": {
                "creator": {
                    "temporal_evidence_mode": "full_temporal",
                    "gate_observation_status": {
                        "selling_point_route": "complete",
                        "variant_focus": "complete",
                        "attention_scan": "complete",
                    },
                    "selling_point_observations": [],
                    "variant_decision_rule": {},
                    "attention_competitors": [],
                    "evidence_units": [
                        {
                            "id": "C1",
                            "time_range": "0.0s - 10.0s",
                            "variant_ids": [],
                            "variant_visual_shares": {},
                            "variant_speech_shares": {},
                            "variant_relation_mode": "none",
                            "comparison_purpose_explicit": False,
                            "variant_attribution_confident": False,
                            "variant_data_valid": True,
                            "functions": ["S3_usage", "S4_effect"],
                        }
                    ],
                },
                "benchmark": {
                    "temporal_evidence_mode": "full_temporal",
                    "gate_observation_status": {
                        "selling_point_route": "complete",
                        "variant_focus": "complete",
                        "attention_scan": "complete",
                    },
                    "selling_point_observations": [],
                    "variant_decision_rule": {},
                    "attention_competitors": [],
                    "evidence_units": [{"id": "B1", "time_range": "0.0s - 10.0s", "variant_ids": [], "variant_data_valid": True}],
                },
            },
            "stage_analysis": stages,
            "improvements": [{"target_stage": "S4", "title": "强化效果证明"}],
        }

    @staticmethod
    def _cache_args() -> SimpleNamespace:
        return SimpleNamespace(
            skip_whisper=False, whisper_language="auto", translate_with_llm=False,
            translation_model="", llm_model="", llm_api_url="", product_name="", product_notes="",
            ocr_mode="off", with_ocr=False, no_ocr=False, llm_dry_run=True,
        )

    @staticmethod
    def _cache_deps() -> dict[str, object]:
        return {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "whisper": "whisper-cli", "whisper_model": None, "whisper_model_th": None}

    @staticmethod
    def _capture_curl(module: object, callback: object) -> list[list[object]]:
        commands: list[list[object]] = []

        def fake_run(command: list[object]) -> SimpleNamespace:
            commands.append(command)
            if "-o" in command:
                Path(str(command[command.index("-o") + 1])).write_text("{}", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")

        with mock.patch.object(module, "run_command", side_effect=fake_run):
            callback()
        return commands


if __name__ == "__main__":
    unittest.main()
