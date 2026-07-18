"""flayr_core.postprocess.repair：对 result data 做修补的纯函数式变换（聚合入口）。

本模块所有函数都是"修改 result data 后正常返回"，不抛 SystemExit，不触发流程终止。
2026-06-15 按职责拆成三个子模块（零跨模块依赖），本文件保留为统一 re-export 入口，
既给现有 `from .repair import X` 调用方零改动，又给后续维护一个一目了然的目录：
  - repair_stages   align_* / stabilize_*（阶段归属与差距等级校准，含背书识别）
  - repair_evidence bind_* / reconcile_* / ground_* / fill_* / materialize_* / deduplicate_*
  - repair_claims   downgrade_* / 产品出镜累加 / 品牌清洗 / 时间归一
"""

from __future__ import annotations

from .repair_claims import (
    allowed_claim_sources,
    bounded_time_range,
    clamp_result_time_ranges,
    derive_product_visibility,
    downgrade_unverified_sensitive_claims,
    local_product_reference,
    remove_unverified_brand_models,
    unit_product_visible,
)
from .repair_evidence import (
    align_stage_flag_evidence,
    bind_improvement_base_material,
    bind_improvement_benchmark_reference,
    bind_timed_transcript_quotes,
    deduplicate_stage_quotes,
    fill_missing_evidence_references,
    ground_improvement_evidence,
    ground_stage_visual_evidence,
    improvement_reference_stage,
    materialize_spoken_stage_evidence,
    normalized_quote_clause,
    reconcile_s3_s4_evidence_coherence,
    reconcile_s5_trust_sources,
    reconcile_unsupported_cta,
    split_quote_clauses,
)
from .repair_stages import (
    apply_comparison_eligibility,
    align_clear_commerce_evidence,
    align_timed_cta_from_transcript,
    creator_has_cta,
    creator_not_worse,
    has_real_endorsement,
    has_hard_endorsement,
    improvement_stage_code,
    role_has_positive_cta,
    role_stage_text,
    repair_s1_hook_boundaries,
    set_stage_small,
    stabilize_improvement_priorities,
    stabilize_stage_severity,
    stage_code,
    stage_text,
)

__all__ = [
    # repair_stages
    "apply_comparison_eligibility",
    "align_clear_commerce_evidence",
    "align_timed_cta_from_transcript",
    "stabilize_stage_severity",
    "stabilize_improvement_priorities",
    "improvement_stage_code",
    "stage_code",
    "stage_text",
    "role_stage_text",
    "role_has_positive_cta",
    "repair_s1_hook_boundaries",
    "creator_not_worse",
    "has_real_endorsement",
    "has_hard_endorsement",
    "creator_has_cta",
    "set_stage_small",
    # repair_evidence
    "bind_timed_transcript_quotes",
    "align_stage_flag_evidence",
    "bind_improvement_benchmark_reference",
    "bind_improvement_base_material",
    "reconcile_s3_s4_evidence_coherence",
    "reconcile_s5_trust_sources",
    "reconcile_unsupported_cta",
    "ground_stage_visual_evidence",
    "ground_improvement_evidence",
    "improvement_reference_stage",
    "fill_missing_evidence_references",
    "materialize_spoken_stage_evidence",
    "deduplicate_stage_quotes",
    "split_quote_clauses",
    "normalized_quote_clause",
    # repair_claims
    "downgrade_unverified_sensitive_claims",
    "derive_product_visibility",
    "unit_product_visible",
    "remove_unverified_brand_models",
    "local_product_reference",
    "allowed_claim_sources",
    "clamp_result_time_ranges",
    "bounded_time_range",
]
