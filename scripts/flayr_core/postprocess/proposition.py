"""flayr_core.postprocess.proposition：产品命题矩阵与跨阶段审计。

本模块只做两类确定性整理：
- 把 Step-0 product_profile/category_profile 与人工 S1 命题合成为 S1-S6 命题矩阵；
- 从已归一的 S1-S6 flags 推导跨阶段状态与绝对质量状态。

这些结果优先作为审计和下游门控输入，不直接替代 LLM 的事实观察。
"""

from __future__ import annotations

import re
from typing import Any


S1_S2_COMPATIBILITY: dict[str, set[str]] = {
    "A": {"A", "C"},
    "B": {"B"},
    "C": {"A", "B"},
    "D": {"A", "D"},
    "E": {"B", "D"},
    "F": {"A", "C"},
    "G": {"A", "D"},
}


def _as_list(value: Any, limit: int = 12) -> list[str]:
    """把字符串/数组归一成去空列表。"""
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        items = []
    out: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _append_unique(target: list[str], values: list[str], limit: int = 12) -> list[str]:
    """追加不重复文本，保持原顺序。"""
    for value in values:
        text = str(value).strip()
        if text and text not in target:
            target.append(text)
        if len(target) >= limit:
            break
    return target


def _compact_text(text: str) -> str:
    """用于中文/本地语混合的保守包含匹配。"""
    return re.sub(r"[\s\W_]+", "", text.lower(), flags=re.UNICODE)


def _matches_any(text: str, anchors: list[str]) -> bool | None:
    """保守判断文本是否触及任一命题锚；无锚点时返回 None。"""
    clean_text = _compact_text(text)
    clean_anchors = [_compact_text(item) for item in anchors if _compact_text(item)]
    if not clean_anchors:
        return None
    return any(anchor in clean_text or clean_text.find(anchor) >= 0 for anchor in clean_anchors)


def _side_stage_text(stage: dict[str, Any], role: str) -> str:
    """拼接某侧报告文本与 flag 理由，供审计匹配。"""
    chunks: list[str] = []
    for key, value in stage.items():
        if key.startswith(role):
            chunks.append(str(value))
    return " ".join(chunks)


def materialize_product_proposition_matrix(result: dict[str, Any], analysis: dict[str, Any] | None) -> None:
    """生成 S1-S6 全阶段命题矩阵，统一暴露横轴。"""
    profile = result.get("product_profile") if isinstance(result.get("product_profile"), dict) else {}
    category = result.get("category_profile") if isinstance(result.get("category_profile"), dict) else {}
    brand = (analysis or {}).get("brand_proposition") if isinstance((analysis or {}).get("brand_proposition"), dict) else {}

    hook_props = _as_list(brand.get("propositions"))
    hook_pains = _as_list(brand.get("painpoints"))
    if not hook_props:
        hook_props = _as_list(profile.get("hook_proposition")) + _as_list(profile.get("physical_task"))
    if not hook_pains:
        hook_pains = _as_list(category.get("painpoints"))

    core_selling_points = _as_list(profile.get("core_selling_points"), limit=8)
    trust_multipliers = _as_list(profile.get("trust_multipliers"), limit=8)
    cta_hooks: list[str] = []
    _append_unique(cta_hooks, _as_list(profile.get("hook_proposition")))
    _append_unique(cta_hooks, _as_list(profile.get("physical_task")))
    _append_unique(cta_hooks, core_selling_points[:4])
    _append_unique(cta_hooks, _as_list(profile.get("core_visual_proposition")))

    result["product_proposition_matrix"] = {
        "source": {
            "s1": "brand_propositions" if brand else "product_foundation",
            "s2_s6": "product_foundation",
        },
        "S1": {
            "hook_propositions": hook_props,
            "painpoints": hook_pains,
        },
        "S2": {
            "handoff_anchor": str(profile.get("hook_proposition") or "").strip(),
            "product_role": str(profile.get("physical_task") or "").strip(),
        },
        "S3": {
            "core_selling_points": core_selling_points,
            "usage_context": str(profile.get("usage_context") or "").strip(),
        },
        "S4": {
            "short_video_proof_plan": profile.get("short_video_proof_plan") if isinstance(profile.get("short_video_proof_plan"), dict) else None,
            "core_visual_proposition": str(profile.get("core_visual_proposition") or "").strip(),
            "visual_proof_points": profile.get("visual_proof_points") if isinstance(profile.get("visual_proof_points"), list) else [],
            "visual_diff_dimensions": _as_list(profile.get("visual_diff_dimensions"), limit=4),
            "proof_mode": str(profile.get("proof_mode") or "").strip(),
            "effect_requires_process": str(profile.get("effect_requires_process") or "").strip(),
        },
        "S5": {
            "trust_multipliers": trust_multipliers,
        },
        "S6": {
            "cta_value_hooks": cta_hooks,
            "decision_threshold": str(category.get("decision_threshold") or "").strip(),
            "drive_type": str(category.get("drive_type") or "").strip(),
        },
    }


