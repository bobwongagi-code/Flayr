"""视频级商业门控：在 S1-S6 之外识别会污染整条视频的根本问题。

本模块不改阶段 severity。它只消费 Stage1 锁定事实、Step-0 产品地基和阶段绝对状态，
产出可审计的全局 finding、因果标签与确定性商业优先级。
"""

from __future__ import annotations

from typing import Any

from ..artifacts import parse_time_range_seconds


_IMPACT_RANK = {"blocking": 0, "major": 1, "minor": 2, "pass": 3, "unknown": 4}
_GLOBAL_ORDER = {"selling_point_route": 0, "focus_coherence": 1, "attention_cleanliness": 2}
_STAGE_ORDER = {"S1": 0, "S4": 1, "S3": 2, "S6": 3, "S2": 4, "S5": 5}
_TEMPORAL_RANK = {"unknown": 0, "static_only": 1, "focused_temporal": 2, "full_temporal": 3}
_PAINPOINT_RELEVANCE_RANK = {
    # 只有标杆命中而达人未命中的核心痛点，才提高同一 severity tier 内的商业优先级。
    "benchmark_only": 0,
    "both": 1,
    "none": 1,
    "creator_only": 2,
}


def materialize_global_diagnosis(result: dict[str, Any], analysis: dict[str, Any] | None) -> None:
    understanding = result.get("video_understanding") if isinstance(result.get("video_understanding"), dict) else {}
    creator = understanding.get("creator") if isinstance(understanding.get("creator"), dict) else {}
    benchmark = understanding.get("benchmark") if isinstance(understanding.get("benchmark"), dict) else {}
    creator_mode = _temporal_mode(creator)
    benchmark_mode = _temporal_mode(benchmark)
    comparative_mode = min((creator_mode, benchmark_mode), key=lambda mode: _TEMPORAL_RANK[mode])

    findings = [
        _selling_point_finding(result, creator, benchmark),
        _focus_finding(result, creator, benchmark, creator_mode, benchmark_mode, comparative_mode),
        _attention_finding(result, creator, benchmark, creator_mode, benchmark_mode, comparative_mode),
    ]
    actionable = [finding for finding in findings if finding["impact"] in {"blocking", "major", "minor"}]
    overall = min(actionable, key=lambda item: _IMPACT_RANK[item["impact"]])["impact"] if actionable else (
        "unknown" if all(item["impact"] == "unknown" for item in findings) else "pass"
    )
    result["global_diagnosis"] = {
        "version": "1.0",
        "temporal_capability": {
            "creator": creator_mode,
            "benchmark": benchmark_mode,
            "comparative": comparative_mode,
        },
        "overall_status": overall,
        "findings": findings,
    }
    _annotate_global_causes(result, findings)
    result["commercial_priorities"] = _commercial_priorities(result, findings)
    _reorder_improvements(result)
    priorities = result["commercial_priorities"]
    result["commercial_priority_summary"] = str(priorities[0].get("summary") or "") if priorities else ""


def _reorder_improvements(result: dict[str, Any]) -> None:
    stage_order = {
        str(item.get("reference_id") or ""): index
        for index, item in enumerate(result.get("commercial_priorities") or [])
        if isinstance(item, dict) and item.get("source") == "stage"
    }
    improvements = [item for item in result.get("improvements") or [] if isinstance(item, dict)]
    improvements.sort(
        key=lambda item: (
            stage_order.get(str(item.get("target_stage") or "")[:2].upper(), 999),
            int(item.get("priority") or 999),
        )
    )
    for index, item in enumerate(improvements, start=1):
        item["priority"] = index
    result["improvements"] = improvements


def _temporal_mode(side: dict[str, Any]) -> str:
    mode = str(side.get("temporal_evidence_mode") or "unknown")
    return mode if mode in _TEMPORAL_RANK else "unknown"


