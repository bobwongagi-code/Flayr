"""Deterministic Stage2 projection into the canonical report-compatible shape.

This module owns no model calls. It converts locked Stage1 evidence and small
Stage2 semantic judgments into code-owned handoff fields.
"""

from __future__ import annotations

import copy
import math
import re
from typing import Any

from ..artifacts import format_seconds, parse_time_range_seconds
from ..stage_evidence_contracts import qualified_stage_evidence_ids, stage_evidence_readiness


_SEGMENTED_STAGE_NAMES = {
    "S1": "S1 Hook",
    "S2": "S2 产品引出",
    "S3": "S3 使用过程",
    "S4": "S4 效果呈现",
    "S5": "S5 信任放大",
    "S6": "S6 CTA",
}
_SEGMENTED_STAGE_QUESTIONS = {
    "S1": "用户凭什么停下来",
    "S2": "产品是否自然承接并成为解决方案",
    "S3": "使用过程是否把核心卖点演示出来",
    "S4": "目标效果是否被清楚且可信地证明",
    "S5": "信任材料是否真实、相关且可追溯",
    "S6": "用户是否知道下一步如何购买或行动",
}
_SEGMENTED_FLAG_REQUIRED_KEYS = {
    "S1": ("exists", "type", "dims", "landing_met", "landing_reason", "window_evidence", "hook_boundary_seconds", "hook_boundary_reason", "s2_start_signal", "landing_window_leak", "anchors_proposition"),
    "S2": ("exists", "merged_with_s3", "module_type", "handoff_met", "s1_s2_compatible", "product_identity_clear", "product_role_clear", "excluded_or_risky_module", "start_seconds", "end_seconds", "handoff_reason"),
    "S3": ("exists", "module_type", "usage_evidence_state", "usage_process_visible", "result_only_without_process", "mouth_only_or_static", "real_usage_met", "core_selling_point_visible", "process_framing_met", "action_proof_met", "action_target_contact_met", "action_application_change_visible", "critical_action_continuity_met", "scene_mode", "usage_context_fit", "continuity_met", "richness_met", "single_scene_continuity_met", "single_scene_variation_met", "multi_scene_logic_met", "multi_scene_transition_met", "multi_scene_role_adaptation_met", "role_design_met", "role_interaction_met", "distinct_personas_met", "steps_clear_met", "pov_immersive_met", "fake_or_staged", "start_seconds", "end_seconds", "usage_reason"),
    "S4": ("effect_type", "effect_evidence_state", "effect_visible", "effect_salience", "effect_proposition_matched", "comparison_control_met", "closeup_or_focus_met", "visual_difference_observed", "module_constraints_met", "effect_maximized", "requires_close_inspection", "effect_attribution_supported", "result_only_without_process", "process_linked_effect", "tamper_or_cut_risk", "effect_reason"),
    "S5": ("exists", "module_type", "trust_evidence_type", "trust_basis", "trust_source_visible", "trust_source_credible", "trust_claim_specific", "product_relevance_met", "independent_trust_purpose", "duplicates_other_stage", "voice_only", "risky_or_unsupported", "start_seconds", "end_seconds", "trust_reason"),
    "S6": ("exists", "module_type", "direct_order_met", "action_path_clear", "soft_purchase_invitation_met", "offer_or_incentive_clear", "price_anchor_met", "urgency_evidence_met", "gift_stack_met", "guarantee_clear_met", "urgency_met", "product_value_recalled", "module_fit_met", "ending_position_met", "depends_on_valid_s4", "compliance_risk", "start_seconds", "end_seconds", "cta_reason"),
}


def _segmented_stage_code(value: Any) -> str:
    match = re.search(r"S[1-6]", str(value or "").upper())
    return match.group(0) if match else ""


def _segmented_text_items(value: Any, limit: int = 5) -> list[str]:
    """Normalize bounded model text fields without iterating string characters."""
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
    elif str(value or "").strip():
        items = [str(value).strip()]
    else:
        items = []
    return items[:limit]