def _computed_s1_s2_compatible(hook_type: Any, s2_type: Any) -> bool | None:
    """按结构库 S1→S2 兼容矩阵计算兼容性。"""
    h = str(hook_type or "").strip()
    s = str(s2_type or "").strip()
    if h in {"", "unknown"} or s in {"", "unknown"}:
        return None
    allowed = S1_S2_COMPATIBILITY.get(h)
    if not allowed:
        return None
    return s in allowed


def _valid_s4_output(flag: dict[str, Any] | None) -> bool:
    """S4 是否有可被 S6-D 复用的有效效果输出。"""
    if not isinstance(flag, dict):
        return False
    return (
        flag.get("effect_visible") is True
        and str(flag.get("effect_salience") or "") in {"clear", "strong"}
        and flag.get("effect_proposition_matched") is True
        and flag.get("effect_attribution_supported") is True
        and flag.get("requires_close_inspection") is not True
        and flag.get("tamper_or_cut_risk") is not True
    )


def _selling_point_chain_state(
    s2: dict[str, Any] | None,
    s3: dict[str, Any] | None,
    s4: dict[str, Any] | None,
) -> dict[str, Any]:
    """审计 S2→S4 卖点链，不改变阶段 severity。

    S2 仍只管产品引出；卖点链审计把"产品身份清楚但卖点没有被过程/效果证明"
    单独暴露，避免把 S3/S4 的问题回填成 S2。
    """
    s2_ready = isinstance(s2, dict) and (
        s2.get("product_identity_clear") is True
        and s2.get("product_role_clear") is True
    )
    s3_ready = isinstance(s3, dict) and (
        s3.get("core_selling_point_visible") is True
        and s3.get("process_framing_met") is not False
        and s3.get("mouth_only_or_static") is not True
        and s3.get("result_only_without_process") is not True
    )
    s4_ready = _valid_s4_output(s4)
    if not s2_ready:
        status = "broken_at_s2"
        reason = "产品身份或解决方案角色不清，卖点链无法启动"
    elif not s3_ready and not s4_ready:
        status = "broken_mid_chain"
        reason = "产品已引出，但核心卖点缺少使用过程或效果证明"
    elif not s3_ready:
        status = "weak_process"
        reason = "效果/结果可能存在，但使用过程没有把核心卖点演示成证据"
    elif not s4_ready:
        status = "weak_effect"
        reason = "使用过程成立，但效果证明不足或不够可见"
    else:
        status = "closed"
        reason = "产品身份、卖点演示和效果证明形成闭环"
    return {
        "status": status,
        "s2_ready": s2_ready,
        "s3_core_process_ready": s3_ready,
        "s4_effect_ready": s4_ready,
        "reason": reason,
    }