def _finding(
    gate_id: str,
    impact: str,
    creator_status: str,
    benchmark_status: str,
    comparative_status: str,
    summary: str,
    downstream_impact: str,
    suggested_action: str,
    evidence_ids: list[str],
    affected_stages: list[str],
    confidence: str,
) -> dict[str, Any]:
    return {
        "id": gate_id,
        "impact": impact,
        "creator_status": creator_status,
        "benchmark_status": benchmark_status,
        "comparative_status": comparative_status,
        "summary": summary,
        "downstream_impact": downstream_impact,
        "suggested_action": suggested_action,
        "evidence_ids": list(dict.fromkeys(evidence_ids)),
        "affected_stages": list(dict.fromkeys(affected_stages)),
        "confidence": confidence,
    }


def _proof_plan(result: dict[str, Any]) -> dict[str, Any]:
    profile = result.get("product_profile") if isinstance(result.get("product_profile"), dict) else {}
    plan = profile.get("short_video_proof_plan") if isinstance(profile.get("short_video_proof_plan"), dict) else {}
    return plan if plan.get("valid") is True else {}


def _dominant_selling_point(side: dict[str, Any]) -> dict[str, Any] | None:
    observations = side.get("selling_point_observations") if isinstance(side.get("selling_point_observations"), list) else []
    candidates = [item for item in observations if isinstance(item, dict) and str(item.get("candidate_id") or "").strip()]
    if not candidates:
        return None
    return max(candidates, key=lambda item: max(float(item.get("visual_share") or 0), float(item.get("speech_share") or 0)))


def _selling_side_status(side: dict[str, Any], anchor_id: str) -> tuple[str, dict[str, Any] | None]:
    dominant = _dominant_selling_point(side)
    if not dominant or not anchor_id:
        return "unknown", dominant
    if str(dominant.get("candidate_id") or "") == anchor_id:
        return ("aligned" if dominant.get("proof_signal_present") is not False else "unproven"), dominant
    return ("alternate_proven" if dominant.get("proof_signal_present") is True else "misaligned"), dominant


def _selling_point_finding(result: dict[str, Any], creator: dict[str, Any], benchmark: dict[str, Any]) -> dict[str, Any]:
    plan = _proof_plan(result)
    profile = result.get("product_profile") if isinstance(result.get("product_profile"), dict) else {}
    anchor_id = str(plan.get("primary_candidate_id") or plan.get("s4_anchor_candidate_id") or "")
    creator_status, dominant = _selling_side_status(creator, anchor_id)
    benchmark_status, _ = _selling_side_status(benchmark, anchor_id)
    evidence_ids = list((dominant or {}).get("evidence_ids") or [])
    source = str(plan.get("selection_source") or "model_category_default")
    confidence = str(plan.get("anchor_confidence") or "low")
    stages = _stage_map(result)
    weak_proof = any(
        str(stages.get(code, {}).get("creator_absolute_status") or "") in {"missing", "weak"}
        for code in ("S3", "S4")
    )
    observation_status = creator.get("gate_observation_status") if isinstance(creator.get("gate_observation_status"), dict) else {}
    if observation_status.get("selling_point_route") != "complete" or not plan or not anchor_id or not dominant:
        return _finding(
            "selling_point_route", "unknown", creator_status, benchmark_status, "unknown",
            "现有事实不足以确认达人是否把最适合短视频证明的卖点作为主路线。",
            "不据此改动 S2-S4 判断。", "补齐卖点占比与证明信号后再判断。", evidence_ids, [], "low",
        )
    if creator_status == "aligned":
        return _finding(
            "selling_point_route", "pass", creator_status, benchmark_status, "comparable",
            "达人主卖点与产品证明计划的核心视觉锚点一致。",
            "后续只需评价 S3/S4 的执行质量。", "保留当前卖点路线，优化证明执行。", evidence_ids, [], "high",
        )
    if creator_status == "alternate_proven":
        return _finding(
            "selling_point_route", "minor", creator_status, benchmark_status, "comparable",
            "达人选择了非首选卖点，但已经给出可感知证明。",
            "路线不是最优先锚点，但不会直接否定 S3/S4 证据。", "确认该替代卖点是否符合本次投放目标。", evidence_ids,
            ["S2", "S3", "S4"], "medium",
        )
    authoritative = (
        source in {"operator_priority", "curated_priority"}
        and str(profile.get("proof_contract_source") or "inferred") in {"operator", "curated"}
        and confidence == "high"
    )
    impact = "blocking" if creator_status == "misaligned" and weak_proof and authoritative else "major"
    return _finding(
        "selling_point_route", impact, creator_status, benchmark_status, "comparable",
        "达人把主要篇幅放在非核心或未被证明的卖点上。",
        "S2 引出的价值、S3 的动作和 S4 的效果可能围绕错误主线展开。",
        "先改为优先证明短视频证明计划中的核心锚点，再优化阶段执行。",
        evidence_ids, ["S2", "S3", "S4"], "high" if authoritative else "medium",
    )


