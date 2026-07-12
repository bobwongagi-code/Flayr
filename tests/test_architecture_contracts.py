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
from flayr_core import proposal_video, translation, utils, video, voice_clone
from flayr_core.report import executive_summary, stage_skipped
from flayr_core.llm import pipeline
from flayr_core.llm.analysis_contract import (
    AnalysisContractError,
    validate_normalized_analysis_contract,
    validate_raw_analysis_envelope,
)
from flayr_core.llm.json_codec import parse_json_text
from flayr_core.llm.product_profile import normalize_product_profile, normalize_proof_contract
from flayr_core.llm.s4_visual_verifier import _visual_verifier_skip_reason
from flayr_core.llm.payload import (
    build_comparison_eligibility_payload,
    build_improvement_reconciliation_payload,
    build_llm_comparison_payload,
    build_llm_repair_payload,
    build_stage_review_payload,
)
from flayr_core.llm.parse import normalize_analysis_result, normalize_s3_flags, normalize_video_fact_result
from flayr_core.postprocess.proposition import materialize_cross_stage_inputs, materialize_quality_audits
from flayr_core.postprocess.chain import stamp_comparison_eligibility
from flayr_core.postprocess.derive import _s6_cta_exec
from flayr_core.postprocess.repair import (
    align_stage_flag_evidence,
    apply_comparison_eligibility,
    reconcile_unsupported_cta,
    stabilize_improvement_priorities,
)
from flayr_core.postprocess.health_rewrite import is_child_toothpaste_context
from flayr_core.postprocess.validate import validate_analysis_dimensions, validate_stage_time_coherence
from flayr_core.prompt import write_analysis_input
from flayr_core.proposition_contract import build_product_proposition_contract
from flayr_core.stage_catalog import DEFAULT_STAGES, fallback_artifact_ranges, stage_tuples
from flayr_core.stage_ownership import CERTIFICATION_OWNERSHIP_PROMPT


class ArchitectureContractTests(unittest.TestCase):
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
                "effect_visible": True,
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
        self.assertEqual(normalized["comparison_eligibility"]["direct_product_stages"], ["S1"])

    def test_locked_comparison_scope_reaches_and_overrides_main_analysis(self) -> None:
        facts = {
            "benchmark": {"product_identity": {"product_category": "挂烫机", "form_factor": "手持挂烫机"}},
            "creator": {"product_identity": {"product_category": "地面清洁机", "form_factor": "吸尘清洁机"}},
        }
        scope_payload = build_comparison_eligibility_payload("test", facts)
        scope_text = scope_payload["messages"][1]["content"][0]["text"]
        self.assertIn("手持挂烫机", scope_text)

        stages = [{"stage": f"S{index}"} for index in range(1, 7)]
        result = {"stage_analysis": stages, "comparison_eligibility": {"scope": "same_product"}}
        analysis = {"comparison_eligibility": {"scope": "cross_product", "direct_product_stages": ["S1"], "reason": "形态不同"}}
        stamp_comparison_eligibility(result, analysis)
        self.assertEqual(result["comparison_eligibility"]["scope"], "cross_product")

    def test_cross_product_scope_excludes_s2_to_s5_from_gap_and_improvements(self) -> None:
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

        self.assertEqual(stages[0].get("comparison_status"), None)
        self.assertEqual(stages[1]["comparison_status"], "not_directly_comparable")
        self.assertEqual(stages[4]["comparison_status"], "not_directly_comparable")
        self.assertEqual(result["improvements"], [{"target_stage": "S6", "title": "CTA 建议", "priority": 1}])
        self.assertIn("S2-S5 仅作创意参考", result["one_line_summary"])
        self.assertEqual(result["key_conclusions"][0], result["one_line_summary"])
        skipped, reason = stage_skipped(stages[2])
        self.assertTrue(skipped)
        self.assertIn("仅作为创意参考", reason)

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
        self.assertIn("不作产品级优劣比较", summary)
        self.assertNotIn("达人效果弱于标杆", summary)

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
            "computed_loop_closure": {"audit_status": "closed"},
            "qa_warnings": ["warning"],
        }
        analysis = {}
        pipeline.apply_finalized_analysis_result(analysis, normalized, Path("result.json"))
        self.assertEqual(analysis["proposition_trace"], {"version": "1.0"})
        self.assertEqual(analysis["computed_loop_closure"]["audit_status"], "closed")
        self.assertEqual(analysis["product_profile"]["proof_contract"]["valid"], True)

    def test_stage_flag_normalization_preserves_proposition_ids(self) -> None:
        normalized = normalize_s3_flags({"proposition_ids": ["selling.1", "selling.1"], "evidence_ids": ["C1"]})
        self.assertEqual(normalized["proposition_ids"], ["selling.1"])

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
        materialize_quality_audits(result, {})

        self.assertEqual([stage["severity"] for stage in stages], before)
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