def materialize_cross_stage_inputs(result: dict[str, Any], analysis: dict[str, Any] | None) -> None:
    """把跨阶段依赖计算成字段，供 derive 消费与报告审计。"""
    materialize_product_proposition_matrix(result, analysis)
    stages = result.get("stage_analysis")
    if not isinstance(stages, list) or len(stages) < 6:
        return

    state: dict[str, Any] = {"roles": {}}
    for role in ("creator", "benchmark"):
        s1 = stages[0].get(f"{role}_hook") if isinstance(stages[0], dict) else None
        s2 = stages[1].get(f"{role}_s2") if isinstance(stages[1], dict) else None
        s3 = stages[2].get(f"{role}_s3") if isinstance(stages[2], dict) else None
        s4 = stages[3].get(f"{role}_s4") if isinstance(stages[3], dict) else None
        s6 = stages[5].get(f"{role}_s6") if isinstance(stages[5], dict) else None

        compat = _computed_s1_s2_compatible(
            (s1 or {}).get("type") if isinstance(s1, dict) else None,
            (s2 or {}).get("module_type") if isinstance(s2, dict) else None,
        )
        if isinstance(s2, dict) and compat is not None:
            s2["computed_s1_s2_compatible"] = compat

        s4_available = _valid_s4_output(s4 if isinstance(s4, dict) else None)
        if isinstance(s6, dict) and s6.get("module_type") == "D":
            s6["computed_depends_on_valid_s4"] = s4_available

        state["roles"][role] = {
            "resolved_s1_hook_type": (s1 or {}).get("type") if isinstance(s1, dict) else "unknown",
            "resolved_hook_anchor": (s1 or {}).get("anchors_proposition") if isinstance(s1, dict) else None,
            "resolved_s2_role": (s2 or {}).get("module_type") if isinstance(s2, dict) else "unknown",
            "computed_s1_s2_compatible": compat,
            "resolved_core_selling_points_shown": (s3 or {}).get("demonstrated_selling_points") if isinstance(s3, dict) else [],
            "resolved_s4_effect_validity": s4_available,
            "s4_output_available": s4_available,
            "selling_point_chain": _selling_point_chain_state(
                s2 if isinstance(s2, dict) else None,
                s3 if isinstance(s3, dict) else None,
                s4 if isinstance(s4, dict) else None,
            ),
        }
    result["cross_stage_state"] = state


def _absolute_status(stage_id: str, flag: dict[str, Any] | None) -> tuple[str, str]:
    """把单侧 flag 转成绝对质量状态；不与标杆比较。"""
    if not isinstance(flag, dict):
        return "unknown", "缺少结构化 flag"
    if stage_id == "S1":
        if flag.get("exists") is False:
            return "missing", "未完成 Hook"
        if flag.get("landing_met") is False:
            return "not_landed", "Hook 结构存在但未打穿"
        if flag.get("anchors_proposition") is False:
            return "generic", "Hook 未锚定本品命题/痛点"
        return "complete", "Hook 完成本品留人功能"
    if stage_id == "S2":
        if flag.get("exists") is False:
            return "missing", "未完成产品引出"
        if flag.get("handoff_met") is not True:
            return "weak", "未自然承接 S1"
        if flag.get("product_identity_clear") is not True or flag.get("product_role_clear") is not True:
            return "weak", "产品身份或解决方案角色不清"
        if flag.get("computed_s1_s2_compatible") is False:
            return "incompatible", "S1/S2 模块组合不符合结构库矩阵"
        return "complete", "产品引出契约完成"
    if stage_id == "S3":
        if flag.get("exists") is False:
            return "missing", "未展示使用过程"
        if flag.get("mouth_only_or_static") is True:
            return "weak", "只口播或静态展示"
        if flag.get("result_only_without_process") is True:
            return "weak", "只有结果没有过程"
        if flag.get("core_selling_point_visible") is not True:
            return "weak", "核心卖点未在动作中可见"
        return "complete", "使用过程证明了核心卖点"
    if stage_id == "S4":
        if flag.get("effect_visible") is False or str(flag.get("effect_salience") or "") == "none":
            return "missing", "未呈现可见效果"
        if flag.get("effect_proposition_matched") is not True:
            return "weak", "效果未命中本品视觉命题"
        if flag.get("effect_attribution_supported") is not True:
            return "weak", "效果归因不足"
        return "complete", "效果可见且命中本品命题"
    if stage_id == "S5":
        if flag.get("exists") is False:
            return "not_applicable", "未设置独立信任环节"
        if flag.get("duplicates_other_stage") is True:
            return "duplicate", "信任材料重复计入其他阶段"
        if flag.get("risky_or_unsupported") is True:
            return "risky", "信任主张存在未支撑风险"
        if flag.get("voice_only") is True:
            return "weak", "信任点只有口播无画面佐证"
        if str(flag.get("trust_evidence_type") or "") == "soft":
            return "soft_trust", "软信任成立但不等同硬背书"
        return "complete", "独立信任材料成立"
    if stage_id == "S6":
        if flag.get("exists") is False:
            return "missing", "未完成结尾 CTA"
        if flag.get("ending_position_met") is not True:
            return "misplaced", "CTA 不在结尾促单位置"
        if flag.get("direct_order_met") is not True and flag.get("action_path_clear") is not True:
            return "weak", "缺少明确购买指令或路径"
        if flag.get("module_type") == "D" and flag.get("computed_depends_on_valid_s4", flag.get("depends_on_valid_s4")) is False:
            return "weak", "效果总结型 CTA 缺少有效 S4 输出支撑"
        return "complete", "结尾购买动作成立"
    return "unknown", "未知阶段"


