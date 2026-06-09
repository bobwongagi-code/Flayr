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
    stabilize_improvement_priorities,
    stabilize_stage_severity,
)

# MY 市场认证主张专项
from .claims_my import (
    discard_unreferenced_certification_claims,
    reconcile_certification_ownership,
)


def apply_postprocess_chain(normalized: dict[str, Any], analysis: dict[str, Any]) -> None:
    """两个 caller 共享的中段流水线。每一步对应一个独立职责模块。"""
    validate_transcript_attribution(normalized, analysis)                    # validate    跨视频串证据校验
    align_clear_commerce_evidence(normalized)                                # repair      关键词归位 benchmark 事实
    bind_timed_transcript_quotes(normalized, analysis)                       # repair      SRT 时间戳回填 quote
    reconcile_certification_ownership(normalized)                            # claims_my   KKM/认证统一归 S2
    discard_unreferenced_certification_claims(normalized)                    # claims_my   删除未支撑认证主张
    align_timed_cta_from_transcript(normalized, analysis)                    # repair      尾段 CTA 时间对齐
    reconcile_unsupported_cta(normalized)                                    # repair      无 CTA 时补占位 evidence
    downgrade_unverified_sensitive_claims(normalized)                        # repair      未验证敏感主张降级 voice_only
    ground_stage_visual_evidence(normalized)                                 # repair      visual_evidence 对齐 evidence_unit
    deduplicate_stage_quotes(normalized)                                     # repair      跨阶段去重 quote 子句
    materialize_spoken_stage_evidence(normalized)                            # repair      有口播无时段证据时补 stage 占位
    fill_missing_evidence_references(normalized)                             # repair      引用错位时补占位或就近匹配
    derive_product_visibility(normalized, analysis)                          # repair      达人产品出镜标记确定性累加 product_visibility
    stabilize_stage_severity(normalized)                                      # repair      severity 阶段归属漂移校准
    stabilize_improvement_priorities(normalized)                              # repair      Top 改进跟随最终商业判断