def _variant_side_status(side: dict[str, Any], mode: str) -> tuple[str, list[str], list[str]]:
    status = side.get("gate_observation_status") if isinstance(side.get("gate_observation_status"), dict) else {}
    if status.get("variant_focus") != "complete":
        return "unknown", [], []
    units = [item for item in side.get("evidence_units") or [] if isinstance(item, dict)]
    variant_ids = list(dict.fromkeys(v for unit in units for v in unit.get("variant_ids") or [] if str(v).strip()))
    if not units:
        return "unknown", [], []
    if len(variant_ids) <= 1:
        return "single_focus", [], variant_ids
    rule = side.get("variant_decision_rule") if isinstance(side.get("variant_decision_rule"), dict) else {}
    explicit_units = [unit for unit in units if unit.get("variant_relation_mode") == "explicit_comparison"]
    explained = rule.get("speech_explains_choice") is True or any(
        unit.get("comparison_purpose_explicit") is True for unit in explicit_units
    )
    evidence_ids = list(rule.get("evidence_ids") or [])
    evidence_ids.extend(str(unit.get("id") or "") for unit in units if len(unit.get("variant_ids") or []) >= 2)
    if explained:
        return "explained_comparison", evidence_ids, variant_ids
    ambiguous_core = any(
        len(unit.get("variant_ids") or []) >= 2
        and unit.get("variant_attribution_confident") is not True
        and set(unit.get("functions") or []) & {"S2_intro", "S3_usage", "S4_effect"}
        for unit in units
    )
    if mode == "full_temporal" and ambiguous_core:
        return "core_attribution_broken", evidence_ids, variant_ids
    return "choice_logic_missing", evidence_ids, variant_ids


def _focus_finding(
    result: dict[str, Any],
    creator: dict[str, Any],
    benchmark: dict[str, Any],
    creator_mode: str,
    benchmark_mode: str,
    comparative_mode: str,
) -> dict[str, Any]:
    creator_status, evidence_ids, _ = _variant_side_status(creator, creator_mode)
    benchmark_status, _, _ = _variant_side_status(benchmark, benchmark_mode)
    comparative_status = "comparable" if comparative_mode in {"full_temporal", "focused_temporal"} else "unknown"
    if creator_status == "unknown":
        impact, summary, affected = "unknown", "现有事实不足以判断多 SKU/变体是否造成焦点分散。", []
    elif creator_status in {"single_focus", "explained_comparison"}:
        impact, summary, affected = "pass", "达人保持单一焦点，或已明确解释多个变体如何比较和选择。", []
    elif creator_status == "core_attribution_broken":
        impact, summary, affected = "blocking", "多个变体混在核心证明片段中，用户难以判断效果和卖点属于哪一款。", ["S2", "S3", "S4"]
    else:
        impact, summary, affected = "major", "视频出现多个变体，但没有清楚说明各自适合谁或如何选择。", ["S2", "S3", "S4"]
    return _finding(
        "focus_coherence", impact, creator_status, benchmark_status, comparative_status, summary,
        "焦点不清会稀释产品主张，并使使用过程和效果证据归属不稳定。",
        "只保留一个主推变体；如必须比较，先明确比较维度和选择规则。",
        evidence_ids, affected, "high" if creator_mode == "full_temporal" else "medium",
    )