def _segmented_evidence_range(facts: dict[str, Any], role: str, stage: str, ids: list[str]) -> str:
    side = facts.get(role) if isinstance(facts.get(role), dict) else {}
    units = {
        str(unit.get("id") or ""): unit
        for unit in side.get("evidence_units") or []
        if isinstance(unit, dict)
    }
    parsed = [
        parse_time_range_seconds(units[item].get("time_range"), None)
        for item in ids
        if item in units
    ]
    parsed = [item for item in parsed if item is not None]
    if not parsed:
        return ""
    return f"{format_seconds(min(item[0] for item in parsed))} - {format_seconds(max(item[1] for item in parsed))}"


def _segmented_qualified_units(
    facts: dict[str, Any],
    role: str,
    stage: str,
    ids: list[str],
) -> list[dict[str, Any]]:
    side = facts.get(role) if isinstance(facts.get(role), dict) else {}
    units = {
        str(unit.get("id") or ""): unit
        for unit in side.get("evidence_units") or []
        if isinstance(unit, dict) and str(unit.get("id") or "").strip()
    }
    qualified = qualified_stage_evidence_ids(side, stage)
    return [
        units[evidence_id]
        for evidence_id in ids
        if evidence_id in qualified and evidence_id in units
    ]


def _segmented_side_summary(units: list[dict[str, Any]], role: str, readiness: str) -> str:
    if not units:
        if readiness == "absent":
            return f"{role}该阶段已完成观察，未发现合同要求的明确证据。"
        if readiness == "unknown":
            return f"{role}该阶段证据资格未知，暂不形成正式结论。"
        if readiness == "conflict":
            return f"{role}该阶段证据存在冲突，暂不形成正式结论。"
        return f"{role}该阶段没有可交接的资格化证据。"
    texts: list[str] = []
    for unit in units:
        text = next(
            (
                str(unit.get(field) or "").strip()
                for field in ("information", "visual_fact", "voiceover_zh", "subtitle_fact")
                if str(unit.get(field) or "").strip()
            ),
            "",
        )
        if text and text not in texts:
            texts.append(text)
    return "；".join(texts[:3]) or f"{role}该阶段已锁定证据，但缺少可展示的文字摘要。"


def _segmented_support_status(units: list[dict[str, Any]]) -> str:
    has_visual = any(str(unit.get("visual_fact") or "").strip() for unit in units)
    has_voice = any(
        str(unit.get(field) or "").strip()
        for unit in units
        for field in ("voiceover", "voiceover_zh", "subtitle_fact")
    )
    if has_visual and has_voice:
        return "supported"
    if has_voice:
        return "voice_only"
    if has_visual:
        return "visual_only"
    return "unknown"


def _sanitize_segmented_flag(value: Any, qualified_ids: set[str]) -> Any:
    """Keep semantic flags but strip any unqualified nested evidence IDs."""
    if isinstance(value, dict):
        return {
            key: (
                [str(item).strip() for item in nested if str(item).strip() in qualified_ids]
                if key == "evidence_ids" and isinstance(nested, list)
                else _sanitize_segmented_flag(nested, qualified_ids)
            )
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_segmented_flag(item, qualified_ids) for item in value]
    return value


def _segmented_model_evidence_ids(raw: dict[str, Any], role: str) -> list[str]:
    """Read only explicit Stage2 evidence-ID fields, including legacy nesting.

    Some compatible providers return the two role judgments as nested
    ``benchmark``/``creator`` objects even when the current compact contract
    asks for top-level ``*_evidence_ids``.  The nested alias is still an
    explicit structured field; free-text reasons are intentionally ignored.
    """
    candidates: list[Any] = [raw.get(f"{role}_evidence_ids")]
    nested = raw.get(role)
    if isinstance(nested, dict):
        candidates.append(nested.get("evidence_ids"))
    for key, value in raw.items():
        if str(key).startswith(f"{role}_") and isinstance(value, dict):
            candidates.append(value.get("evidence_ids"))
    ids: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, list):
            continue
        ids.extend(str(item).strip() for item in candidate if str(item).strip())
    return list(dict.fromkeys(ids))


def _segmented_complete_flag(value: Any, stage: str) -> bool:
    if not isinstance(value, dict):
        return False
    required = _SEGMENTED_FLAG_REQUIRED_KEYS.get(stage, ())
    if any(key not in value for key in required):
        return False
    if stage == "S1" and not isinstance(value.get("dims"), dict):
        return False
    return True


