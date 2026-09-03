"""Normalization for product identity and comparison eligibility contracts.

The public ``parse`` module re-exports this module's functions and constants.
The small runtime normalizer bridge keeps callers that historically patched
``llm.parse.normalize_*`` working after the structural split.
"""

from __future__ import annotations

import sys
from typing import Any

from .normalization_primitives import normalize_choice, normalize_demo_flag, normalize_evidence


_IDENTITY_BASIS = {"visible", "spoken", "subtitle", "mixed", "unknown"}
_IDENTITY_CONFIDENCE = {"high", "medium", "low"}
_COMPARISON_SCOPES = {
    "same_product", "comparable_variant", "same_task_structure", "creative_reference_only", "cross_product", "uncertain",
}
_COMPARISON_SCOPE_ORIGINS = {"facts", "operator_certified"}
_IDENTITY_RELATIONS = {"exact_product", "same_product_family", "different_product", "uncertain"}
_SUBSTITUTION_RELATIONS = {"same_solution", "strong_substitute", "partial_substitute", "none", "uncertain"}
_STAGE_COMPARISON_STATUSES = {"direct", "structural", "not_applicable", "not_comparable"}
_ALL_STAGE_CODES = ("S1", "S2", "S3", "S4", "S5", "S6")
_S5_FACT_SCOPE_SOURCE = "bilateral_stage1_facts"


def _facade_normalizer(name: str, fallback: Any) -> Any:
    """Resolve a legacy facade helper when callers monkeypatch it."""
    facade = sys.modules.get(f"{__package__}.parse")
    return getattr(facade, name, fallback) if facade is not None else fallback


def _runtime_normalize_evidence(value: Any) -> list[str]:
    return _facade_normalizer("normalize_evidence", normalize_evidence)(value)


def _runtime_normalize_demo_flag(value: Any) -> bool | None:
    return _facade_normalizer("normalize_demo_flag", normalize_demo_flag)(value)


def _runtime_normalize_choice(value: Any, allowed: set[str], fallback: str) -> str:
    return _facade_normalizer("normalize_choice", normalize_choice)(value, allowed, fallback)


def normalize_video_product_identity(value: Any) -> dict[str, Any]:
    """归一单视频中实际观察到的产品身份，不允许用声明产品补空。"""
    value = value if isinstance(value, dict) else {}
    return {
        "brand_or_product_name": str(value.get("brand_or_product_name") or "").strip(),
        "brand": str(value.get("brand") or "").strip(),
        "product_line": str(value.get("product_line") or "").strip(),
        "product_category": str(value.get("product_category") or "").strip(),
        "functional_form": str(value.get("functional_form") or value.get("form_factor") or "").strip(),
        "form_factor": str(value.get("functional_form") or value.get("form_factor") or "").strip(),
        "variant_attributes": _runtime_normalize_evidence(value.get("variant_attributes")),
        "core_job": str(value.get("core_job") or "").strip(),
        "target_object": str(value.get("target_object") or "").strip(),
        "use_mechanism": str(value.get("use_mechanism") or "").strip(),
        "desired_outcome": str(value.get("desired_outcome") or "").strip(),
        "identity_basis": _runtime_normalize_choice(value.get("identity_basis"), _IDENTITY_BASIS, "unknown"),
        "confidence": _runtime_normalize_choice(value.get("confidence"), _IDENTITY_CONFIDENCE, "low"),
    }