def _attention_side_status(side: dict[str, Any], mode: str) -> tuple[str, list[str], float]:
    status = side.get("gate_observation_status") if isinstance(side.get("gate_observation_status"), dict) else {}
    if status.get("attention_scan") != "complete":
        return "unknown", [], 0.0
    competitors = [item for item in side.get("attention_competitors") or [] if isinstance(item, dict)]
    if not competitors:
        return ("clean" if mode != "unknown" else "unknown"), [], 0.0
    relevant = [item for item in competitors if item.get("participates_in_product_task") is False and item.get("high_salience") is True]
    if not relevant:
        return "clean", [], 0.0
    evidence_ids = [str(eid) for item in relevant for eid in item.get("evidence_ids") or []]
    if mode != "full_temporal":
        return "temporal_unknown", evidence_ids, 0.0
    persistent = [item for item in relevant if item.get("persistent_motion") is True]
    if not persistent:
        return "clean", evidence_ids, 0.0
    total_duration = _side_duration(side)
    occupied = _ranges_duration([value for item in persistent for value in item.get("time_ranges") or []])
    share = min(1.0, occupied / total_duration) if total_duration > 0 else 0.0
    occluding_ranges = [
        value
        for item in persistent
        if item.get("occludes_proof_area") is True
        for value in item.get("time_ranges") or []
    ]
    occluding_share = min(1.0, _ranges_duration(occluding_ranges) / total_duration) if total_duration > 0 else 0.0
    if occluding_share >= 0.30:
        return "proof_obstructed", evidence_ids, share
    return "distracting", evidence_ids, share


def _attention_finding(
    result: dict[str, Any],
    creator: dict[str, Any],
    benchmark: dict[str, Any],
    creator_mode: str,
    benchmark_mode: str,
    comparative_mode: str,
) -> dict[str, Any]:
    creator_status, evidence_ids, share = _attention_side_status(creator, creator_mode)
    benchmark_status, _, _ = _attention_side_status(benchmark, benchmark_mode)
    comparative_status = "comparable" if comparative_mode in {"full_temporal", "focused_temporal"} else "unknown"
    if creator_status in {"unknown", "temporal_unknown"}:
        impact, summary, affected = "unknown", "连续时序证据不足，不能确认疑似物体是否持续抢占注意力。", []
    elif creator_status == "clean":
        impact, summary, affected = "pass", "未发现持续抢占产品证明注意力的非任务物体。", []
    elif creator_status == "proof_obstructed":
        impact, summary, affected = "blocking", "高显著干扰物持续遮挡核心证明区域，产品价值难以被看清。", _stages_for_evidence(result, evidence_ids)
    else:
        impact, summary, affected = "major", "非任务物体持续运动并抢占画面注意力。", _stages_for_evidence(result, evidence_ids)
    suffix = f"（约覆盖视频 {share:.0%}）" if share > 0 else ""
    return _finding(
        "attention_cleanliness", impact, creator_status, benchmark_status, comparative_status, f"{summary}{suffix}",
        "用户注意力会从产品、动作或效果证据转移。", "移除或固定无关高显著物体，保证核心证明区域持续清楚。",
        evidence_ids, affected, "high" if creator_mode == "full_temporal" else "low",
    )


def _ranges_duration(ranges: list[Any]) -> float:
    intervals: list[tuple[float, float]] = []
    for value in ranges:
        parsed = parse_time_range_seconds(value, None)
        if parsed is None:
            continue
        start, end = parsed
        if end > start:
            intervals.append((start, end))
    intervals.sort()
    merged: list[tuple[float, float]] = []
    for start, end in intervals:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return sum(end - start for start, end in merged)


def _side_duration(side: dict[str, Any]) -> float:
    units = [item for item in side.get("evidence_units") or [] if isinstance(item, dict)]
    ends = []
    for item in units:
        parsed = parse_time_range_seconds(item.get("time_range"), None)
        if parsed is not None:
            ends.append(parsed[1])
    return max(ends, default=0.0)