def _project_segmented_role_evidence(
    output: dict[str, Any],
    raw: dict[str, Any],
    facts: dict[str, Any],
    stage: str,
    role: str,
    *,
    scope_closed: bool,
) -> tuple[list[str], str]:
    side = facts.get(role) if isinstance(facts.get(role), dict) else {}
    qualified = qualified_stage_evidence_ids(side, stage)
    ids = [item for item in _segmented_model_evidence_ids(raw, role) if item in qualified]
    readiness = stage_evidence_readiness(side, stage)
    if readiness != "present":
        ids = []
    elif scope_closed:
        # Closed scopes still expose the locked ledger for audit without
        # depending on the model to repeat IDs for a stage it did not judge.
        ids = sorted(qualified)
    ids = list(dict.fromkeys(ids))
    units = _segmented_qualified_units(facts, role, stage, ids)
    output[f"{role}_evidence_ids"] = ids
    output[f"{role}_time_range"] = _segmented_evidence_range(facts, role, stage, ids)
    output[f"{role}_summary"] = _segmented_side_summary(units, role, readiness)
    output[f"{role}_key_message"] = output[f"{role}_summary"]
    output[f"{role}_visual_evidence"] = [
        str(unit.get("visual_fact") or "").strip()
        for unit in units
        if str(unit.get("visual_fact") or "").strip()
    ]
    output[f"{role}_support_status"] = _segmented_support_status(units)
    output[f"{role}_quote"] = next(
        (
            str(unit.get("voiceover") or "").strip()
            for unit in units
            if str(unit.get("voiceover") or "").strip()
        ),
        "",
    )
    output[f"{role}_quote_zh"] = next(
        (
            str(unit.get("voiceover_zh") or "").strip()
            for unit in units
            if str(unit.get("voiceover_zh") or "").strip()
        ),
        "",
    )
    return ids, readiness


def _apply_segmented_handoff_state(
    output: dict[str, Any],
    raw: dict[str, Any],
    readiness: dict[str, str],
    role_ids: dict[str, list[str]],
    *,
    scope_closed: bool,
    comparison_status: str,
) -> None:
    missing_model_references = not scope_closed and any(
        readiness[role] == "present" and not role_ids[role]
        for role in ("benchmark", "creator")
    )
    if not scope_closed and set(readiness.values()) == {"absent"}:
        scope_closed = True
        comparison_status = "not_applicable"
        output["comparison_status"] = "not_applicable"
        output["model_comparison_status"] = "not_applicable"
        output["judgment_reason"] = "双方 Stage1 均完整确认未执行本阶段功能；本轮不涉及，不生成阶段差距。"
    if scope_closed:
        output["relation"] = "uncertain"
        output["model_gap_magnitude"] = "uncertain"
        output["stage_state"] = "unknown"
        output["stage_handoff_status"] = (
            "not_applicable" if comparison_status == "not_applicable" else "not_comparable"
        )
        return
    if missing_model_references:
        output["relation"] = "uncertain"
        output["model_gap_magnitude"] = "uncertain"
        output["stage_state"] = "unknown"
        output["judgment_reason"] = (
            str(raw.get("judgment_reason") or "").strip()
            or "Stage2 未返回可核验的阶段证据 ID，未将候选事实升级为正式引用。"
        )
        output["stage_handoff_status"] = "handoff_loss"
        return
    if any(value not in {"present", "absent"} for value in readiness.values()):
        output["relation"] = "uncertain"
        output["model_gap_magnitude"] = "uncertain"
        output["stage_state"] = "unknown" if "conflict" not in readiness.values() else "conflict"
        output["judgment_reason"] = (
            str(raw.get("judgment_reason") or "").strip()
            or f"Stage1 资格未闭合：benchmark={readiness['benchmark']}，creator={readiness['creator']}。"
        )
        output["stage_handoff_status"] = "evidence_blocked"
        return

    stage_state = str(raw.get("stage_state") or "unknown").strip().lower()
    output["stage_state"] = stage_state if stage_state in {"completed", "unknown", "conflict", "blocked"} else "unknown"
    output["judgment_reason"] = str(raw.get("judgment_reason") or raw.get("reason") or "").strip()
    if output["stage_state"] != "completed":
        output["relation"] = "uncertain"
        output["model_gap_magnitude"] = "uncertain"
        output["stage_handoff_status"] = "unknown" if output["stage_state"] == "unknown" else "evidence_blocked"
        return
    relation = str(raw.get("relation") or "").strip().lower()
    output["relation"] = relation if relation in {"creator_better", "benchmark_better", "equivalent", "uncertain"} else "uncertain"
    magnitude = str(raw.get("model_gap_magnitude") or "").strip().lower()
    output["model_gap_magnitude"] = magnitude if magnitude in {"none", "small", "medium", "large", "uncertain"} else "uncertain"
    output["stage_handoff_status"] = "grounded"


