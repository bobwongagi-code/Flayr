#!/usr/bin/env python3
"""对最终 analysis.json 做可重复的 Ground Truth 评测。

这个脚本只读取结果：不调用模型、不修改 runs/、不参与 severity 推导。
它明确区分 final severity 与 model_severity，避免将旧的原始模型输出
误作当前主链结果，也把比较资格作为统计门槛。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.structure_modules import stage1_event_catalog
from flayr_core.postprocess.derive import CRITICAL_BAND, TH_MEDIUM, TH_SMALL


SEVERITIES = ("small", "medium", "large")
SEVERITY_RANK = {value: index for index, value in enumerate(SEVERITIES)}
NOT_APPLICABLE = "na"
STAGE_SEVERITY_SCOPE = "stage_severity"
WHOLE_VIDEO_OBSERVATION_SCOPE = "whole_video_observation"
WHOLE_VIDEO_VERDICTS = {"viable", "not_viable"}
STAGE_RE = re.compile(r"^(S[1-6])")
SHADOW_STAGES = ("S1", "S2", "S3", "S4")
PROMOTION_MIN_SAMPLES_PER_STAGE = 3
PROMOTION_MIN_CATEGORIES = 3
STAGE1_EVENT_IDS = tuple(str(item["id"]) for item in stage1_event_catalog())

# 这是分析链的字段所有权表，不是新的判断规则。它让 GT 评测能审计：
# Stage2 输出的结构化观察到底被 severity、跨阶段逻辑还是报告/QA 使用，避免
# “字段已经输出但下游悄悄丢掉”的黑盒问题。
FLAG_SUFFIXES = {
    "S1": "hook",
    "S2": "s2",
    "S3": "s3",
    "S4": "s4",
    "S5": "s5",
    "S6": "s6",
}
FLAG_FIELD_OWNERSHIP: dict[str, dict[str, set[str]]] = {
    "S1": {
        "derive": {"exists", "dims", "landing_met", "anchors_proposition"},
        "cross_stage": {"type", "hook_boundary_seconds", "proposition_ids"},
        "qa_report": {"hook_boundary_reason", "s2_start_signal", "landing_reason", "window_evidence", "landing_window_leak", "landing_conditions", "landing_shadow_met", "landing_failure_reasons", "stay_motivation_mechanism", "landing_shadow_reason", "landing_shadow_window_leak", "evidence_ids"},
    },
    "S2": {
        "derive": {"exists", "merged_with_s3", "handoff_met", "s1_s2_compatible", "computed_s1_s2_compatible", "product_identity_clear", "product_role_clear", "excluded_or_risky_module"},
        "cross_stage": {"module_type", "proposition_ids"},
        "qa_report": {"start_seconds", "end_seconds", "handoff_reason", "evidence_ids"},
    },
    "S3": {
        "derive": {"exists", "usage_process_visible", "result_only_without_process", "mouth_only_or_static", "real_usage_met", "core_selling_point_visible", "action_proof_met", "action_target_contact_met", "action_application_change_visible", "critical_action_continuity_met", "missing_selling_points", "scene_mode", "usage_context_fit", "continuity_met", "richness_met", "single_scene_continuity_met", "single_scene_variation_met", "multi_scene_logic_met", "multi_scene_transition_met", "multi_scene_role_adaptation_met", "role_design_met", "role_interaction_met", "fake_or_staged"},
        "cross_stage": {"demonstrated_selling_points", "proposition_ids"},
        "qa_report": {"module_type", "process_framing_met", "distinct_personas_met", "steps_clear_met", "pov_immersive_met", "presentation_overlays", "start_seconds", "end_seconds", "usage_reason", "evidence_ids"},
    },
    "S4": {
        "derive": {"effect_type", "effect_visible", "effect_salience", "effect_proposition_matched", "comparison_control_met", "closeup_or_focus_met", "visual_difference_observed", "module_constraints_met", "effect_maximized", "requires_close_inspection", "effect_attribution_supported", "result_only_without_process", "process_linked_effect", "tamper_or_cut_risk"},
        "cross_stage": {"effect_visible", "effect_proposition_matched", "process_linked_effect", "proposition_ids"},
        "qa_report": {"start_seconds", "end_seconds", "effect_reason", "evidence_ids", "visual_verifier_reason"},
    },
    "S5": {
        "derive": {"exists", "trust_evidence_type", "trust_basis", "trust_source_visible", "trust_source_credible", "trust_claim_specific", "product_relevance_met", "independent_trust_purpose", "duplicates_other_stage", "voice_only", "risky_or_unsupported"},
        "cross_stage": {"proposition_ids"},
        "qa_report": {"module_type", "trust_source_evidence_ids", "start_seconds", "end_seconds", "trust_reason", "evidence_ids"},
    },
    "S6": {
        "derive": {"exists", "direct_order_met", "action_path_clear", "soft_purchase_invitation_met", "offer_or_incentive_clear", "urgency_met", "product_value_recalled", "module_fit_met", "ending_position_met", "depends_on_valid_s4", "computed_depends_on_valid_s4", "compliance_risk"},
        "cross_stage": {"module_type", "proposition_ids"},
        "qa_report": {"price_anchor_met", "urgency_evidence_met", "gift_stack_met", "guarantee_clear_met", "start_seconds", "end_seconds", "cta_reason", "evidence_ids"},
    },
}
MULTIMODAL_FIELD_OWNERSHIP = {
    "derive": {
        "channel_impacts", "dominant_channel", "cross_channel_relation",
        "integrated_effect", "compensation_applied",
    },
    "cross_stage": set(),
    "qa_report": {"channel_evidence_ids", "integration_reason"},
}


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根节点必须为对象：{path}")
    return value


def stage_id(value: Any) -> str | None:
    match = STAGE_RE.match(str(value or "").strip())
    return match.group(1) if match else None


def normalize_severity(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in SEVERITY_RANK else None


def normalize_ground_truth(value: Any) -> str | None:
    """GT 允许 na；它表示该阶段不适用，不进入准确率统计。"""
    normalized = str(value or "").strip().lower()
    return normalized if normalized in {*SEVERITIES, NOT_APPLICABLE} else None


def severity_from_score(score: float) -> str:
    """严格复用 derive 的连续分分桶边界，不把覆盖规则混进来。"""
    if score > TH_MEDIUM:
        return "large"
    if score > TH_SMALL:
        return "medium"
    return "small"


def severity_diagnostics(expected: str, final: str, stage: dict[str, Any]) -> dict[str, Any]:
    """给标签偏差增加序数距离与连续分诊断，不改变 matched 判定。

    landing 下限、命题锚、红线和过程/效果断层都可能覆盖普通分桶。此时即使
    trace 中保留了 S，也不能把差一档解释为阈值附近的离散化噪声。
    """
    distance = abs(SEVERITY_RANK[final] - SEVERITY_RANK[expected])
    trace = stage.get("severity_derivation")
    if not isinstance(trace, dict):
        trace = {}
    score = trace.get("S")
    reason = str(trace.get("reason") or "")
    if not isinstance(score, (int, float)):
        if "执行分缺失" in reason:
            mechanism = "missing_execution"
        elif "双方均未涉及" in reason:
            mechanism = "both_absent"
        elif "达人持平或更优" in reason:
            mechanism = "non_positive_execution_gap"
        elif "红线" in reason or "下限" in reason or "断层" in reason:
            mechanism = "structural_override"
        else:
            mechanism = "other_non_score_path"
        return {
            "ordinal_distance": distance,
            "score": None,
            "score_bucket": None,
            "distance_to_nearest_threshold": None,
            "near_threshold": None,
            "derivation_path": "non_score_path",
            "decision_mechanism": mechanism,
        }

    numeric_score = float(score)
    score_bucket = severity_from_score(numeric_score)
    threshold_distance = min(abs(numeric_score - TH_SMALL), abs(numeric_score - TH_MEDIUM))
    override_markers = ("红线", "下限", "断层", "持平或更优", "均未涉及", "不进公式")
    threshold_only = final == score_bucket and not any(marker in reason for marker in override_markers)
    return {
        "ordinal_distance": distance,
        "score": numeric_score,
        "score_bucket": score_bucket,
        "distance_to_nearest_threshold": round(threshold_distance, 4),
        "near_threshold": threshold_distance <= CRITICAL_BAND if threshold_only else None,
        "derivation_path": "threshold" if threshold_only else "override_or_floor",
        "decision_mechanism": "threshold_bucket" if threshold_only else "score_with_override_or_floor",
    }


def sample_run_path(runs_root: Path, sample_id: str, run_prefix: str) -> Path:
    return runs_root / f"{run_prefix}{sample_id}" / "analysis.json"


def parse_explicit_run_paths(values: list[str]) -> dict[str, Path]:
    """解析显式 sample→analysis.json 映射，避免验证依赖运行目录命名。"""
    paths: dict[str, Path] = {}
    for value in values:
        sample_id, separator, raw_path = str(value).partition("=")
        sample_id = sample_id.strip()
        raw_path = raw_path.strip()
        if not separator or not sample_id or not raw_path:
            raise ValueError(f"--run-path 必须是 sample_id=/absolute/or/relative/analysis.json：{value}")
        if sample_id in paths:
            raise ValueError(f"--run-path 重复 sample_id：{sample_id}")
        paths[sample_id] = Path(raw_path)
    return paths


def manifest_samples(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    samples = manifest.get("samples")
    if not isinstance(samples, list):
        return {}
    return {
        str(item.get("id")): item
        for item in samples
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }


def eligible_stages(
    sample_id: str,
    label: dict[str, Any],
    manifest_sample: dict[str, Any] | None,
    result: dict[str, Any],
) -> tuple[set[str], str]:
    """取 GT、输入清单和当前结果三者共同允许的阶段。"""
    if isinstance(manifest_sample, dict) and (
        str(manifest_sample.get("evaluation_scope") or STAGE_SEVERITY_SCOPE).strip()
        == WHOLE_VIDEO_OBSERVATION_SCOPE
    ):
        return set(), WHOLE_VIDEO_OBSERVATION_SCOPE
    labels = label.get("stages") if isinstance(label.get("stages"), dict) else {}
    eligible = {
        stage
        for stage, severity in labels.items()
        if stage_id(stage) is not None and normalize_ground_truth(severity) in SEVERITY_RANK
    }
    sources = ["ground_truth"]

    if isinstance(manifest_sample, dict):
        metric_scope = str(manifest_sample.get("metric_scope") or "").strip()
        configured = manifest_sample.get("direct_product_stages")
        if metric_scope == "excluded":
            return set(), "manifest_excluded"
        if isinstance(configured, list):
            eligible &= {str(item) for item in configured}
            sources.append("manifest")

    eligibility = result.get("comparison_eligibility")
    if isinstance(eligibility, dict):
        current = eligibility.get("direct_product_stages")
        if isinstance(current, list):
            eligible &= {str(item) for item in current}
            sources.append("analysis")

    return eligible, "+".join(sources)


def diagnosis(expected: str, final: str, stage: dict[str, Any]) -> str:
    """定位偏差发生在哪一层，不把模型错误伪装成事实错误。"""
    model = normalize_severity(stage.get("model_severity"))
    derivation = stage.get("severity_derivation")
    derived = isinstance(derivation, dict) and derivation.get("status") == "derived"
    if final == expected:
        return "matched"
    if model == expected and final != expected:
        return "derive_regression"
    if model == final:
        return "model_or_evidence_judgment"
    if derived:
        return "model_and_derive_disagree"
    return "unknown_decision_path"


def side_execution(stage: dict[str, Any], role: str) -> dict[str, Any]:
    shadow = stage.get(f"{role}_absolute_execution_shadow")
    return {
        "execution": stage.get(f"{role}_execution"),
        "absolute_status": stage.get(f"{role}_absolute_status"),
        "absolute_reason": stage.get(f"{role}_absolute_reason"),
        "shadow": shadow if isinstance(shadow, dict) else None,
    }


def _role_units(result: dict[str, Any], role: str) -> dict[str, dict[str, Any]]:
    understanding = result.get("video_understanding")
    role_data = understanding.get(role) if isinstance(understanding, dict) else None
    units = role_data.get("evidence_units") if isinstance(role_data, dict) else None
    return {
        str(unit.get("id")): unit
        for unit in units or []
        if isinstance(unit, dict) and str(unit.get("id") or "").strip()
    }


def _usable_fact(unit: dict[str, Any]) -> bool:
    return any(
        str(unit.get(field) or "").strip()
        for field in ("information", "voiceover", "voiceover_zh", "visual_fact", "subtitle_fact", "audio_fact")
    )


def _flag_chain_audit(result: dict[str, Any], sample_id: str) -> list[dict[str, Any]]:
    """审计每个阶段 flag 的证据完整性和字段去向。

    这不是“证据召回率”：没有人工 key-event 标注时，不能把“被引用”伪装成“所有关键事实都采到”。
    它先给出可客观测量的引用完整性，并明确暴露召回评估是否具备输入条件。
    """
    stages = result.get("stage_analysis")
    if not isinstance(stages, list):
        return []
    by_stage = {
        stage_id(stage.get("stage")): stage
        for stage in stages
        if isinstance(stage, dict) and stage_id(stage.get("stage"))
    }
    records: list[dict[str, Any]] = []
    for current_stage, suffix in FLAG_SUFFIXES.items():
        stage = by_stage.get(current_stage)
        if not isinstance(stage, dict):
            continue
        ownership = FLAG_FIELD_OWNERSHIP[current_stage]
        classified_fields = set().union(*ownership.values())
        for role in ("creator", "benchmark"):
            flag = stage.get(f"{role}_{suffix}")
            if not isinstance(flag, dict):
                records.append({
                    "sample_id": sample_id,
                    "stage": current_stage,
                    "role": role,
                    "flag_present": False,
                    "human_event_recall": "unavailable_without_human_key_events",
                })
                continue
            units = _role_units(result, role)
            evidence_ids = [str(value) for value in flag.get("evidence_ids") or [] if str(value).strip()]
            placeholders = [value for value in evidence_ids if "_NO_" in value]
            resolved = [value for value in evidence_ids if value in units and value not in placeholders]
            unresolved = [value for value in evidence_ids if value not in units and value not in placeholders]
            populated = {key for key, value in flag.items() if value not in (None, "", [], {})}
            unclassified = sorted(populated - classified_fields)
            records.append({
                "sample_id": sample_id,
                "stage": current_stage,
                "role": role,
                "flag_present": True,
                "evidence_ids": evidence_ids,
                "resolved_evidence_ids": resolved,
                "placeholder_evidence_ids": placeholders,
                "unresolved_evidence_ids": unresolved,
                "resolved_usable_fact_count": sum(1 for value in resolved if _usable_fact(units[value])),
                "field_ownership": {
                    owner: sorted(populated & fields)
                    for owner, fields in ownership.items()
                },
                "unclassified_populated_fields": unclassified,
                "human_event_recall": "unavailable_without_human_key_events",
            })
        multimodal_ownership = MULTIMODAL_FIELD_OWNERSHIP
        multimodal_classified = set().union(*multimodal_ownership.values())
        for role in ("creator", "benchmark"):
            assessment = stage.get(f"{role}_multimodal")
            if not isinstance(assessment, dict):
                records.append({
                    "sample_id": sample_id,
                    "stage": current_stage,
                    "role": role,
                    "flag_kind": "multimodal",
                    "flag_present": False,
                    "human_event_recall": "unavailable_without_human_key_events",
                })
                continue
            units = _role_units(result, role)
            evidence_map = assessment.get("channel_evidence_ids")
            evidence_ids = list(dict.fromkeys(
                str(value)
                for values in evidence_map.values() if isinstance(evidence_map, dict) and isinstance(values, list)
                for value in values if str(value).strip()
            )) if isinstance(evidence_map, dict) else []
            unresolved = [value for value in evidence_ids if value not in units]
            populated = {key for key, value in assessment.items() if value not in (None, "", [], {})}
            records.append({
                "sample_id": sample_id,
                "stage": current_stage,
                "role": role,
                "flag_kind": "multimodal",
                "flag_present": True,
                "evidence_ids": evidence_ids,
                "resolved_evidence_ids": [value for value in evidence_ids if value in units],
                "placeholder_evidence_ids": [],
                "unresolved_evidence_ids": unresolved,
                "resolved_usable_fact_count": sum(
                    1 for value in evidence_ids if value in units and _usable_fact(units[value])
                ),
                "field_ownership": {
                    owner: sorted(populated & fields)
                    for owner, fields in multimodal_ownership.items()
                },
                "unclassified_populated_fields": sorted(populated - multimodal_classified),
                "human_event_recall": "unavailable_without_human_key_events",
            })
    return records


def _event_time_bounds(value: Any) -> tuple[float, float] | None:
    if isinstance(value, list) and len(value) == 2:
        try:
            start, end = float(value[0]), float(value[1])
        except (TypeError, ValueError):
            return None
        return (min(start, end), max(start, end))
    parts = re.findall(r"\d+(?:\.\d+)?", str(value or ""))
    if len(parts) < 2:
        return None
    return float(parts[0]), float(parts[1])


def _ranges_overlap(left: tuple[float, float], right: tuple[float, float]) -> bool:
    return left[0] <= right[1] and right[0] <= left[1]


def _stage_referenced_ids(result: dict[str, Any], current_stage: str, role: str) -> set[str]:
    suffix = FLAG_SUFFIXES.get(current_stage)
    for stage in result.get("stage_analysis") or []:
        if not isinstance(stage, dict) or stage_id(stage.get("stage")) != current_stage:
            continue
        ids = {str(value) for value in stage.get(f"{role}_evidence_ids") or [] if str(value).strip()}
        flag = stage.get(f"{role}_{suffix}") if suffix else None
        if isinstance(flag, dict):
            ids.update(str(value) for value in flag.get("evidence_ids") or [] if str(value).strip())
        return ids
    return set()


def _human_key_event_audit(
    labels: dict[str, Any],
    run_paths: dict[str, Path],
) -> dict[str, Any]:
    """按人工独立 key-event 标注，拆开 Stage1 抽取与 Stage2 使用两个召回环节。

    可选标签格式：
    key_events: [{
      "id": "creator_s3_application", "role": "creator", "stage": "S3",
      "time_range": [12.0, 18.0], "required_functions": ["S3_usage"],
      "channels_any": ["visual_fact"], "terms_any": ["涂抹", "按压"]
    }]
    time_range、role、stage 是必填；其余条件只用于收紧匹配，不是让人工重写模型事实。
    """
    label_samples = labels.get("samples") if isinstance(labels.get("samples"), dict) else {}
    records: list[dict[str, Any]] = []
    invalid: list[dict[str, str]] = []
    annotated_samples = 0
    for sample_id, label in sorted(label_samples.items()):
        events = label.get("key_events") if isinstance(label, dict) else None
        if not isinstance(events, list) or not events:
            continue
        annotated_samples += 1
        path = run_paths.get(sample_id)
        if path is None or not path.exists():
            invalid.append({"sample_id": sample_id, "reason": "有 key_events 但缺 analysis.json"})
            continue
        result = read_json(path)
        for index, event in enumerate(events, start=1):
            if not isinstance(event, dict):
                invalid.append({"sample_id": sample_id, "reason": f"key_events[{index}] 不是对象"})
                continue
            role = str(event.get("role") or "").strip()
            current_stage = str(event.get("stage") or "").strip().upper()
            time_range = _event_time_bounds(event.get("time_range"))
            if role not in {"creator", "benchmark"} or current_stage not in FLAG_SUFFIXES or time_range is None:
                invalid.append({"sample_id": sample_id, "reason": f"key_events[{index}] 缺 role/stage/time_range"})
                continue
            required_functions = {str(value) for value in event.get("required_functions") or [] if str(value).strip()}
            channels_any = [str(value) for value in event.get("channels_any") or [] if str(value).strip()]
            terms_any = [str(value).lower() for value in event.get("terms_any") or [] if str(value).strip()]
            matches: list[str] = []
            for unit_id, unit in _role_units(result, role).items():
                unit_range = _event_time_bounds(unit.get("time_range"))
                if unit_range is None or not _ranges_overlap(time_range, unit_range):
                    continue
                functions = {str(value) for value in unit.get("functions") or [] if str(value).strip()}
                if required_functions and not required_functions.issubset(functions):
                    continue
                if channels_any and not any(str(unit.get(channel) or "").strip() for channel in channels_any):
                    continue
                unit_text = json.dumps(unit, ensure_ascii=False).lower()
                if terms_any and not any(term in unit_text for term in terms_any):
                    continue
                matches.append(unit_id)
            referenced = _stage_referenced_ids(result, current_stage, role)
            records.append({
                "sample_id": sample_id,
                "event_id": str(event.get("id") or f"event_{index}"),
                "role": role,
                "stage": current_stage,
                "time_range": event.get("time_range"),
                "stage1_recalled": bool(matches),
                "stage1_matching_evidence_ids": matches,
                "stage2_referenced": bool(set(matches) & referenced),
                "stage2_referenced_evidence_ids": sorted(set(matches) & referenced),
            })
    if not records:
        status = "unavailable_without_human_key_events"
    elif invalid:
        status = "partial_invalid_annotations"
    else:
        status = "measured"
    stage1_recalled = sum(1 for row in records if row["stage1_recalled"])
    stage2_referenced = sum(1 for row in records if row["stage2_referenced"])
    return {
        "status": status,
        "annotation_contract": {
            "required": ["id", "role", "stage", "time_range"],
            "optional": ["required_functions", "channels_any", "terms_any"],
        },
        "summary": {
            "annotated_samples": annotated_samples,
            "events": len(records),
            "stage1_recalled": stage1_recalled,
            "stage1_recall": round(stage1_recalled / len(records), 4) if records else None,
            "stage2_referenced": stage2_referenced,
            "stage2_use_given_recall": round(stage2_referenced / stage1_recalled, 4) if stage1_recalled else None,
        },
        "missed_by_stage1": [row for row in records if not row["stage1_recalled"]],
        "not_used_by_stage2": [row for row in records if row["stage1_recalled"] and not row["stage2_referenced"]],
        "invalid_annotations": invalid,
        "records": records,
    }


def _stage1_audit_contract(run_paths: dict[str, Path]) -> dict[str, Any]:
    """检查最终产物是否保留可审计的 Stage1 facts，不把字段缺失误判为事实未发生。"""
    records: list[dict[str, Any]] = []
    for sample_id, path in sorted(run_paths.items()):
        if not path.exists():
            continue
        result = read_json(path)
        for role in ("benchmark", "creator"):
            side = result.get("video_understanding", {}).get(role, {})
            side = side if isinstance(side, dict) else {}
            units = _role_units(result, role)
            checklist = side.get("evidence_checklist")
            checks = side.get("structure_event_checks")
            check_items = checks if isinstance(checks, list) else []
            module_ids = [str(item.get("module_id") or "") for item in check_items if isinstance(item, dict)]
            unexpected = sorted(set(module_ids) - set(STAGE1_EVENT_IDS))
            missing = [module_id for module_id in STAGE1_EVENT_IDS if module_id not in module_ids]
            duplicates = sorted({module_id for module_id in module_ids if module_ids.count(module_id) > 1})
            invalid_present_evidence = sorted(
                {
                    str(evidence_id)
                    for item in check_items
                    if isinstance(item, dict) and item.get("present") is True
                    for evidence_id in item.get("evidence_ids") or []
                    if str(evidence_id) not in units
                }
            )
            records.append({
                "sample_id": sample_id,
                "role": role,
                "checklist_present": isinstance(checklist, list),
                "checklist_items": len(checklist) if isinstance(checklist, list) else 0,
                "event_checks_present": isinstance(checks, list),
                "event_check_count": len(check_items),
                "missing_event_modules": missing,
                "unexpected_event_modules": unexpected,
                "duplicate_event_modules": duplicates,
                "invalid_present_evidence_ids": invalid_present_evidence,
            })
    complete = [
        row for row in records
        if row["checklist_present"]
        and row["event_checks_present"]
        and not row["missing_event_modules"]
        and not row["unexpected_event_modules"]
        and not row["duplicate_event_modules"]
        and not row["invalid_present_evidence_ids"]
    ]
    return {
        "expected_event_modules": list(STAGE1_EVENT_IDS),
        "summary": {
            "role_artifacts": len(records),
            "complete_role_artifacts": len(complete),
            "complete_rate": round(len(complete) / len(records), 4) if records else None,
        },
        "violations": [row for row in records if row not in complete],
        "records": records,
    }


def chain_audit(run_paths: dict[str, Path], labels: dict[str, Any]) -> dict[str, Any]:
    """汇总 Stage1→Stage2→derive 的可观测契约健康度。"""
    records: list[dict[str, Any]] = []
    missing_runs: list[dict[str, str]] = []
    for sample_id, path in sorted(run_paths.items()):
        if not path.exists():
            missing_runs.append({"sample_id": sample_id, "path": str(path)})
            continue
        records.extend(_flag_chain_audit(read_json(path), sample_id))

    present = [row for row in records if row.get("flag_present")]
    evidence_expected = [row for row in present if row.get("evidence_ids")]
    unresolved = [row for row in present if row.get("unresolved_evidence_ids")]
    placeholders = [row for row in present if row.get("placeholder_evidence_ids")]
    unclassified = [row for row in present if row.get("unclassified_populated_fields")]
    derived_fields = sum(len((row.get("field_ownership") or {}).get("derive") or []) for row in present)
    cross_stage_fields = sum(len((row.get("field_ownership") or {}).get("cross_stage") or []) for row in present)
    qa_report_fields = sum(len((row.get("field_ownership") or {}).get("qa_report") or []) for row in present)
    human_events = _human_key_event_audit(labels, run_paths)
    stage1_contract = _stage1_audit_contract(run_paths)
    return {
        "schema_version": 3,
        "scope": "artifact_contract_audit",
        "limitations": [
            "引用完整性不等于 Stage1 事实召回率；human_key_event_audit 只在人工独立 key-event 标注存在时计算事实召回。",
            "字段所有权用于审计代码去向；qa_report 字段不应被误判为 derive 信息丢失。",
        ],
        "summary": {
            "flags_expected": len(records),
            "flags_present": len(present),
            "flags_missing": len(records) - len(present),
            "flags_with_evidence": len(evidence_expected),
            "flags_with_unresolved_evidence": len(unresolved),
            "flags_with_placeholder_evidence": len(placeholders),
            "flags_with_unclassified_populated_fields": len(unclassified),
            "populated_fields_consumed_by_derive": derived_fields,
            "populated_fields_consumed_by_cross_stage": cross_stage_fields,
            "populated_fields_reserved_for_qa_or_report": qa_report_fields,
            "human_event_recall": human_events["status"],
            "stage1_audit_contract_complete_rate": stage1_contract["summary"]["complete_rate"],
        },
        "evidence_integrity_violations": unresolved,
        "placeholder_evidence_records": placeholders,
        "unclassified_field_records": unclassified,
        "human_key_event_audit": human_events,
        "stage1_audit_contract": stage1_contract,
        "records": records,
        "missing_runs": missing_runs,
    }


def whole_video_model_observation(sample_id: str, label: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """保留全片可行性样本的人工裁决和模型原始结论，不伪造阶段准确率。"""
    return {
        "sample_id": sample_id,
        "partition": str(label.get("partition") or "unknown"),
        "expected_verdict": str(label.get("overall_verdict") or "").strip().lower(),
        "expected_reason": str(label.get("overall_reason") or "").strip(),
        "model_output": {
            "one_line_verdict": result.get("one_line_verdict"),
            "one_line_summary": result.get("one_line_summary"),
            "executive_summary": result.get("executive_summary"),
            "holistic_assessment": result.get("holistic_assessment"),
            "key_conclusions": result.get("key_conclusions"),
        },
        "evaluation": "human_review_required",
    }


def video_path(result: dict[str, Any], role: str) -> str:
    videos = result.get("videos")
    side = videos.get(role) if isinstance(videos, dict) else None
    return str(side.get("path") or "") if isinstance(side, dict) else ""


def blind_contract_violations(labels: dict[str, Any], manifest: dict[str, Any]) -> list[str]:
    """校验盲测标签和输入清单没有把历史开发视频重新命名成 blind。"""
    label_samples = labels.get("samples") if isinstance(labels.get("samples"), dict) else {}
    inputs = manifest_samples(manifest)
    historical_paths: dict[str, str] = {}
    for sample_id, sample in inputs.items():
        if str(sample.get("group") or "") == "blind":
            continue
        for field in ("benchmark_video", "creator_video"):
            path = str(sample.get(field) or "").strip()
            if path:
                historical_paths[path] = sample_id

    violations: list[str] = []
    for sample_id, sample in sorted(inputs.items()):
        if sample.get("group") != "blind":
            continue
        label = label_samples.get(sample_id)
        if not isinstance(label, dict):
            violations.append(f"blind {sample_id} 缺少 GT")
            continue
        if label.get("partition") != "blind":
            violations.append(f"blind {sample_id} 的 GT partition 不是 blind")
        evaluation_scope = str(sample.get("evaluation_scope") or STAGE_SEVERITY_SCOPE).strip()
        if evaluation_scope not in {STAGE_SEVERITY_SCOPE, WHOLE_VIDEO_OBSERVATION_SCOPE}:
            violations.append(f"blind {sample_id} 使用未知 evaluation_scope：{evaluation_scope}")
        if evaluation_scope == WHOLE_VIDEO_OBSERVATION_SCOPE:
            if label.get("evaluation_scope") != WHOLE_VIDEO_OBSERVATION_SCOPE:
                violations.append(f"blind {sample_id} 的 GT 未标记为 whole_video_observation")
            verdict = str(label.get("overall_verdict") or "").strip().lower()
            if verdict not in WHOLE_VIDEO_VERDICTS:
                violations.append(f"blind {sample_id} 缺少有效 overall_verdict")
            if not str(label.get("overall_reason") or "").strip():
                violations.append(f"blind {sample_id} 缺少 overall_reason")
            continue
        stages = label.get("stages") if isinstance(label.get("stages"), dict) else {}
        missing_stages = [
            stage
            for stage in ("S1", "S2", "S3", "S4", "S5", "S6")
            if normalize_ground_truth(stages.get(stage)) is None
        ]
        if missing_stages:
            violations.append(f"blind {sample_id} 缺少 GT：{','.join(missing_stages)}")
        for field in ("benchmark_video", "creator_video"):
            path = str(sample.get(field) or "").strip()
            if path in historical_paths:
                violations.append(f"blind {sample_id} 复用了 {historical_paths[path]} 的 {field}")

    for sample_id, label in sorted(label_samples.items()):
        if not isinstance(label, dict) or label.get("partition") != "blind":
            continue
        sample = inputs.get(sample_id)
        if not isinstance(sample, dict):
            violations.append(f"blind {sample_id} 缺少 validation-inputs 条目")
        elif sample.get("group") != "blind":
            violations.append(f"blind {sample_id} 的输入 group 不是 blind")
        elif str(label.get("evaluation_scope") or STAGE_SEVERITY_SCOPE).strip() != str(
            sample.get("evaluation_scope") or STAGE_SEVERITY_SCOPE
        ).strip():
            violations.append(f"blind {sample_id} 的 GT evaluation_scope 与输入不一致")
    return violations


def promotion_readiness(
    rows: list[dict[str, Any]],
    labels: dict[str, Any],
    manifest: dict[str, Any],
    shadow_invariance_violations: list[dict[str, Any]],
) -> dict[str, Any]:
    """给 shadow→severity 晋级提供硬门，不让实验字段被提前消费。"""
    samples = manifest_samples(manifest)
    categories_by_partition: dict[str, set[str]] = defaultdict(set)
    sample_ids_by_partition: dict[str, set[str]] = defaultdict(set)
    stage_coverage: dict[str, dict[str, Any]] = {}
    reasons: list[str] = []
    for row in rows:
        partition = row["partition"]
        sample_id = row["sample_id"]
        sample_ids_by_partition[partition].add(sample_id)
        category = str((samples.get(sample_id) or {}).get("product_category") or "").strip()
        if category:
            categories_by_partition[partition].add(category)

    calibration_categories = set()
    for partition in ("calibration", "seen_validation"):
        calibration_categories.update(categories_by_partition.get(partition, set()))
    if len(calibration_categories) < PROMOTION_MIN_CATEGORIES:
        reasons.append(f"校准/已见验证覆盖品类不足 {PROMOTION_MIN_CATEGORIES} 个")

    for stage in SHADOW_STAGES:
        stage_rows = [row for row in rows if row["stage"] == stage]
        gap_examples = sum(1 for row in stage_rows if row["expected"] in {"medium", "large"})
        control_examples = sum(1 for row in stage_rows if row["expected"] == "small")
        shadow_role_count = sum(
            1
            for row in stage_rows
            for role in ("creator", "benchmark")
            if isinstance(row[role].get("shadow"), dict)
        )
        shadow_expected = len(stage_rows) * 2
        stage_coverage[stage] = {
            "evaluated_pairs": len(stage_rows),
            "gap_examples": gap_examples,
            "no_gap_controls": control_examples,
            "shadow_role_coverage": shadow_role_count,
            "shadow_role_expected": shadow_expected,
        }
        if gap_examples < PROMOTION_MIN_SAMPLES_PER_STAGE:
            reasons.append(f"{stage} 缺少至少 {PROMOTION_MIN_SAMPLES_PER_STAGE} 个中/大差距样本")
        if control_examples < PROMOTION_MIN_SAMPLES_PER_STAGE:
            reasons.append(f"{stage} 缺少至少 {PROMOTION_MIN_SAMPLES_PER_STAGE} 个 small 对照样本")
        if shadow_role_count != shadow_expected:
            reasons.append(f"{stage} shadow 覆盖不完整")

    blind_contract = manifest.get("blind_validation_contract") if isinstance(manifest.get("blind_validation_contract"), dict) else {}
    min_blind_samples = int(blind_contract.get("minimum_samples") or PROMOTION_MIN_SAMPLES_PER_STAGE)
    blind_count = len(sample_ids_by_partition.get("blind", set()))
    if blind_count < min_blind_samples:
        reasons.append(f"缺少至少 {min_blind_samples} 个全新 blind 样本")
    blind_violations = blind_contract_violations(labels, manifest)
    reasons.extend(blind_violations)
    if shadow_invariance_violations:
        reasons.append("shadow 在同一视频跨配对中出现执行分漂移")
    # 当前 shadow 尚未获准生成 candidate severity；此门防止直接替换正式输出。
    reasons.append("shadow 尚未通过候选 severity 离线对账，不得接入正式 severity")
    return {
        "eligible": False,
        "policy": {
            "min_gap_examples_per_stage": PROMOTION_MIN_SAMPLES_PER_STAGE,
            "min_no_gap_controls_per_stage": PROMOTION_MIN_SAMPLES_PER_STAGE,
            "min_categories": PROMOTION_MIN_CATEGORIES,
            "min_new_blind_samples": min_blind_samples,
        },
        "coverage": {
            "samples_by_partition": {key: len(value) for key, value in sorted(sample_ids_by_partition.items())},
            "categories": sorted(calibration_categories),
            "stages": stage_coverage,
            "shadow_invariance_violations": len(shadow_invariance_violations),
            "blind_contract_violations": blind_violations,
        },
        "reasons": list(dict.fromkeys(reasons)),
    }


def evaluate(
    labels: dict[str, Any],
    manifest: dict[str, Any],
    run_paths: dict[str, Path],
) -> dict[str, Any]:
    label_samples = labels.get("samples") if isinstance(labels.get("samples"), dict) else {}
    input_samples = manifest_samples(manifest)
    rows: list[dict[str, Any]] = []
    missing_runs: list[dict[str, str]] = []
    missing_labels: list[str] = []
    whole_video_observations: list[dict[str, Any]] = []
    invariance: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    shadow_invariance: dict[tuple[str, str, str], set[str]] = defaultdict(set)

    for sample_id, path in sorted(run_paths.items()):
        if not path.exists():
            missing_runs.append({"sample_id": sample_id, "path": str(path)})
            continue
        label = label_samples.get(sample_id)
        if not isinstance(label, dict):
            missing_labels.append(sample_id)
            continue
        result = read_json(path)
        allowed, scope_source = eligible_stages(sample_id, label, input_samples.get(sample_id), result)
        if scope_source == WHOLE_VIDEO_OBSERVATION_SCOPE:
            whole_video_observations.append(whole_video_model_observation(sample_id, label, result))
            continue
        stages = result.get("stage_analysis")
        if not isinstance(stages, list):
            missing_runs.append({"sample_id": sample_id, "path": str(path), "reason": "missing_stage_analysis"})
            continue
        by_id = {stage_id(stage.get("stage")): stage for stage in stages if isinstance(stage, dict) and stage_id(stage.get("stage"))}
        expected_stages = label.get("stages") if isinstance(label.get("stages"), dict) else {}
        for current_stage in sorted(allowed):
            stage = by_id.get(current_stage)
            expected = normalize_severity(expected_stages.get(current_stage))
            if not isinstance(stage, dict) or expected is None:
                continue
            final = normalize_severity(stage.get("severity"))
            if final is None:
                continue
            direction = "matched"
            if final != expected:
                direction = "underestimated" if SEVERITY_RANK[final] < SEVERITY_RANK[expected] else "overestimated"
            row = {
                "sample_id": sample_id,
                "partition": str(label.get("partition") or "unknown"),
                "run_path": str(path),
                "stage": current_stage,
                "scope_source": scope_source,
                "expected": expected,
                "final": final,
                "model": normalize_severity(stage.get("model_severity")),
                "matched": final == expected,
                "direction": direction,
                "diagnosis": diagnosis(expected, final, stage),
                "creator": side_execution(stage, "creator"),
                "benchmark": side_execution(stage, "benchmark"),
            }
            row.update(severity_diagnostics(expected, final, stage))
            rows.append(row)
            for role in ("creator", "benchmark"):
                path_key = video_path(result, role)
                execution = row[role]["execution"]
                if path_key and execution is not None:
                    invariance[(role, path_key, current_stage)].add(str(execution))
                shadow = row[role]["shadow"]
                shadow_score = shadow.get("score") if isinstance(shadow, dict) else None
                if path_key and shadow_score is not None:
                    shadow_invariance[(role, path_key, current_stage)].add(str(shadow_score))

    stage_counts: dict[str, Counter[str]] = defaultdict(Counter)
    direction_counts: Counter[str] = Counter()
    diagnosis_counts: Counter[str] = Counter()
    distance_counts: Counter[str] = Counter()
    decision_mechanism_counts: Counter[str] = Counter()
    matched_mechanism_counts: Counter[str] = Counter()
    mismatch_mechanism_counts: Counter[str] = Counter()
    mechanism_by_stage: dict[str, dict[str, Counter[str]]] = defaultdict(
        lambda: {"all": Counter(), "matched": Counter(), "mismatched": Counter()}
    )
    confusion = {
        expected: {final: 0 for final in SEVERITIES}
        for expected in SEVERITIES
    }
    partition_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        stage_counts[row["stage"]]["total"] += 1
        stage_counts[row["stage"]][row["direction"]] += 1
        direction_counts[row["direction"]] += 1
        diagnosis_counts[row["diagnosis"]] += 1
        distance_counts[f"off_by_{row['ordinal_distance']}"] += 1
        decision_mechanism_counts[row["decision_mechanism"]] += 1
        mechanism_by_stage[row["stage"]]["all"][row["decision_mechanism"]] += 1
        if row["matched"]:
            matched_mechanism_counts[row["decision_mechanism"]] += 1
            mechanism_by_stage[row["stage"]]["matched"][row["decision_mechanism"]] += 1
        if not row["matched"]:
            mismatch_mechanism_counts[row["decision_mechanism"]] += 1
            mechanism_by_stage[row["stage"]]["mismatched"][row["decision_mechanism"]] += 1
        confusion[row["expected"]][row["final"]] += 1
        partition_counts[row["partition"]]["total"] += 1
        partition_counts[row["partition"]]["matched"] += int(row["matched"])

    unstable = [
        {
            "role": role,
            "video_path": path,
            "stage": stage,
            "execution_values": sorted(values, key=float),
        }
        for (role, path, stage), values in sorted(invariance.items())
        if len(values) > 1
    ]
    mismatches = [row for row in rows if not row["matched"]]
    threshold_mismatches = [row for row in mismatches if row["derivation_path"] == "threshold"]
    near_threshold_mismatches = [row for row in threshold_mismatches if row["near_threshold"] is True]
    away_from_threshold_mismatches = [row for row in threshold_mismatches if row["near_threshold"] is False]
    override_mismatches = [row for row in mismatches if row["derivation_path"] == "override_or_floor"]
    non_score_mismatches = [row for row in mismatches if row["derivation_path"] == "non_score_path"]
    shadow_unstable = [
        {
            "role": role,
            "video_path": path,
            "stage": stage,
            "score_values": sorted(values, key=float),
        }
        for (role, path, stage), values in sorted(shadow_invariance.items())
        if len(values) > 1
    ]
    readiness = promotion_readiness(rows, labels, manifest, shadow_unstable)
    return {
        "schema_version": 2,
        "sources": {
            "ground_truth": labels.get("source"),
            "ground_truth_policy": labels.get("policy"),
            "uses_final_analysis_json": True,
        },
        "summary": {
            "evaluated": len(rows),
            "matched": sum(1 for row in rows if row["matched"]),
            "accuracy": round(sum(1 for row in rows if row["matched"]) / len(rows), 4) if rows else None,
            "ordinal_distance": dict(sorted(distance_counts.items())),
            "two_band_errors": distance_counts.get("off_by_2", 0),
            "directions": dict(sorted(direction_counts.items())),
            "diagnoses": dict(sorted(diagnosis_counts.items())),
            "by_partition": {key: dict(value) for key, value in sorted(partition_counts.items())},
            "whole_video_observations": len(whole_video_observations),
        },
        "by_stage": {key: dict(value) for key, value in sorted(stage_counts.items())},
        "confusion_matrix": {
            "rows": "ground_truth",
            "columns": "final_severity",
            "values": confusion,
        },
        "boundary_diagnostics": {
            "policy": {
                "thresholds": {"small_medium": TH_SMALL, "medium_large": TH_MEDIUM},
                "critical_band": CRITICAL_BAND,
                "interpretation": "near_threshold 仅标记可能的离散化边界噪声，不把 mismatch 改判为正确。红线、下限和断层覆盖路径不参与该解释。",
            },
            "mismatches": len(mismatches),
            "threshold_path": len(threshold_mismatches),
            "near_threshold": len(near_threshold_mismatches),
            "away_from_threshold": len(away_from_threshold_mismatches),
            "override_or_floor": len(override_mismatches),
            "non_score_path": len(non_score_mismatches),
            "mismatch_decision_mechanisms": dict(sorted(mismatch_mechanism_counts.items())),
            "all_decision_mechanisms": dict(sorted(decision_mechanism_counts.items())),
            "matched_decision_mechanisms": dict(sorted(matched_mechanism_counts.items())),
            "decision_mechanisms_by_stage": {
                stage: {
                    bucket: dict(sorted(counts.items()))
                    for bucket, counts in groups.items()
                }
                for stage, groups in sorted(mechanism_by_stage.items())
            },
        },
        "decision_level_evaluation": {
            "status": "unavailable_without_human_priority_and_root_cause_labels",
            "reason": "当前 GT 只有阶段 severity/关系和少量全局观察，没有可对账的人工 Top-N 根因及优先级；不能用阶段排名相同冒充商业决策一致。",
        },
        "mismatches": mismatches,
        "execution_invariance_violations": unstable,
        "shadow_execution_invariance_violations": shadow_unstable,
        "whole_video_observations": whole_video_observations,
        "promotion_readiness": readiness,
        "chain_audit": chain_audit(run_paths, labels),
        "missing_runs": missing_runs,
        "missing_labels": sorted(missing_labels),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="评测 Flayr 最终 analysis.json 与人工 GT 的一致性")
    parser.add_argument("--labels", type=Path, default=Path("references/ground-truth-labels.json"))
    parser.add_argument("--manifest", type=Path, default=Path("references/validation-inputs.json"))
    parser.add_argument("--runs-root", type=Path, default=Path("runs"))
    parser.add_argument("--run-prefix", default="contract-", help="默认以此前缀查找 <sample>/analysis.json")
    parser.add_argument("--sample", action="append", default=[], help="只评测指定 sample id，可重复传入")
    parser.add_argument(
        "--run-path",
        action="append",
        default=[],
        metavar="SAMPLE_ID=PATH",
        help="显式指定某个 sample 的 analysis.json，覆盖目录前缀推导；可重复传入",
    )
    parser.add_argument("--output", type=Path, required=True, help="评测结果 JSON 输出路径")
    args = parser.parse_args()

    labels = read_json(args.labels)
    manifest = read_json(args.manifest)
    samples = labels.get("samples") if isinstance(labels.get("samples"), dict) else {}
    try:
        explicit_paths = parse_explicit_run_paths(args.run_path)
    except ValueError as exc:
        parser.error(str(exc))
    selected = args.sample or sorted(explicit_paths) or sorted(samples)
    run_paths = {sample_id: sample_run_path(args.runs_root, sample_id, args.run_prefix) for sample_id in selected}
    run_paths.update(explicit_paths)
    report = evaluate(labels, manifest, run_paths)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = report["summary"]
    print(f"evaluated={summary['evaluated']} matched={summary['matched']} accuracy={summary['accuracy']}")
    print(f"directions={summary['directions']}")
    print(f"diagnoses={summary['diagnoses']}")
    print(f"output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