def _stage_map(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    mapped: dict[str, dict[str, Any]] = {}
    for stage in result.get("stage_analysis") or []:
        if not isinstance(stage, dict):
            continue
        code = str(stage.get("stage") or "")[:2].upper()
        if code in _STAGE_ORDER:
            mapped[code] = stage
    return mapped


def _stages_for_evidence(result: dict[str, Any], evidence_ids: list[str]) -> list[str]:
    evidence_set = set(evidence_ids)
    affected = []
    for code, stage in _stage_map(result).items():
        if evidence_set & set(stage.get("creator_evidence_ids") or []):
            affected.append(code)
    return affected or (["S3", "S4"] if evidence_ids else [])


def _annotate_global_causes(result: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    actionable = [item for item in findings if item.get("impact") in {"blocking", "major", "minor"}]
    for code, stage in _stage_map(result).items():
        stage["affected_by_global_issues"] = [
            str(item["id"]) for item in actionable if code in item.get("affected_stages", [])
        ]
    for improvement in result.get("improvements") or []:
        if not isinstance(improvement, dict):
            continue
        target = str(improvement.get("target_stage") or "")[:2].upper()
        improvement["root_cause_ids"] = [
            str(item["id"]) for item in actionable if target in item.get("affected_stages", [])
        ]


def _commercial_relevance(stage: dict[str, Any]) -> dict[str, Any]:
    """读取 Stage2 的分类事实；unknown 只表示未知，不按 none 处理。"""
    value = str(stage.get("painpoint_relevance") or "").strip().lower()
    if value not in _PAINPOINT_RELEVANCE_RANK:
        return {
            "status": "unknown",
            "value": None,
            "priority_rank": None,
            "source": "stage_analysis.painpoint_relevance",
        }
    return {
        "status": "known",
        "value": value,
        "priority_rank": _PAINPOINT_RELEVANCE_RANK[value],
        "source": "stage_analysis.painpoint_relevance",
    }


def _commercial_priorities(result: dict[str, Any], findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    priorities: list[dict[str, Any]] = []
    for finding in findings:
        impact = str(finding.get("impact") or "unknown")
        if impact not in {"blocking", "major", "minor"}:
            continue
        tier = {"blocking": "P0", "major": "P2", "minor": "P4"}[impact]
        priorities.append(
            {
                "id": f"global:{finding['id']}",
                "source": "global",
                "tier": tier,
                "title": _global_title(str(finding["id"])),
                "summary": str(finding.get("summary") or ""),
                "reference_id": str(finding["id"]),
                "root_cause_ids": [str(finding["id"])],
                "_sort": (int(tier[1]), _GLOBAL_ORDER.get(str(finding["id"]), 99), -len(finding.get("affected_stages") or []), _confidence_rank(finding.get("confidence")), str(finding["id"])),
            }
        )
    improvements_by_stage = {
        str(item.get("target_stage") or "")[:2].upper()
        for item in result.get("improvements") or []
        if isinstance(item, dict)
    }
    for code, stage in _stage_map(result).items():
        if str(stage.get("comparison_status") or "") in {"not_directly_comparable", "not_applicable"}:
            continue
        severity = str(stage.get("severity") or "small")
        if severity == "small" and code not in improvements_by_stage:
            continue
        tier = {"large": "P1", "medium": "P3", "small": "P5"}.get(severity, "P3")
        status = str(stage.get("creator_absolute_status") or "unknown")
        relevance = _commercial_relevance(stage)
        relevance_sort_rank = (
            relevance["priority_rank"]
            if relevance["status"] == "known" and isinstance(relevance["priority_rank"], int)
            else 99
        )
        priorities.append(
            {
                "id": f"stage:{code}",
                "source": "stage",
                "tier": tier,
                "title": str(stage.get("stage") or code),
                "summary": str(stage.get("gap") or stage.get("gap_summary") or ""),
                "reference_id": code,
                "root_cause_ids": list(stage.get("affected_by_global_issues") or []),
                "commercial_relevance": relevance,
                "_sort": (
                    int(tier[1]),
                    _stage_failure_rank(status),
                    relevance_sort_rank,
                    _STAGE_ORDER.get(code, 99),
                    code,
                ),
            }
        )
    priorities.sort(key=lambda item: item["_sort"])
    for item in priorities:
        item.pop("_sort", None)
    return priorities


def _global_title(gate_id: str) -> str:
    return {
        "selling_point_route": "主卖点路线",
        "focus_coherence": "产品焦点一致性",
        "attention_cleanliness": "画面注意力洁净度",
    }.get(gate_id, gate_id)


def _confidence_rank(value: Any) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(str(value or "low"), 2)


def _stage_failure_rank(status: str) -> int:
    if status in {"missing", "generic", "incompatible", "misplaced"}:
        return 0
    if status in {"risky", "not_landed"}:
        return 1
    if status in {"weak", "duplicate"}:
        return 2
    return 3