def _attach_segmented_compatibility_fields(
    output: dict[str, Any],
    raw: dict[str, Any],
) -> None:
    raw_gap_type = str(raw.get("gap_type") or "").strip().lower()
    output["gap_type"] = (
        raw_gap_type
        if output["stage_state"] == "completed" and raw_gap_type in {"structural", "execution", "resource", "unknown"}
        else "unknown"
    )
    output["gap_summary"] = [output["judgment_reason"] or "待基于阶段证据复核。"]
    output["evidence"] = [output["judgment_reason"] or "阶段证据由代码交接。"]
    output["gap"] = output["judgment_reason"] or "阶段差距待复核。"
    output["time_range"] = (
        f"标杆 {output.get('benchmark_time_range') or '待复核'} / "
        f"达人 {output.get('creator_time_range') or '待复核'}"
    )
    output["model_severity"] = (
        output["model_gap_magnitude"]
        if output["model_gap_magnitude"] in {"small", "medium", "large"}
        else None
    )
    output["severity"] = output["model_severity"]
    output.update(
        {
            "creator_module_id": "unknown",
            "benchmark_module_id": "unknown",
            "module_fit": "unknown",
            "module_fit_reason": output["judgment_reason"],
            "task_completion": None,
            "voice_performance": {
                "pace": "unknown",
                "energy": "unknown",
                "key_pause": None,
                "note": "由阶段证据交接。",
            },
            "benchmark_execution": None,
            "creator_execution": None,
            "painpoint_relevance": None,
            "stage_standard_delivery": "unknown",
        }
    )


