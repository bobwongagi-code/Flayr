"""flayr_core.postprocess.chain：postprocess 流水线编排。

本模块只放 apply_postprocess_chain 一个函数，不写任何业务逻辑。
每一步通过显式 import 引入，读起来像一份"流水线说明书"——
每行都能一眼看出调的是哪个模块的哪个函数。

被两个 caller 共享：
  - flayr_core.llm.pipeline.merge_analysis_result
  - flayr_core.llm.pipeline._process_llm_result (内含主链 + repair 重试)

不包括的尾部处理（两个 caller 各自显式调用，顺序略有差异）：
  - merge: ground_improvement_evidence + sanitize_child_toothpaste/health (用 merge_context)
           + validate_evidence_alignment + validate_stage_ownership
           + remove_unverified_brand_models + clamp_result_time_ranges
  - _process_llm_result: validate_evidence_alignment + validate_stage_ownership
           + sanitize_health/child_toothpaste (用 analysis_input)
           + ground_improvement_evidence + validate_analysis_dimensions
           + validate_recommendation_safety + validate_creator_script_language
           + remove_unverified_brand_models + clamp_result_time_ranges
"""

from __future__ import annotations

from typing import Any

from .audit import PostprocessAudit

# 通用校验（会抛 SystemExit）
from .validate import validate_transcript_attribution

# 内容修补（修改 result data，正常返回）
from .repair import (
    apply_comparison_eligibility,
    align_stage_flag_evidence,
    align_clear_commerce_evidence,
    align_timed_cta_from_transcript,
    bind_timed_transcript_quotes,
    deduplicate_stage_quotes,
    derive_product_visibility,
    downgrade_unverified_sensitive_claims,
    fill_missing_evidence_references,
    ground_stage_visual_evidence,
    materialize_spoken_stage_evidence,
    prune_multimodal_evidence_to_stage,
    reconcile_s3_s4_evidence_coherence,
    reconcile_s5_trust_sources,
    reconcile_unsupported_cta,
    repair_s1_hook_boundaries,
    stabilize_improvement_priorities,
    stabilize_stage_severity,
)

# MY 市场认证主张专项
from .claims_my import (
    discard_unreferenced_certification_claims,
    reconcile_certification_ownership,
)

# 4d：severity 确定性推导（执行分 + 品类权重表；事实缺失自动跳过，绝不抛错）
from .derive import derive_severity_from_facts
from .proposition import materialize_cross_stage_inputs, materialize_quality_audits


def stamp_product_foundation(normalized: dict[str, Any], analysis: dict[str, Any] | None) -> None:
    """Step-0 品地基权威覆盖：若上游已确立 product_foundation（特征+命题），用它覆盖结果里的
    category_profile/product_profile，杜绝阶段2 内联现编的漂移；下游 derive(4d) 因此读到权威值。
    无地基（如离线复跑、Step-0 失败兜底）则原样保留模型产出，主分析照常跑完。"""
    foundation = (analysis or {}).get("product_foundation") or {}
    if foundation.get("category_profile"):
        normalized["category_profile"] = foundation["category_profile"]
    if foundation.get("product_profile"):
        profile = dict(normalized.get("product_profile") or {})
        for key, value in foundation["product_profile"].items():
            if value not in (None, "", []):
                profile[key] = value
        normalized["product_profile"] = profile


def stamp_comparison_eligibility(normalized: dict[str, Any], analysis: dict[str, Any] | None) -> None:
    """资格层是 facts 的独立判定，不允许主对比模型漏填或改写。"""
    contract = (analysis or {}).get("comparison_contract") or (analysis or {}).get("comparison_eligibility")
    if isinstance(contract, dict):
        normalized["comparison_contract"] = dict(contract)
        normalized["comparison_eligibility"] = dict(contract)


