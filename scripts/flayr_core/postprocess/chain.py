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

# 通用校验（会抛 SystemExit）
from .validate import validate_transcript_attribution

# 内容修补（修改 result data，正常返回）
from .repair import (
    align_clear_commerce_evidence,
    align_timed_cta_from_transcript,
    bind_timed_transcript_quotes,
    deduplicate_stage_quotes,
    derive_product_visibility,
    downgrade_unverified_sensitive_claims,
    fill_missing_evidence_references,
    ground_stage_visual_evidence,
    materialize_spoken_stage_evidence,
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


def sanitize_promise_chain_scope(normalized: dict[str, Any]) -> None:
    """promise_chain 只管 S1-S4；CTA/促单问题归 S6，不让它污染承诺链。"""
    chain = normalized.get("promise_chain")
    if not isinstance(chain, dict):
        return
    reason = str(chain.get("break_reason") or "")
    if not any(token in reason for token in ("S5", "S6", "CTA", "促单", "下单", "购买指令", "转化链条")):
        return
    if str(chain.get("broken_at") or "").strip() not in {"none", "unknown", ""}:
        return
    chain["chain_closed"] = True
    chain["broken_at"] = "none"
    chain["break_reason"] = "前四阶段的承诺、承接、证明与效果已围绕同一产品命题闭环；后续购买引导不属于本字段。"


def apply_postprocess_chain(normalized: dict[str, Any], analysis: dict[str, Any]) -> None:
    """两个 caller 共享的中段流水线。每一步对应一个独立职责模块。"""
    stamp_product_foundation(normalized, analysis)                           # foundation  Step-0 品地基权威覆盖（须在 derive 前）
    sanitize_promise_chain_scope(normalized)                                  # repair      S1-S4 承诺链不接收 S6/CTA 断点
    validate_transcript_attribution(normalized, analysis)                    # validate    跨视频串证据校验
    align_clear_commerce_evidence(normalized)                                # repair      关键词归位 benchmark 事实
    bind_timed_transcript_quotes(normalized, analysis)                       # repair      SRT 时间戳回填 quote
    reconcile_certification_ownership(normalized)                            # claims_my   KKM/认证统一归 S5
    discard_unreferenced_certification_claims(normalized)                    # claims_my   删除未支撑认证主张
    align_timed_cta_from_transcript(normalized, analysis)                    # repair      尾段 CTA 时间对齐
    reconcile_unsupported_cta(normalized)                                    # repair      无 CTA 时补占位 evidence
    downgrade_unverified_sensitive_claims(normalized)                        # repair      未验证敏感主张降级 voice_only
    ground_stage_visual_evidence(normalized)                                 # repair      visual_evidence 对齐 evidence_unit
    deduplicate_stage_quotes(normalized)                                     # repair      跨阶段去重 quote 子句
    materialize_spoken_stage_evidence(normalized)                            # repair      有口播无时段证据时补 stage 占位
    fill_missing_evidence_references(normalized)                             # repair      引用错位时补占位或就近匹配
    derive_product_visibility(normalized, analysis)                          # repair      达人产品出镜标记确定性累加 product_visibility
    repair_s1_hook_boundaries(normalized, analysis)                           # repair      S1/S2 边界按 SRT/facts 候选收敛，防 Hook 吃掉产品引出
    materialize_cross_stage_inputs(normalized, analysis)                       # proposition 品命题矩阵 + S1→S2/S4→S6 跨阶段输入
    stabilize_stage_severity(normalized)                                      # repair      severity 阶段归属漂移校准
    derive_severity_from_facts(normalized, analysis)                          # derive      4d 执行分+权重表确定性推导（成功则覆盖，缺事实保留上游结果；含晃动封顶）
    materialize_quality_audits(normalized, analysis)                           # proposition 绝对质量层 + S5/S6 命题审计（不覆盖 gap severity）
    stabilize_improvement_priorities(normalized)                              # repair      Top 改进跟随最终商业判断