def _normalize_segmented_stage(
    raw: dict[str, Any],
    stage: str,
    facts: dict[str, Any],
    comparison_eligibility: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project a small Stage2 judgment into the legacy report shape.

    Only semantic judgment fields and complete stage-specific flags are read
    from the model. Summaries, quotes, support status, ranges, and references
    are rebuilt from the qualified Stage1 ledger so a prompt violation cannot
    reclaim a code-owned field.
    """
    stage_contract = (
        comparison_eligibility.get("stage_eligibility", {}).get(stage)
        if isinstance(comparison_eligibility, dict)
        and isinstance(comparison_eligibility.get("stage_eligibility"), dict)
        and isinstance(comparison_eligibility.get("stage_eligibility", {}).get(stage), dict)
        else {}
    )
    comparison_status = str(stage_contract.get("status") or "").strip().lower()
    scope_closed = comparison_status in {"not_comparable", "not_applicable"}
    output: dict[str, Any] = {
        "stage": _SEGMENTED_STAGE_NAMES[stage],
        "core_question": _SEGMENTED_STAGE_QUESTIONS[stage],
        "stage_state": "unknown",
        "relation": "uncertain",
        "model_gap_magnitude": "uncertain",
        "judgment_reason": str(raw.get("judgment_reason") or raw.get("reason") or "").strip(),
    }
    if scope_closed:
        output["comparison_status"] = (
            "not_applicable" if comparison_status == "not_applicable" else "not_directly_comparable"
        )
        output["model_comparison_status"] = comparison_status
        output["judgment_reason"] = (
            output["judgment_reason"]
            or str(stage_contract.get("basis") or "该阶段不在当前比较合同范围内。").strip()
        )
    role_ids: dict[str, list[str]] = {}
    readiness: dict[str, str] = {}
    for role in ("benchmark", "creator"):
        role_ids[role], readiness[role] = _project_segmented_role_evidence(
            output,
            raw,
            facts,
            stage,
            role,
            scope_closed=scope_closed,
        )
    _apply_segmented_handoff_state(
        output,
        raw,
        readiness,
        role_ids,
        scope_closed=scope_closed,
        comparison_status=comparison_status,
    )
    _attach_segmented_compatibility_fields(output, raw)

    # A nested structured flag is accepted only as a complete object. Partial
    # semantic objects are worse than an explicit unknown because the existing
    # resolver/validators would otherwise mistake omitted booleans for facts.
    if output.get("stage_handoff_status") == "grounded":
        for role in ("benchmark", "creator"):
            key = f"{role}_{stage.lower() if stage != 'S1' else 'hook'}"
            value = raw.get(key)
            if _segmented_complete_flag(value, stage):
                output[key] = _sanitize_segmented_flag(value, set(role_ids[role]))
    return output


def _deterministic_product_visibility(facts: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    """Project product visibility from immutable creator facts, without LLM estimation."""
    side = facts.get("creator") if isinstance(facts.get("creator"), dict) else {}
    raw_duration = (analysis.get("videos", {}).get("creator", {}) or {}).get("duration_seconds")
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError):
        duration = 0.0
    if not math.isfinite(duration) or duration <= 0:
        return {
            "first_appearance_sec": None,
            "total_screen_time_sec": None,
            "video_duration_sec": None,
            "ratio": None,
            "estimation_note": "达人视频时长缺失或无效，无法从时间区间计算产品出镜统计，需人工复核。",
        }
    visibility_observed = any(
        isinstance(unit, dict)
        and (unit.get("product_visible") is True or unit.get("product_visible") is False)
        for unit in side.get("evidence_units") or []
    )
    if not visibility_observed:
        return {
            "first_appearance_sec": None,
            "total_screen_time_sec": None,
            "video_duration_sec": round(duration, 3),
            "ratio": None,
            "estimation_note": "Stage1 未提供明确的 product_visible 观察，不能把未知当作产品未出镜，需人工复核。",
        }
    intervals: list[tuple[float, float]] = []
    for unit in side.get("evidence_units") or []:
        if not isinstance(unit, dict) or unit.get("product_visible") is not True:
            continue
        parsed = parse_time_range_seconds(unit.get("time_range"), duration or None)
        if parsed is not None and parsed[1] > parsed[0]:
            intervals.append(parsed)
    intervals.sort()
    merged: list[list[float]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    total = sum(end - start for start, end in merged)
    first = merged[0][0] if merged else 0.0
    ratio = total / duration
    return {
        "first_appearance_sec": round(first, 3),
        "total_screen_time_sec": round(total, 3),
        "video_duration_sec": round(duration, 3),
        "ratio": round(min(max(ratio, 0.0), 1.0), 6),
        "estimation_note": "由代码从达人 Stage1 evidence_units 的明确 product_visible 标记合并区间计算。",
    }


def _deterministic_improvement(stage_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = [
        item for item in stage_results
        if isinstance(item, dict) and item.get("model_gap_magnitude") in {"large", "medium"}
    ]
    selected = candidates[0] if candidates else (stage_results[0] if stage_results else {})
    stage = _segmented_stage_code(selected.get("stage")) or "S1"
    return [{
        "title": f"优先复核{stage}阶段证据",
        "target_stage": stage,
        "gmv_impact": "待基于完整阶段证据确认",
        "gap_type": selected.get("gap_type") if selected.get("gap_type") in {"structural", "execution", "resource", "unknown"} else "unknown",
        "time_range": selected.get("creator_time_range") or "",
        "creator_time_range": selected.get("creator_time_range") or "",
        "benchmark_time_range": selected.get("benchmark_time_range") or "",
        "problem": selected.get("gap") or "阶段证据不足，暂不生成确定性建议。",
        "benchmark_reference": selected.get("benchmark_summary") or "暂无合格标杆证据。",
        "benchmark_evidence_ids": selected.get("benchmark_evidence_ids") or [],
        "suggestion": "待阶段证据完成后再生成具体改进动作。",
        "actions": ["补齐并复核该阶段关键证据"],
        "gmv_reason": "避免把证据未知误写成业务缺陷。",
        "evidence": selected.get("evidence") or [],
        "priority": 1,
    }]


def _project_synthesis_improvements(
    raw_improvements: Any,
    stage_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project model prose onto code-owned stage evidence and ranges.

    Stage3 may write prose and select a target stage. It cannot author IDs,
    time ranges, gap types, or priority because those fields already have a
    single authoritative source in the normalized Stage2 result.
    """
    by_code = {
        code: stage
        for stage in stage_results
        if isinstance(stage, dict)
        for code in [_segmented_stage_code(stage.get("stage"))]
        if code in _SEGMENTED_STAGE_NAMES
    }
    projected: list[dict[str, Any]] = []
    for item in raw_improvements if isinstance(raw_improvements, list) else []:
        if not isinstance(item, dict):
            continue
        code = _segmented_stage_code(item.get("target_stage"))
        stage = by_code.get(code)
        if stage is None:
            continue
        magnitude = str(stage.get("model_gap_magnitude") or "uncertain").strip().lower()
        priority = {"large": 1, "medium": 2, "small": 3, "none": 4}.get(magnitude, 4)
        raw_actions = item.get("actions")
        actions = [raw_actions] if isinstance(raw_actions, str) else raw_actions
        actions = actions if isinstance(actions, list) else []
        projected.append(
            {
                "title": str(item.get("title") or f"复核{code}阶段").strip(),
                "target_stage": code,
                "problem": str(item.get("problem") or stage.get("gap") or "阶段差距待复核").strip(),
                "suggestion": str(item.get("suggestion") or "待基于阶段证据复核").strip(),
                "actions": [str(value).strip() for value in actions if str(value).strip()][:5],
                "gmv_reason": str(item.get("gmv_reason") or "避免把证据未知误写成业务缺陷").strip(),
                "gmv_impact": str(item.get("gmv_impact") or "待基于完整阶段证据确认").strip(),
                # Stage3 can supply prose only.  gap_type is projected from
                # the already-closed Stage2 result.
                "gap_type": stage.get("gap_type") if stage.get("gap_type") in {"structural", "execution", "resource", "unknown"} else "unknown",
                "time_range": stage.get("time_range") or "",
                "creator_time_range": stage.get("creator_time_range") or "",
                "benchmark_time_range": stage.get("benchmark_time_range") or "",
                "benchmark_reference": stage.get("benchmark_summary") or "暂无合格标杆证据。",
                "benchmark_evidence_ids": list(stage.get("benchmark_evidence_ids") or []),
                "evidence": list(stage.get("evidence") or []),
                "priority": priority,
            }
        )
    return projected[:5]


def _prepare_segmented_synthesis(raw: dict[str, Any] | None, stage_results: list[dict[str, Any]]) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    result = copy.deepcopy(raw)
    result.setdefault("one_line_verdict", "基于分阶段证据完成分析，部分字段需按阶段状态复核。")
    result.setdefault("one_line_summary", result["one_line_verdict"])
    result.setdefault("executive_summary", result["one_line_summary"])
    result.setdefault("holistic_assessment", {})
    result.setdefault("key_conclusions", [])
    result.setdefault("loop_closure", {})
    result.setdefault("s3_s4_relationship", {})
    result.setdefault("promise_chain", {})
    result["improvements"] = _project_synthesis_improvements(
        result.get("improvements"),
        stage_results,
    ) or _deterministic_improvement(stage_results)
    return result


def _segmented_stage_unresolved(stage_results: list[dict[str, Any]]) -> list[str]:
    """Return core stages that cannot support a completed run marker."""
    unresolved: list[str] = []
    for stage in stage_results:
        if not isinstance(stage, dict):
            continue
        code = _segmented_stage_code(stage.get("stage"))
        comparison_status = str(stage.get("comparison_status") or "").strip().lower()
        if comparison_status in {"not_directly_comparable", "not_applicable"}:
            # A closed comparison scope is an intentional terminal state, not
            # a failed Stage2 handoff. It must remain explicit in the report,
            # but should not make an otherwise complete segmented run appear
            # degraded.
            continue
        status = str(
            stage.get("analysis_status")
            or stage.get("stage_handoff_status")
            or "unknown"
        ).strip().lower()
        stage_state = str(stage.get("stage_state") or "unknown").strip().lower()
        magnitude = str(stage.get("model_gap_magnitude") or "unknown").strip().lower()
        # ``stage_state`` is a required semantic output.  A grounded evidence
        # handoff is necessary but not sufficient: if the model did not close
        # the stage judgment itself, relation/magnitude must not become a
        # publishable conclusion through a compatibility default.
        if code and (
            status != "grounded"
            or stage_state != "completed"
            or magnitude == "uncertain"
        ):
            unresolved.append(code)
    return list(dict.fromkeys(unresolved))