def sanitize_promise_chain_scope(normalized: dict[str, Any]) -> None:
    """promise_chain 只管 S1-S4；CTA/促单问题归 S6，不让它污染承诺链。"""
    chain = normalized.get("promise_chain")
    if not isinstance(chain, dict):
        return
    reason = str(chain.get("break_reason") or "")
    # “转化链条”可泛指 S1-S4 的承诺到效果验证，不能单独视为 CTA 污染。
    if not any(token in reason for token in ("S5", "S6", "CTA", "促单", "下单", "购买指令")):
        return
    if str(chain.get("broken_at") or "").strip() not in {"none", "unknown", ""}:
        return
    chain["chain_closed"] = True
    chain["broken_at"] = "none"
    chain["break_reason"] = "前四阶段的承诺、承接、证明与效果已围绕同一产品命题闭环；后续购买引导不属于本字段。"


def apply_postprocess_chain(
    normalized: dict[str, Any],
    analysis: dict[str, Any],
    audit: PostprocessAudit | None = None,
) -> None:
    """两个 caller 共享的中段流水线，并可记录每个规则的字段变更。"""

    def step(rule: str, function: Any, *args: Any) -> None:
        if audit is None:
            function(*args)
        else:
            audit.run(normalized, rule, function, *args)

    step("postprocess.stamp_product_foundation", stamp_product_foundation, normalized, analysis)
    step("postprocess.stamp_comparison_eligibility", stamp_comparison_eligibility, normalized, analysis)
    step("postprocess.sanitize_promise_chain_scope", sanitize_promise_chain_scope, normalized)
    step("postprocess.validate_transcript_attribution", validate_transcript_attribution, normalized, analysis)
    step("postprocess.align_clear_commerce_evidence", align_clear_commerce_evidence, normalized)
    step("postprocess.bind_timed_transcript_quotes", bind_timed_transcript_quotes, normalized, analysis)
    step("postprocess.reconcile_certification_ownership", reconcile_certification_ownership, normalized)
    step("postprocess.discard_unreferenced_certification_claims", discard_unreferenced_certification_claims, normalized)
    step("postprocess.align_timed_cta_from_transcript", align_timed_cta_from_transcript, normalized, analysis)
    step("postprocess.reconcile_unsupported_cta", reconcile_unsupported_cta, normalized)
    step("postprocess.downgrade_unverified_sensitive_claims", downgrade_unverified_sensitive_claims, normalized)
    step("postprocess.ground_stage_visual_evidence", ground_stage_visual_evidence, normalized)
    step("postprocess.deduplicate_stage_quotes", deduplicate_stage_quotes, normalized)
    step("postprocess.materialize_spoken_stage_evidence", materialize_spoken_stage_evidence, normalized)
    step("postprocess.align_stage_flag_evidence", align_stage_flag_evidence, normalized)
    step("postprocess.fill_missing_evidence_references", fill_missing_evidence_references, normalized)
    step("postprocess.reconcile_s3_s4_evidence_coherence", reconcile_s3_s4_evidence_coherence, normalized)
    step("postprocess.derive_product_visibility", derive_product_visibility, normalized, analysis)
    step("postprocess.repair_s1_hook_boundaries", repair_s1_hook_boundaries, normalized, analysis)
    step(
        "postprocess.reconcile_s5_trust_sources",
        reconcile_s5_trust_sources,
        normalized,
        analysis.get("s5_source_signals_required") is True,
    )
    step("postprocess.prune_multimodal_evidence_to_stage", prune_multimodal_evidence_to_stage, normalized)
    step("postprocess.materialize_cross_stage_inputs", materialize_cross_stage_inputs, normalized, analysis)
    step("postprocess.stabilize_stage_severity", stabilize_stage_severity, normalized)
    step("postprocess.derive_severity_from_facts", derive_severity_from_facts, normalized, analysis)
    step("postprocess.apply_comparison_eligibility", apply_comparison_eligibility, normalized)
    step("postprocess.materialize_quality_audits", materialize_quality_audits, normalized, analysis)
    step("postprocess.stabilize_improvement_priorities", stabilize_improvement_priorities, normalized)