def normalize_comparison_contract(
    value: Any,
    *,
    allow_code_owned_s5_scope: bool = False,
) -> dict[str, Any]:
    """归一商品关系与阶段级可比合同，并由合同派生旧 scope 兼容视图。

    ``status_source=bilateral_stage1_facts`` is a code-owned assertion. Raw
    provider or external results cannot carry it through normalization; only
    postprocess/report replay paths that already trust the frozen bilateral
    Stage1 fact matrix may opt in explicitly.
    """
    value = value if isinstance(value, dict) else {}
    legacy_scope = _runtime_normalize_choice(value.get("scope"), _COMPARISON_SCOPES, "uncertain")
    identity_relation = _runtime_normalize_choice(value.get("identity_relation"), _IDENTITY_RELATIONS, "uncertain")
    substitution_relation = _runtime_normalize_choice(value.get("substitution_relation"), _SUBSTITUTION_RELATIONS, "uncertain")

    # 旧结果迁移：只恢复旧 scope 能明确表达的语义，绝不从包装文字猜商品关系。
    if identity_relation == "uncertain":
        if legacy_scope == "same_product":
            identity_relation, substitution_relation = "exact_product", "same_solution"
        elif legacy_scope == "comparable_variant":
            identity_relation, substitution_relation = "same_product_family", "same_solution"
        elif legacy_scope in {"same_task_structure", "creative_reference_only"}:
            identity_relation = "different_product"
            substitution_relation = "strong_substitute" if legacy_scope == "same_task_structure" else "partial_substitute"
        elif legacy_scope == "cross_product":
            identity_relation, substitution_relation = "different_product", "none"

    shared = value.get("shared_job") if isinstance(value.get("shared_job"), dict) else {}
    shared_job = {
        "same_consumer_job": _runtime_normalize_demo_flag(shared.get("same_consumer_job")),
        "same_target_object": _runtime_normalize_demo_flag(shared.get("same_target_object")),
        "same_desired_outcome": _runtime_normalize_demo_flag(shared.get("same_desired_outcome")),
        "same_purchase_decision": _runtime_normalize_demo_flag(shared.get("same_purchase_decision")),
        "complement_or_dependency": _runtime_normalize_demo_flag(shared.get("complement_or_dependency")),
        "reason": str(shared.get("reason") or "").strip(),
        "evidence_ids": _runtime_normalize_evidence(shared.get("evidence_ids")),
    }

    # 兼容旧的人工 same_task_structure 结果：该 scope 本身代表运营已确认共同任务，
    # 旧文件没有 shared_job 字段时补回其原有语义，避免迁移后被误降级。
    if legacy_scope == "same_task_structure" and not shared:
        shared_job.update(
            {
                "same_consumer_job": True,
                "same_target_object": True,
                "same_desired_outcome": True,
                "same_purchase_decision": True,
                "complement_or_dependency": False,
                "reason": "由旧版人工确认的同任务结构对标迁移。",
            }
        )

    if identity_relation in {"exact_product", "same_product_family"}:
        substitution_relation = "same_solution"
    elif identity_relation == "different_product" and substitution_relation == "strong_substitute":
        hard_gates = (
            shared_job["same_consumer_job"] is True,
            shared_job["same_target_object"] is True,
            shared_job["same_desired_outcome"] is True,
            shared_job["same_purchase_decision"] is True,
            shared_job["complement_or_dependency"] is False,
        )
        if not all(hard_gates):
            substitution_relation = "partial_substitute"

    raw_stage_eligibility = value.get("stage_eligibility") if isinstance(value.get("stage_eligibility"), dict) else {}
    legacy_stages = {
        str(item or "").upper().strip()
        for item in value.get("direct_product_stages", [])
        if str(item or "").upper().strip() in _ALL_STAGE_CODES
    }
    stage_eligibility: dict[str, dict[str, Any]] = {}
    for stage in _ALL_STAGE_CODES:
        raw = raw_stage_eligibility.get(stage) if isinstance(raw_stage_eligibility.get(stage), dict) else {}
        raw_status = _runtime_normalize_choice(raw.get("status"), _STAGE_COMPARISON_STATUSES, "not_comparable")
        status = raw_status
        raw_basis = str(raw.get("basis") or "").strip()
        raw_s5_scope_source = (
            str(raw.get("status_source") or "").strip().lower()
            if stage == "S5"
            else ""
        )
        s5_scope_source = raw_s5_scope_source if allow_code_owned_s5_scope else ""
        if not raw and stage in legacy_stages:
            status = "direct" if identity_relation in {"exact_product", "same_product_family"} else "structural"
        if identity_relation in {"exact_product", "same_product_family"}:
            # Preserve the only sanctioned S5 closure through a later
            # round-trip.  Other provider/category not_applicable values are
            # reopened below rather than becoming a hidden prior.
            status = (
                "not_applicable"
                if stage == "S5"
                and raw_status == "not_applicable"
                and s5_scope_source == _S5_FACT_SCOPE_SOURCE
                else "direct"
            )
        elif identity_relation == "uncertain" or substitution_relation in {"none", "uncertain"}:
            status = "not_comparable"
        elif status == "direct":
            status = "structural"
        if stage == "S5" and status == "not_applicable" and s5_scope_source != _S5_FACT_SCOPE_SOURCE:
            # A provider/category prior cannot close S5.  Only the postprocess
            # may write this status after bilateral Stage1 absence is closed.
            if identity_relation in {"exact_product", "same_product_family"}:
                status = "direct"
            elif substitution_relation in {"strong_substitute", "partial_substitute"}:
                status = "structural"
            else:
                status = "not_comparable"
        if (
            identity_relation in {"exact_product", "same_product_family"}
            and status == "direct"
            and raw_status != "direct"
        ):
            # Product relationship and Stage1 evidence readiness are separate
            # axes.  A provider may conservatively report ``not_comparable``
            # because a stage has unknown evidence, but that must not survive
            # as the basis text after code-owned scope normalization says the
            # same product is directly comparable.  The evidence gate carries
            # the unresolved state downstream; this contract records only the
            # product-level scope.
            raw_basis = "产品关系允许该阶段直接比较；Stage1 证据资格由独立证据门禁判断。"
        stage_eligibility[stage] = {
            "status": status,
            "basis": raw_basis,
            "shared_contract": str(raw.get("shared_contract") or "").strip(),
            "restrictions": _runtime_normalize_evidence(raw.get("restrictions")),
            "evidence_ids": _runtime_normalize_evidence(raw.get("evidence_ids")),
        }
        if (
            stage == "S5"
            and status == "not_applicable"
            and s5_scope_source == _S5_FACT_SCOPE_SOURCE
        ):
            stage_eligibility[stage]["status_source"] = s5_scope_source

    comparable_stages = [
        stage for stage in _ALL_STAGE_CODES
        if stage_eligibility[stage]["status"] in {"direct", "structural"}
    ]
    if identity_relation in {"exact_product", "same_product_family"}:
        overall_status = "full_direct"
    elif identity_relation == "uncertain" or substitution_relation == "uncertain":
        overall_status = "uncertain"
    elif not comparable_stages:
        overall_status = "not_comparable"
    else:
        overall_status = "selective_structural"

    if identity_relation == "exact_product":
        scope = "same_product"
    elif identity_relation == "same_product_family":
        scope = "comparable_variant"
    elif substitution_relation == "strong_substitute":
        scope = "same_task_structure"
    elif substitution_relation == "partial_substitute":
        scope = "creative_reference_only"
    elif identity_relation == "different_product":
        scope = "cross_product"
    else:
        scope = "uncertain"

    return {
        "identity_relation": identity_relation,
        "substitution_relation": substitution_relation,
        "shared_job": shared_job,
        "stage_eligibility": stage_eligibility,
        "overall_status": overall_status,
        "comparable_stages": comparable_stages,
        "scope": scope,
        "direct_product_stages": comparable_stages,
        "reason": str(value.get("reason") or "").strip(),
        "scope_origin": _runtime_normalize_choice(value.get("scope_origin"), _COMPARISON_SCOPE_ORIGINS, "facts"),
        "facts_scope": _runtime_normalize_choice(value.get("facts_scope"), _COMPARISON_SCOPES, "uncertain"),
        "facts_reason": str(value.get("facts_reason") or "").strip(),
        "evidence_ids": _runtime_normalize_evidence(value.get("evidence_ids")),
        "confidence": _runtime_normalize_choice(value.get("confidence"), _IDENTITY_CONFIDENCE, "low"),
    }


def normalize_comparison_eligibility(value: Any) -> dict[str, Any]:
    """旧字段兼容入口；返回同一份三层合同，scope 仅为派生视图。"""
    return _facade_normalizer("normalize_comparison_contract", normalize_comparison_contract)(value)


__all__ = (
    "_IDENTITY_BASIS",
    "_IDENTITY_CONFIDENCE",
    "_COMPARISON_SCOPES",
    "_COMPARISON_SCOPE_ORIGINS",
    "_IDENTITY_RELATIONS",
    "_SUBSTITUTION_RELATIONS",
    "_STAGE_COMPARISON_STATUSES",
    "_ALL_STAGE_CODES",
    "_S5_FACT_SCOPE_SOURCE",
    "normalize_video_product_identity",
    "normalize_comparison_contract",
    "normalize_comparison_eligibility",
)