def materialize_quality_audits(result: dict[str, Any], analysis: dict[str, Any] | None) -> None:
    """补充绝对质量状态与 S5/S6 命题审计，不覆盖 gap severity。"""
    matrix = result.get("product_proposition_matrix") if isinstance(result.get("product_proposition_matrix"), dict) else {}
    stages = result.get("stage_analysis")
    if not isinstance(stages, list):
        return

    stage_flags = {
        "S1": "hook",
        "S2": "s2",
        "S3": "s3",
        "S4": "s4",
        "S5": "s5",
        "S6": "s6",
    }
    absolute: dict[str, Any] = {}
    for index, stage in enumerate(stages, start=1):
        if not isinstance(stage, dict):
            continue
        stage_id = f"S{index}"
        suffix = stage_flags.get(stage_id)
        if not suffix:
            continue
        absolute[stage_id] = {}
        for role in ("creator", "benchmark"):
            flag = stage.get(f"{role}_{suffix}")
            status, reason = _absolute_status(stage_id, flag if isinstance(flag, dict) else None)
            stage[f"{role}_absolute_status"] = status
            stage[f"{role}_absolute_reason"] = reason
            absolute[stage_id][role] = {"status": status, "reason": reason}

    if len(stages) >= 5 and isinstance(stages[4], dict):
        anchors = ((matrix.get("S5") or {}).get("trust_multipliers") or []) if isinstance(matrix.get("S5"), dict) else []
        audit: dict[str, Any] = {}
        for role in ("creator", "benchmark"):
            stage = stages[4]
            flag = stage.get(f"{role}_s5")
            text = _side_stage_text(stage, role)
            matched = _matches_any(text, anchors)
            if isinstance(flag, dict) and flag.get("product_relevance_met") is True:
                matched = True
            audit[role] = {
                "trust_anchor_matched": matched,
                "duplicate_stage_source": "other_stage" if isinstance(flag, dict) and flag.get("duplicates_other_stage") is True else "none",
                "absolute_missing_reason": stage.get(f"{role}_absolute_reason") if stage.get(f"{role}_absolute_status") in {"missing", "not_applicable"} else "",
            }
        stages[4]["trust_anchor_audit"] = audit

    if len(stages) >= 6 and isinstance(stages[5], dict):
        anchors = ((matrix.get("S6") or {}).get("cta_value_hooks") or []) if isinstance(matrix.get("S6"), dict) else []
        audit = {}
        for role in ("creator", "benchmark"):
            stage = stages[5]
            flag = stage.get(f"{role}_s6")
            text = _side_stage_text(stage, role)
            matched = _matches_any(text, anchors)
            if isinstance(flag, dict) and (flag.get("product_value_recalled") is True or flag.get("module_fit_met") is True):
                matched = True
            audit[role] = {
                "cta_anchor_matched": matched,
                "absolute_missing_reason": stage.get(f"{role}_absolute_reason") if stage.get(f"{role}_absolute_status") == "missing" else "",
            }
        stages[5]["cta_anchor_audit"] = audit
    result["absolute_quality"] = absolute
