"""flayr_core.postprocess.repair_evidence：证据单元的回填、占位与对齐。

从 repair.py 按 region 簇拆出（2026-06-15，零跨模块依赖）：
  - bind_*        用真实数据回填 stage / improvement
  - reconcile_*   补占位 evidence_unit 让 schema 完整
  - ground_*      把字段对齐到所引用 evidence_unit 的事实
  - fill_*        修复阶段引用 evidence_unit 时间错位
  - materialize_* 阶段有口播但缺时段证据时造一个 stage 占位单元
  - deduplicate_* 去除跨阶段重复 quote 子句
所有函数都是"修改 result data 后正常返回"，不抛 SystemExit。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..artifacts import (
    format_seconds,
    parse_time_range_seconds,
    parse_timestamp_seconds,
)
from ..evidence_states import (
    S2_HARD_FACT_FIELDS,
    S3_HARD_FACT_FIELDS,
    S4_HARD_FACT_FIELDS,
    S3_USAGE_EVIDENCE_STATES,
    S4_EFFECT_EVIDENCE_STATES,
    hard_fact_fingerprint,
    s2_hard_fact_snapshot,
)
from ..llm.parse import S5_SOURCE_STATUSES, is_effective_voiceover, normalize_s5_source_status
from .utils import (
    adjacent_review_range,
    ensure_evidence_unit,
    evidence_mentions_product,
    evidence_overlaps_range,
    evidence_unit_at_time,
    nearest_evidence_unit,
    nearest_product_evidence_unit,
    referenced_spoken_unit,
)


# region bind ----------------------------------------------------------------

def bind_timed_transcript_quotes(result: dict[str, Any], analysis: dict[str, Any]) -> None:
    """用 SRT 时间戳重新校对每个阶段的 quote，并清除已知的"视觉证据"占位。"""
    videos = analysis.get("videos", {})
    for stage in result.get("stage_analysis", []):
        for role in ("benchmark", "creator"):
            references = [str(value) for value in stage.get(f"{role}_evidence_ids", [])]
            if any("_NO_" in value for value in references):
                stage[f"{role}_quote"] = ""
                stage[f"{role}_quote_zh"] = ""


def align_stage_flag_evidence(result: dict[str, Any]) -> None:
    """统一阶段主引用与嵌套 flag 引用，只在已有真实 evidence 间对齐。

    Repair/Phase C 有时只补全 flag.evidence_ids，若先给 stage 主字段造 NO_STAGE 占位，
    报告会出现“flag 有真实画面、主结论却无证据”的自相矛盾。此处先双向恢复真实引用，
    后续 fill_missing_evidence_references 才只处理双方都没有事实引用的情形。
    """
    flag_names = {1: "hook", 2: "s2", 3: "s3", 4: "s4", 5: "s5", 6: "s6"}
    understanding = result.get("video_understanding", {})
    for index, stage in enumerate(result.get("stage_analysis", []), start=1):
        flag_name = flag_names.get(index)
        if not flag_name or not isinstance(stage, dict):
            continue
        for role in ("benchmark", "creator"):
            flag = stage.get(f"{role}_{flag_name}")
            stage_key = f"{role}_evidence_ids"
            stage_ids = [
                str(value)
                for value in stage.get(stage_key, [])
                if str(value).strip()
            ]
            units = understanding.get(role, {}).get("evidence_units", [])
            # 多模态判断和阶段主结论消费同一批锁定事实。模型可能在 channel_evidence_ids
            # 引用同阶段真实单元，却漏把它同步到主 evidence_ids；只将同侧、真实且与阶段
            # 时间相交的引用并入主列表，未知或跨时段引用仍留给 validator 阻断。
            multimodal = stage.get(f"{role}_multimodal")
            channel_refs = multimodal.get("channel_evidence_ids") if isinstance(multimodal, dict) else None
            if isinstance(channel_refs, dict):
                multimodal_ids = list(dict.fromkeys(
                    str(value)
                    for refs in channel_refs.values()
                    if isinstance(refs, list)
                    for value in refs
                    if str(value).strip() and "_NO_" not in str(value)
                ))
                valid_multimodal_ids = {
                    str(unit.get("id"))
                    for unit in units
                    if isinstance(unit, dict)
                    and str(unit.get("id")) in multimodal_ids
                    and evidence_overlaps_range(unit, stage.get(f"{role}_time_range"))
                }
                for evidence_id in multimodal_ids:
                    if evidence_id in valid_multimodal_ids and evidence_id not in stage_ids:
                        stage_ids.append(evidence_id)
                if stage_ids:
                    stage[stage_key] = stage_ids

            if not isinstance(flag, dict):
                continue
            flag_ids = [str(value) for value in flag.get("evidence_ids", []) if str(value).strip()]
            flag_units = [
                unit
                for unit in units
                if isinstance(unit, dict)
                and str(unit.get("id")) in flag_ids
                and evidence_overlaps_range(unit, stage.get(f"{role}_time_range"))
            ]
            if flag_units and (not stage_ids or any("_NO_" in value for value in stage_ids)):
                restored_ids = [str(unit.get("id")) for unit in flag_units]
                stage[stage_key] = restored_ids
                primary = flag_units[0]
                for key in ("summary", "key_message"):
                    field = f"{role}_{key}"
                    current = str(stage.get(field) or "").strip()
                    if not current or current.startswith("（LLM 未填写"):
                        stage[field] = str(primary.get("information") or primary.get("visual_fact") or "").strip()
                if not is_effective_voiceover(stage.get(f"{role}_quote")):
                    stage[f"{role}_quote"] = str(primary.get("voiceover") or "").strip()
                    stage[f"{role}_quote_zh"] = str(primary.get("voiceover_zh") or "").strip()
                stage[f"{role}_support_status"] = (
                    "supported" if is_effective_voiceover(primary.get("voiceover")) else "visual_only"
                )
                stage_ids = restored_ids
            if stage_ids and not flag_ids:
                flag["evidence_ids"] = stage_ids
                stage[f"{role}_support_status"] = "visual_only"
                continue
            if flag_ids:
                continue
            reliable_unit = referenced_spoken_unit(result, stage, role)
            if reliable_unit:
                stage[f"{role}_quote"] = str(reliable_unit.get("voiceover") or "")
                stage[f"{role}_quote_zh"] = str(reliable_unit.get("voiceover_zh") or "")
                continue


def prune_multimodal_evidence_to_stage(result: dict[str, Any]) -> None:
    """在阶段证据最终收口后，裁掉跨模态字段里已不属于该阶段的旧引用。

    只做集合交集，不新增证据：允许集合由阶段主引用与 Stage1 锁定的同阶段
    ``functions`` 共同构成。这样 S5 等后续专项修复收窄主引用后，不会留下
    已过期的渠道引用，也不需要让 LLM repair 改写 evidence ids。
    """
    stage_functions = {
        "S1": "S1_hook",
        "S2": "S2_intro",
        "S3": "S3_usage",
        "S4": "S4_effect",
        "S5": "S5_trust",
        "S6": "S6_cta",
    }
    understanding = result.get("video_understanding") if isinstance(result.get("video_understanding"), dict) else {}
    for index, stage in enumerate(result.get("stage_analysis", []), start=1):
        if not isinstance(stage, dict):
            continue
        stage_id = f"S{index}"
        function = stage_functions.get(stage_id)
        for role in ("creator", "benchmark"):
            assessment = stage.get(f"{role}_multimodal")
            channel_refs = assessment.get("channel_evidence_ids") if isinstance(assessment, dict) else None
            if not isinstance(channel_refs, dict):
                continue
            allowed = {
                str(item)
                for item in stage.get(f"{role}_evidence_ids") or []
                if str(item).strip()
            }
            role_understanding = understanding.get(role) if isinstance(understanding.get(role), dict) else {}
            allowed.update(
                str(unit.get("id"))
                for unit in role_understanding.get("evidence_units") or []
                if isinstance(unit, dict)
                and function in {str(value) for value in unit.get("functions") or []}
            )
            for channel, refs in channel_refs.items():
                if isinstance(refs, list):
                    channel_refs[channel] = [
                        str(item) for item in refs if str(item).strip() and str(item) in allowed
                    ]


def _check_evidence_state_consistency(
    flag: Any,
    *,
    state_key: str,
    allowed_states: tuple[str, ...],
    state_complete: str,
    state_none: str,
    required_fields: tuple[str, ...],
    positive_fields: tuple[str, ...],
    must_be_true_fields: tuple[str, ...] = (),
    state_must_be_true: dict[str, tuple[str, ...]] | None = None,
    state_must_be_false: dict[str, tuple[str, ...]] | None = None,
    result_only: str | None = None,
) -> dict[str, Any]:
    """Check only impossible state/boolean combinations.

    This function deliberately does not decide whether a video is ``partial``
    or ``none``. That semantic state is an upstream fact. Missing or unstable
    inputs become ``incomplete`` and are refused by derive.
    """
    if not isinstance(flag, dict):
        return {
            "status": "incomplete",
            "reason_code": "missing_field",
            "checked_fields": [],
        }
    state = flag.get(state_key)
    if state not in allowed_states:
        return {
            "status": "incomplete",
            "reason_code": "evidence_state_missing",
            "state": state,
            "checked_fields": [],
        }
    missing = [key for key in required_fields if flag.get(key) is not True and flag.get(key) is not False]
    if missing:
        return {
            "status": "incomplete",
            "reason_code": "hard_fact_missing",
            "state": state,
            "missing_fields": missing,
            "checked_fields": list(required_fields),
        }
    positive = [key for key in positive_fields if flag.get(key) is True]
    impossible: list[str] = []
    for key in (state_must_be_true or {}).get(state, ()):
        if flag.get(key) is not True:
            impossible.append(key)
    for key in (state_must_be_false or {}).get(state, ()):
        if flag.get(key) is not False:
            impossible.append(key)
    if state == state_complete:
        impossible.extend(key for key in must_be_true_fields if flag.get(key) is False)
        if flag.get("fake_or_staged") is True:
            impossible.append("fake_or_staged")
    if state == state_none:
        impossible.extend(positive)
    if result_only is not None:
        if state == result_only:
            if flag.get("process_linked_effect") is True:
                impossible.append("process_linked_effect")
            if flag.get("result_only_without_process") is False:
                impossible.append("result_only_without_process")
        elif state == state_complete and flag.get("result_only_without_process") is True:
            impossible.append("result_only_without_process")
    if impossible:
        return {
            "status": "state_conflict",
            "reason_code": "state_hard_fact_conflict",
            "state": state,
            "conflicting_fields": sorted(set(impossible)),
            "checked_fields": list(required_fields),
        }
    return {
        "status": "consistent",
        "reason_code": "predicate_not_met",
        "state": state,
        "checked_fields": list(required_fields),
    }


_STAGE_FLAG_NAMES = {
    1: "hook",
    2: "s2",
    3: "s3",
    4: "s4",
    5: "s5",
    6: "s6",
}


def _check_evidence_reference_temporal_consistency(
    units: Any,
    references: Any,
    stage_range: Any,
    *,
    reference: str,
) -> dict[str, Any]:
    """Check that referenced locked facts overlap the recorded stage window.

    This is a read-only contract check. It deliberately reuses
    ``evidence_overlaps_range`` so repair, validation, and Phase C keep the
    same strict-positive-overlap semantics: a shared endpoint is not overlap;
    a unit crossing a boundary still overlaps both windows and is not rejected
    by this rule alone.
    """
    result: dict[str, Any] = {
        "reference": reference,
        "stage_time_range": stage_range,
    }
    if references is None:
        return {
            **result,
            "status": "incomplete",
            "reason_code": "missing_field",
            "evidence_ids": [],
        }
    if not isinstance(references, list):
        return {
            **result,
            "status": "incomplete",
            "reason_code": "missing_field",
            "evidence_ids": [],
        }

    evidence_ids = [str(value).strip() for value in references if str(value).strip()]
    result["evidence_ids"] = evidence_ids
    # Empty evidence can be valid for explicit absent/none states. The state
    # validators own that semantic decision; this check has no timestamp to
    # compare and therefore remains neutral.
    if not evidence_ids:
        return {
            **result,
            "status": "consistent",
            "reason_code": "predicate_not_met",
        }

    if parse_time_range_seconds(stage_range, None) is None:
        return {
            **result,
            "status": "incomplete",
            "reason_code": "hard_fact_missing",
            "missing_fields": ["stage_time_range"],
        }

    units_by_id = {
        str(unit.get("id") or "").strip(): unit
        for unit in units or []
        if isinstance(unit, dict) and str(unit.get("id") or "").strip()
    }
    missing_ids = [evidence_id for evidence_id in evidence_ids if evidence_id not in units_by_id]
    if missing_ids:
        return {
            **result,
            "status": "incomplete",
            "reason_code": "missing_field",
            "missing_evidence_ids": missing_ids,
        }

    invalid_time_ids = [
        evidence_id
        for evidence_id in evidence_ids
        if parse_time_range_seconds(units_by_id[evidence_id].get("time_range"), None) is None
    ]
    if invalid_time_ids:
        return {
            **result,
            "status": "incomplete",
            "reason_code": "hard_fact_missing",
            "invalid_time_range_evidence_ids": invalid_time_ids,
        }

    outside_ids = [
        evidence_id
        for evidence_id in evidence_ids
        if not evidence_overlaps_range(units_by_id[evidence_id], stage_range)
    ]
    if outside_ids:
        return {
            **result,
            "status": "state_conflict",
            "reason_code": "evidence_temporal_mismatch",
            "conflicting_evidence_ids": outside_ids,
            "evidence_time_ranges": {
                evidence_id: units_by_id[evidence_id].get("time_range")
                for evidence_id in outside_ids
            },
        }
    return {
        **result,
        "status": "consistent",
        "reason_code": "predicate_not_met",
    }


def validate_stage_evidence_temporal_consistency(result: dict[str, Any]) -> None:
    """Record the S1-S6 evidence-to-stage temporal contract without rewriting.

    Stage-level references, structured flag references, multimodal channel
    references, and S5 source references all point back to the same locked
    Stage1 evidence units. Only a reference with no positive time intersection
    becomes ``state_conflict``; missing/invalid inputs remain ``incomplete`` so
    the existing structural validators can report their own failure.
    """
    stages = result.get("stage_analysis")
    if not isinstance(stages, list):
        return
    understanding = result.get("video_understanding")
    understanding = understanding if isinstance(understanding, dict) else {}

    for index, stage in enumerate(stages, start=1):
        if not isinstance(stage, dict):
            continue
        checks: dict[str, dict[str, Any]] = {}
        flag_name = _STAGE_FLAG_NAMES.get(index)
        for role in ("creator", "benchmark"):
            role_understanding = understanding.get(role)
            role_understanding = role_understanding if isinstance(role_understanding, dict) else {}
            units = role_understanding.get("evidence_units")
            units = units if isinstance(units, list) else []
            stage_range = stage.get(f"{role}_time_range")

            stage_reference = f"{role}_evidence_ids"
            if stage_reference in stage:
                checks[stage_reference] = _check_evidence_reference_temporal_consistency(
                    units,
                    stage.get(stage_reference),
                    stage_range,
                    reference=stage_reference,
                )

            if flag_name:
                flag_key = f"{role}_{flag_name}"
                flag = stage.get(flag_key)
                if isinstance(flag, dict):
                    evidence_key = f"{flag_key}.evidence_ids"
                    if "evidence_ids" in flag:
                        checks[evidence_key] = _check_evidence_reference_temporal_consistency(
                            units,
                            flag.get("evidence_ids"),
                            stage_range,
                            reference=evidence_key,
                        )
                    if flag_name == "s5" and "trust_source_evidence_ids" in flag:
                        source_key = f"{flag_key}.trust_source_evidence_ids"
                        checks[source_key] = _check_evidence_reference_temporal_consistency(
                            units,
                            flag.get("trust_source_evidence_ids"),
                            stage_range,
                            reference=source_key,
                        )

            multimodal = stage.get(f"{role}_multimodal")
            channel_refs = multimodal.get("channel_evidence_ids") if isinstance(multimodal, dict) else None
            if isinstance(channel_refs, dict):
                for channel, references in channel_refs.items():
                    if not isinstance(references, list):
                        continue
                    channel_key = f"{role}_multimodal.channel_evidence_ids.{channel}"
                    checks[channel_key] = _check_evidence_reference_temporal_consistency(
                        units,
                        references,
                        stage_range,
                        reference=channel_key,
                    )

        has_temporal_conflict = any(
            isinstance(check, dict) and check.get("status") == "state_conflict"
            for check in checks.values()
        )
        # S3/S4 already carry read-only hard-fact markers consumed by derive;
        # keep their positive audit marker. For S1/S2/S5/S6 only persist a
        # marker when the new guard actually found a conflict, preserving the
        # existing clean-result shape for stages with no issue.
        if checks and (index in {3, 4} or has_temporal_conflict):
            postprocess_state = stage.setdefault("_postprocess_state", {})
            if isinstance(postprocess_state, dict):
                postprocess_state["evidence_temporal_checks"] = checks


def validate_s2_hard_fact_consistency(result: dict[str, Any]) -> None:
    """Record the read-only S2 contract marker consumed by its floor rule.

    S2 has no semantic state enum, so the marker only proves that every
    boolean used by ``S2_contract_floor`` was explicitly produced. It never
    changes a flag or assigns severity.
    """
    stages = result.get("stage_analysis")
    if not isinstance(stages, list) or len(stages) < 2 or not isinstance(stages[1], dict):
        return
    s2 = stages[1]
    required_fields = S2_HARD_FACT_FIELDS
    checks: dict[str, dict[str, Any]] = {}
    for role in ("creator", "benchmark"):
        flag = s2.get(f"{role}_s2")
        if not isinstance(flag, dict):
            checks[f"{role}_s2"] = {
                "status": "incomplete",
                "reason_code": "missing_field",
                "checked_fields": list(required_fields),
            }
            continue
        missing = [
            key
            for key in required_fields
            if flag.get(key) is not True and flag.get(key) is not False
        ]
        checks[f"{role}_s2"] = {
            "status": "incomplete" if missing else "consistent",
            "reason_code": "hard_fact_missing" if missing else "predicate_not_met",
            "missing_fields": missing,
            "checked_fields": list(required_fields),
        }
        if not missing:
            checks[f"{role}_s2"]["facts_sha256"] = hard_fact_fingerprint(
                s2_hard_fact_snapshot(flag), required_fields
            )
    postprocess_state = s2.setdefault("_postprocess_state", {})
    if isinstance(postprocess_state, dict):
        postprocess_state["s2_hard_fact_checks"] = checks


def validate_s3_s4_hard_fact_consistency(result: dict[str, Any]) -> None:
    """Record mechanical S3/S4 state checks without rewriting semantic facts.

    The marker is stored under ``_postprocess_state`` so it cannot be mistaken
    for an LLM fact. derive reads this marker as a precondition for a floor.
    """
    validate_stage_evidence_temporal_consistency(result)
    stages = result.get("stage_analysis")
    if not isinstance(stages, list) or len(stages) < 4:
        return
    s3, s4 = stages[2], stages[3]
    if not isinstance(s3, dict) or not isinstance(s4, dict):
        return

    s3_fields = S3_HARD_FACT_FIELDS
    s4_fields = S4_HARD_FACT_FIELDS
    s3_positive_fields = tuple(
        field
        for field in s3_fields
        if field not in {"result_only_without_process", "mouth_only_or_static", "fake_or_staged"}
    )
    s3_must_be_true_fields = tuple(field for field in s3_positive_fields)
    s4_positive_fields = (
        "effect_visible",
        "effect_proposition_matched",
        "visual_difference_observed",
        "module_constraints_met",
        "effect_attribution_supported",
        "process_linked_effect",
        "result_only_without_process",
    )
    state_checks: dict[str, dict[str, Any]] = {}
    for role in ("creator", "benchmark"):
        state_checks[f"{role}_s3"] = _check_evidence_state_consistency(
            s3.get(f"{role}_s3"),
            state_key="usage_evidence_state",
            allowed_states=S3_USAGE_EVIDENCE_STATES,
            state_complete="complete",
            state_none="none",
            required_fields=s3_fields,
            positive_fields=s3_positive_fields,
            must_be_true_fields=s3_must_be_true_fields,
            state_must_be_true={
                "partial": ("exists", "real_usage_met"),
            },
            state_must_be_false={
                "partial": ("result_only_without_process", "fake_or_staged"),
                "complete": (
                    "result_only_without_process",
                    "mouth_only_or_static",
                    "fake_or_staged",
                ),
            },
        )
        state_checks[f"{role}_s4"] = _check_evidence_state_consistency(
            s4.get(f"{role}_s4"),
            state_key="effect_evidence_state",
            allowed_states=S4_EFFECT_EVIDENCE_STATES,
            state_complete="verified",
            state_none="none",
            required_fields=s4_fields,
            positive_fields=s4_positive_fields,
            must_be_true_fields=(
                "effect_visible",
                "effect_proposition_matched",
                "visual_difference_observed",
                "module_constraints_met",
                "effect_attribution_supported",
                "process_linked_effect",
            ),
            state_must_be_true={
                "result_only": ("effect_visible", "result_only_without_process"),
            },
            state_must_be_false={
                "result_only": ("process_linked_effect",),
            },
            result_only="result_only",
        )
        for stage, suffix in ((s3, "s3"), (s4, "s4")):
            postprocess_state = stage.get("_postprocess_state")
            temporal_checks = postprocess_state.get("evidence_temporal_checks", {}) if isinstance(postprocess_state, dict) else {}
            temporal_conflicts = [
                check
                for key, check in temporal_checks.items()
                if key.startswith(f"{role}_")
                and isinstance(check, dict)
                and check.get("status") == "state_conflict"
            ]
            if temporal_conflicts:
                flag_key = f"{role}_{suffix}"
                state_checks[flag_key] = {
                    **state_checks[flag_key],
                    "status": "state_conflict",
                    "reason_code": "evidence_temporal_mismatch",
                    "temporal_conflicts": temporal_conflicts,
                }
    for stage in (s3, s4):
        postprocess_state = stage.setdefault("_postprocess_state", {})
        if isinstance(postprocess_state, dict):
            checks = {
                key: dict(value)
                for key, value in state_checks.items()
                if key.endswith("_s3") or key.endswith("_s4")
            }
            for key, check in checks.items():
                if check.get("status") == "incomplete":
                    continue
                flag = s3.get(key) if key.endswith("_s3") else s4.get(key)
                fields = s3_fields if key.endswith("_s3") else s4_fields
                if isinstance(flag, dict):
                    check["facts_sha256"] = hard_fact_fingerprint(flag, fields)
            postprocess_state["evidence_hard_fact_checks"] = checks


def reconcile_s3_s4_evidence_coherence(result: dict[str, Any]) -> None:
    """Backward-compatible name for the read-only hard-fact validator."""
    validate_s3_s4_hard_fact_consistency(result)


def bind_improvement_benchmark_reference(item: dict[str, Any], stage: dict[str, Any]) -> None:
    evidence_ids = [str(value) for value in stage.get("benchmark_evidence_ids", []) if str(value).strip()]
    item["benchmark_evidence_ids"] = evidence_ids
    time_range = str(stage.get("benchmark_time_range") or "").strip()
    stage_name = str(stage.get("stage") or "").strip()
    item["benchmark_time_range"] = time_range
    if "B_NO_USAGE" in evidence_ids:
        item["benchmark_reference"] = (
            f"标杆 {stage_name}（{time_range}）未识别到可独立验证的使用步骤画面；"
            "该建议来自达人缺口，不将反馈或成分片段误作使用演示。"
        )
    else:
        summary = str(stage.get("benchmark_summary") or stage.get("benchmark_key_message") or "").strip()
        item["benchmark_reference"] = f"标杆 {stage_name}（{time_range}）：{summary}"
    creator_range = str(item.get("creator_time_range") or item.get("time_range") or "").strip()
    references = ", ".join(evidence_ids) or "无独立证据"
    item["evidence"] = [
        f"标杆依据：{references}，对应 {stage_name} {time_range}。",
        f"达人修改位置：{creator_range}，改造目标为补足该阶段信息传递。",
    ]


def bind_improvement_base_material(item: dict[str, Any], creator_units: list[Any]) -> None:
    if item.get("base_frame_suitability") == "no_suitable_frame":
        item["base_frame_evidence_id"] = ""
        return
    chosen = evidence_unit_at_time(creator_units, item.get("best_base_frame_time"))
    prompt = str(item.get("suggestion") or "")
    needs_product = bool(re.search(r"产品|包装|瓶|product|label|bungkusan|troli", prompt, flags=re.IGNORECASE))
    if needs_product and not evidence_mentions_product(chosen):
        chosen = nearest_product_evidence_unit(creator_units, item.get("best_base_frame_time"))
    if not isinstance(chosen, dict):
        item["base_frame_suitability"] = "no_suitable_frame"
        item["best_base_frame_time"] = ""
        item["base_frame_evidence_id"] = ""
        item["base_frame_reason"] = "达人现有素材中未找到可验证的改造基底，需补拍或补充素材。"
        return
    item["base_frame_evidence_id"] = str(chosen.get("id") or "").strip()
    if needs_product and parse_timestamp_seconds(item.get("best_base_frame_time")) is not None and not evidence_mentions_product(
        evidence_unit_at_time(creator_units, item.get("best_base_frame_time"))
    ):
        parsed = parse_time_range_seconds(chosen.get("time_range"), None)
        if parsed is not None:
            item["best_base_frame_time"] = format_seconds(parsed[0])
    visible_fact = str(chosen.get("visual_fact") or chosen.get("information") or "").strip()
    item["base_frame_reason"] = f"来自达人 {item['base_frame_evidence_id']} 的真实素材：{visible_fact}"

# endregion


# region reconcile -----------------------------------------------------------

def reconcile_unsupported_cta(result: dict[str, Any]) -> None:
    """S6 没有可识别的购买指令时写占位；字幕/画面驱动 CTA 不得被空 quote 覆盖。"""
    stages = result.get("stage_analysis", [])
    if len(stages) < 6:
        return
    cta = stages[5]
    for role, code in (("benchmark", "B"), ("creator", "C")):
        quote = str(cta.get(f"{role}_quote") or "")
        flag = cta.get(f"{role}_s6") if isinstance(cta.get(f"{role}_s6"), dict) else {}
        has_direct_or_path = (
            flag.get("direct_order_met") is True
            or flag.get("action_path_clear") is True
        )
        has_valid_soft_invitation = (
            flag.get("soft_purchase_invitation_met") is True
            and flag.get("offer_or_incentive_clear") is True
        )
        if flag.get("exists") is True and not has_direct_or_path and not has_valid_soft_invitation:
            # S6 的“软促单”必须同时有面向观众的购买邀请和明确利益点。
            # 单纯总结效果、展示产品或暗示值得购买不是 CTA，不能留给 derive 当作有效促单。
            flag["exists"] = False
            flag["soft_purchase_invitation_met"] = False
            flag["module_fit_met"] = False
            flag["ending_position_met"] = False
            flag["evidence_ids"] = []
            previous_reason = str(flag.get("cta_reason") or "").strip()
            reason = "未见明确下单/路径，且不满足“购买邀请+利益点”的软促单定义，按无 CTA 处理。"
            flag["cta_reason"] = f"{previous_reason} {reason}".strip()
        units = result.get("video_understanding", {}).get(role, {}).get("evidence_units", [])
        flag_ids = [str(value) for value in flag.get("evidence_ids", []) if str(value).strip()]
        referenced = [
            unit for unit in units
            if isinstance(unit, dict) and str(unit.get("id")) in flag_ids and "_NO_CTA" not in str(unit.get("id"))
        ]
        referenced_text = json.dumps(referenced, ensure_ascii=False)
        has_positive_flag = (
            flag.get("exists") is True
            and (
                has_direct_or_path
                or has_valid_soft_invitation
            )
            and bool(referenced)
        )
        has_positive_text = bool(
            re.search(
                r"\b(beli|troli|klik|cart|checkout|order|link|shop)\b|购买|下单|购物车|点击|购买号召|购买指令",
                quote + " " + referenced_text,
                flags=re.IGNORECASE,
            )
        )
        if has_positive_flag or has_positive_text:
            # 旧结果可能已被本函数写过 _NO_CTA；一旦结构化 flag + 原始事实证明 CTA 存在，
            # 恢复真实引用并移除代码生成的过期占位，避免后续建议继续引用假缺失。
            if referenced:
                cta[f"{role}_evidence_ids"] = flag_ids
                cta[f"{role}_key_message"] = str(referenced[0].get("information") or "").strip()
                cta[f"{role}_summary"] = cta[f"{role}_key_message"]
                cta[f"{role}_quote"] = str(referenced[0].get("voiceover") or "").strip()
                cta[f"{role}_quote_zh"] = str(referenced[0].get("voiceover_zh") or "").strip()
                cta[f"{role}_support_status"] = "voice_only" if cta[f"{role}_quote"] else "visual_only"
            units[:] = [unit for unit in units if str(unit.get("id") or "") != f"{code}_NO_CTA"]
            continue
        unit_id = f"{code}_NO_CTA"
        placeholder = {
            "id": unit_id,
            "time_range": str(cta.get(f"{role}_time_range") or ""),
            "information": "结尾未识别到明确的购买或点击指令。",
            "voiceover": "",
            "voiceover_zh": "",
            "visual_fact": "结尾仅可见产品或人物表现，未见可验证的购物车或下单提示。",
            "subtitle_fact": "",
        }
        ensure_evidence_unit(units, placeholder)
        cta[f"{role}_evidence_ids"] = [unit_id]
        cta[f"{role}_key_message"] = placeholder["information"]
        cta[f"{role}_summary"] = placeholder["information"]
        cta[f"{role}_quote"] = ""
        cta[f"{role}_quote_zh"] = ""
        cta[f"{role}_visual_evidence"] = [placeholder["visual_fact"]]
        cta[f"{role}_support_status"] = "visual_only"

    def has_valid_cta(role: str) -> bool:
        flag = cta.get(f"{role}_s6")
        if not isinstance(flag, dict) or flag.get("exists") is not True:
            return False
        return (
            flag.get("direct_order_met") is True
            or flag.get("action_path_clear") is True
            or (
                flag.get("soft_purchase_invitation_met") is True
                and flag.get("offer_or_incentive_clear") is True
            )
        )

    benchmark_has_cta = has_valid_cta("benchmark")
    creator_has_cta = has_valid_cta("creator")
    if not benchmark_has_cta and not creator_has_cta:
        conclusion = "双方结尾均未出现明确购买指令、购买路径，或“购买邀请+利益点”的软促单组合；S6 不构成相对差距，但两者均未完成促单。"
        cta["gap"] = conclusion
        cta["gap_summary"] = [conclusion]
        cta["evidence"] = ["双方的结尾均未引用到可验证的购买或点击指令。"]
    elif creator_has_cta and not benchmark_has_cta:
        conclusion = "达人结尾提供了有效 CTA，标杆未形成有效 CTA；S6 不构成达人的相对差距。"
        cta["gap"] = conclusion
        cta["gap_summary"] = [conclusion]
    elif benchmark_has_cta and not creator_has_cta:
        conclusion = "标杆结尾提供了有效 CTA，达人未形成有效 CTA；达人缺少促进下单的明确收口。"
        cta["gap"] = conclusion
        cta["gap_summary"] = [conclusion]


_S5_VALID_BASES = {
    "authority",
    "traceable_data",
    "independent_user",
    "social_consensus",
    "process_transparency",
}


def _valid_s5_source_ids(flag: dict[str, Any], units: list[Any]) -> list[str]:
    """只接受当前 S5 已引用的、带同类型来源信号的事实单元。"""
    basis = str(flag.get("trust_basis") or "")
    if basis not in _S5_VALID_BASES:
        return []
    source_ids = [str(value).strip() for value in flag.get("trust_source_evidence_ids", []) if str(value).strip()]
    evidence_ids = [str(value).strip() for value in flag.get("evidence_ids", []) if str(value).strip()]
    # 修复模型漏填 source_ids 的情况，但绝不从未被 S5 引用的其它时间段“借”背书。
    candidates = list(dict.fromkeys([*source_ids, *evidence_ids]))
    valid: list[str] = []
    for unit in units:
        if not isinstance(unit, dict):
            continue
        unit_id = str(unit.get("id") or "").strip()
        if unit_id not in candidates:
            continue
        signals = {str(item).strip() for item in unit.get("trust_source_signals", []) if str(item).strip()}
        reference = str(unit.get("trust_source_reference") or "").strip()
        if basis in signals and reference:
            valid.append(unit_id)
    return valid


def _s5_source_status(flag: dict[str, Any], units: list[Any]) -> tuple[str, list[str]]:
    """返回当前 S5 引用来源的状态，不把缺失或不完整证据改写成 absent。"""
    candidates = list(dict.fromkeys([
        *[str(value).strip() for value in flag.get("trust_source_evidence_ids", []) if str(value).strip()],
        *[str(value).strip() for value in flag.get("evidence_ids", []) if str(value).strip()],
    ]))
    if not candidates:
        return "missing", []
    unit_map = {
        str(unit.get("id") or "").strip(): unit
        for unit in units
        if isinstance(unit, dict) and str(unit.get("id") or "").strip()
    }
    if any(candidate not in unit_map for candidate in candidates):
        return "missing", []
    statuses = []
    for candidate in candidates:
        raw_status = unit_map[candidate].get("trust_source_status")
        status = str(raw_status).strip() if raw_status is not None else normalize_s5_source_status(unit_map[candidate])
        statuses.append(status if status in S5_SOURCE_STATUSES else "uncertain")
    if any(status == "uncertain" for status in statuses):
        return "uncertain", []
    if any(status == "missing" for status in statuses):
        return "missing", []
    valid_ids = _valid_s5_source_ids(flag, units)
    if valid_ids:
        return "explicit_present", valid_ids
    # Stage1 明确看到了来源，但它与当前 S5 basis 无法对齐时是冲突/不确定，
    # 不能降格成 explicit_absent，否则会错误触发 S5 ceiling。
    if any(status == "explicit_present" for status in statuses):
        return "uncertain", []
    return "explicit_absent", []


def _s5_flag_explicit_absence(flag: dict[str, Any], source_status: str) -> bool:
    """只有 Stage2 明确声明无独立信任材料时，才允许 S5 ceiling 消费 absence。"""
    candidates = [
        str(value).strip()
        for value in [
            *(flag.get("trust_source_evidence_ids") or []),
            *(flag.get("evidence_ids") or []),
        ]
        if str(value).strip()
    ]
    return (
        (
            source_status == "explicit_absent"
            or (source_status == "missing" and not candidates)
        )
        and
        flag.get("exists") is False
        and flag.get("independent_trust_purpose") is False
        and flag.get("duplicates_other_stage") is False
        and str(flag.get("trust_basis") or "") == "none"
        and str(flag.get("trust_evidence_type") or "") in {"none", "unknown"}
    )


def _s5_flag_product_claim_or_offer(flag: dict[str, Any]) -> bool:
    """Keep product claims and offers distinct from a confirmed lack of trust."""
    return (
        flag.get("exists") is False
        and flag.get("independent_trust_purpose") is False
        and flag.get("duplicates_other_stage") is False
        and str(flag.get("trust_basis") or "") in {"product_claim", "offer_or_spec"}
    )


def _set_s5_absent_stage_side(stage: dict[str, Any], role: str) -> None:
    label = "标杆" if role == "benchmark" else "达人"
    stage[f"{role}_summary"] = f"{label}未提供可核验的独立信任材料。"
    stage[f"{role}_key_message"] = stage[f"{role}_summary"]
    stage[f"{role}_quote"] = ""
    stage[f"{role}_quote_zh"] = ""
    stage[f"{role}_support_status"] = "visual_only"
    stage[f"{role}_visual_evidence"] = ["未引用到可核验的独立信任来源。"]


def _s5_improvement_targets_benchmark(item: dict[str, Any]) -> bool:
    target = str(item.get("target_stage") or "").upper()
    if re.search(r"\bS5\b", target):
        return True
    return not target and bool(re.search(r"信任|背书|认证|口碑|证言", str(item.get("title") or "")))


def reconcile_s5_trust_sources(result: dict[str, Any], source_signals_required: bool) -> None:
    """以 Stage1 锁定来源校验 S5，但保留 missing/uncertain，不伪造明确 absence。"""
    if not source_signals_required:
        return
    stages = result.get("stage_analysis", [])
    if len(stages) < 5 or not isinstance(stages[4], dict):
        return
    stage = stages[4]
    understanding = result.get("video_understanding", {})
    valid_roles: dict[str, bool | None] = {}
    reconciled: list[dict[str, str]] = []
    for role in ("creator", "benchmark"):
        key = f"{role}_s5"
        flag = stage.get(key)
        if not isinstance(flag, dict):
            continue
        units = understanding.get(role, {}).get("evidence_units", [])
        units = units if isinstance(units, list) else []
        source_status, source_ids = _s5_source_status(flag, units)
        basis = str(flag.get("trust_basis") or "unknown")
        has_valid_source = (
            flag.get("exists") is True
            and flag.get("independent_trust_purpose") is True
            and flag.get("duplicates_other_stage") is not True
            and flag.get("trust_claim_specific") is True
            and flag.get("product_relevance_met") is True
            and basis in _S5_VALID_BASES
            and source_status == "explicit_present"
            and bool(source_ids)
        )
        if has_valid_source:
            flag["trust_source_evidence_ids"] = source_ids
            flag["_s5_source_status"] = "explicit_present"
            valid_roles[role] = True
            continue
        if _s5_flag_explicit_absence(flag, source_status):
            flag["_s5_source_status"] = "explicit_absent"
            valid_roles[role] = False
            continue
        if _s5_flag_product_claim_or_offer(flag):
            flag["_s5_source_status"] = "product_claim_or_offer"
            valid_roles[role] = None
            reconciled.append({
                "role": role,
                "basis": basis,
                "status": "product_claim_or_offer",
                "reason": "产品主张或优惠不是独立信任来源，也不等同于明确 absence。",
            })
            continue
        # 来源缺失、不完整或与 flag 声明不一致时保留原 flag，让后续质量门禁触发
        # repair；不能把“无法证明存在”降格成“明确不存在”并触发 ceiling。
        flag["_s5_source_status"] = "unknown"
        valid_roles[role] = None
        reason = "S5 来源事实缺失或不完整，保留 unknown，不能按未涉及处理。"
        reconciled.append({"role": role, "basis": basis, "status": source_status, "reason": reason})

    benchmark_valid = valid_roles.get("benchmark") is True
    creator_valid = valid_roles.get("creator") is True
    roles_known = valid_roles.get("benchmark") is not None and valid_roles.get("creator") is not None
    if roles_known and not benchmark_valid and not creator_valid:
        stage["gap"] = "双方均未提供可核验的独立信任材料，S5 不构成独立差距。"
        stage["gap_summary"] = [stage["gap"]]
    elif roles_known and benchmark_valid and not creator_valid:
        stage["gap"] = "标杆提供了可核验的独立信任材料，达人未提供对应来源。"
        stage["gap_summary"] = [stage["gap"]]
    elif roles_known and creator_valid and not benchmark_valid:
        stage["gap"] = "达人提供了可核验的独立信任材料，标杆未提供；S5 不构成达人差距。"
        stage["gap_summary"] = [stage["gap"]]

    if roles_known and not benchmark_valid:
        result["improvements"] = [
            item
            for item in result.get("improvements", [])
            if not isinstance(item, dict) or not _s5_improvement_targets_benchmark(item)
        ]
    if reconciled:
        warnings = result.get("qa_warnings") if isinstance(result.get("qa_warnings"), list) else []
        warnings.extend(
            f"S5 来源校验：{item['role']}侧 {item['basis']} 状态为 {item['status']}，保留 unknown，等待 repair。"
            for item in reconciled
        )
        result["qa_warnings"] = warnings
        result["s5_source_reconciliation"] = reconciled

# endregion


# region ground --------------------------------------------------------------

def ground_stage_visual_evidence(result: dict[str, Any]) -> None:
    """把每个 stage 的 visual_evidence 与所引用 evidence_unit 的 visual/subtitle_fact 对齐。"""
    understanding = result.get("video_understanding", {})
    for stage in result.get("stage_analysis", []):
        for role in ("benchmark", "creator"):
            references = {str(value) for value in stage.get(f"{role}_evidence_ids", [])}
            units = understanding.get(role, {}).get("evidence_units", [])
            facts: list[str] = []
            for unit in units:
                if not isinstance(unit, dict) or str(unit.get("id")) not in references:
                    continue
                for key in ("visual_fact", "subtitle_fact"):
                    fact = str(unit.get(key) or "").strip()
                    if fact:
                        facts.append(fact)
            cautions = [
                str(value)
                for value in stage.get(f"{role}_visual_evidence", [])
                if re.search(r"未核验|未验证|待复核", str(value))
            ]
            stage[f"{role}_visual_evidence"] = list(dict.fromkeys([*facts, *cautions]))[:5]


def ground_improvement_evidence(result: dict[str, Any]) -> None:
    """把每个提升点的标杆引用 + 达人基底帧引用，绑回 stage 和 evidence_units。"""
    stages = result.get("stage_analysis", [])
    creator_units = result.get("video_understanding", {}).get("creator", {}).get("evidence_units", [])
    if not isinstance(stages, list) or not isinstance(creator_units, list):
        return
    for item in result.get("improvements", []):
        if not isinstance(item, dict):
            continue
        stage = improvement_reference_stage(item, stages)
        if stage:
            bind_improvement_benchmark_reference(item, stage)
        bind_improvement_base_material(item, creator_units)


def improvement_reference_stage(item: dict[str, Any], stages: list[Any]) -> dict[str, Any] | None:
    target_match = re.search(r"\b(S[1-6])\b", str(item.get("target_stage") or ""), flags=re.IGNORECASE)
    target_code = target_match.group(1).upper() if target_match else ""
    if target_code:
        target_index = int(target_code[1]) - 1
        if 0 <= target_index < len(stages) and isinstance(stages[target_index], dict):
            return stages[target_index]

    title = str(item.get("title") or "").lower()
    text = " ".join(str(item.get(key) or "") for key in ("title", "problem", "suggestion")).lower()
    keyword_stages = (
        (5, ("cta", "下单", "购买", "购物车")),
        (0, ("hook", "开头", "钩子")),
        (1, ("产品引出", "引出")),
        (2, ("使用", "步骤", "演示")),
        (3, ("效果", "反馈", "结果")),
        (4, ("信任", "认证", "成分")),
    )
    for index, keywords in keyword_stages:
        if any(keyword in title for keyword in keywords) and index < len(stages):
            return stages[index] if isinstance(stages[index], dict) else None
    for index, keywords in keyword_stages:
        if any(keyword in text for keyword in keywords) and index < len(stages):
            return stages[index] if isinstance(stages[index], dict) else None
    return None

# endregion


# region fill ----------------------------------------------------------------

def _stage_reference_ids(stage: dict[str, Any], role: str) -> set[str]:
    return {
        str(value).strip()
        for value in stage.get(f"{role}_evidence_ids") or []
        if str(value).strip()
    }


def _stage_review_range_from_facts(
    result: dict[str, Any],
    role: str,
    stage_index: int,
    stage: dict[str, Any],
) -> str:
    """Derive a bounded review window from already locked neighboring facts."""
    range_key = f"{role}_time_range"
    if parse_time_range_seconds(stage.get(range_key), None) is not None:
        return str(stage.get(range_key) or "")

    role_result = result.get("video_understanding", {}).get(role, {})
    units = role_result.get("evidence_units", []) if isinstance(role_result, dict) else []
    units_by_id = {
        str(unit.get("id")): unit
        for unit in units
        if isinstance(unit, dict) and str(unit.get("id") or "").strip()
    }

    def timed_units(ids: set[str]) -> list[dict[str, Any]]:
        return [
            units_by_id[item]
            for item in ids
            if item in units_by_id and parse_time_range_seconds(units_by_id[item].get("time_range"), None) is not None
        ]

    current_units = timed_units(_stage_reference_ids(stage, role))
    if current_units:
        ranges = [parse_time_range_seconds(unit.get("time_range"), None) for unit in current_units]
        start = min(item[0] for item in ranges if item is not None)
        end = max(item[1] for item in ranges if item is not None)
        return f"{format_seconds(start)} - {format_seconds(max(end, start + 0.5))}"

    stages = result.get("stage_analysis") or []
    before: dict[str, Any] | None = None
    before_end: float | None = None
    for previous_stage in reversed(stages[:stage_index]):
        previous_ids = _stage_reference_ids(previous_stage, role)
        candidates = timed_units(previous_ids)
        if candidates:
            before = max(
                candidates,
                key=lambda unit: parse_time_range_seconds(unit.get("time_range"), None)[1],
            )
            before_end = parse_time_range_seconds(before.get("time_range"), None)[1]
            break

    after: dict[str, Any] | None = None
    future_candidates: list[dict[str, Any]] = []
    seen_before = {str(before.get("id"))} if before else set()
    for future_stage in stages[stage_index + 1 :]:
        future_ids = _stage_reference_ids(future_stage, role)
        future_candidates.extend(unit for unit in timed_units(future_ids) if str(unit.get("id")) not in seen_before)
    if before_end is not None:
        after = min(
            (
                unit
                for unit in future_candidates
                if parse_time_range_seconds(unit.get("time_range"), None)[0] >= before_end
            ),
            key=lambda unit: parse_time_range_seconds(unit.get("time_range"), None)[0],
            default=None,
        )
    if after is None and future_candidates:
        after = min(
            future_candidates,
            key=lambda unit: parse_time_range_seconds(unit.get("time_range"), None)[0],
        )
    if before is not None and after is not None:
        review_range = adjacent_review_range(before, after, "")
        if review_range:
            return review_range
    if before is not None:
        _, end = parse_time_range_seconds(before.get("time_range"), None)
        return f"{format_seconds(end)} - {format_seconds(end + 0.5)}"
    if after is not None:
        start, _ = parse_time_range_seconds(after.get("time_range"), None)
        end = start if start > 0 else 0.5
        return f"{format_seconds(max(0.0, start - 0.5))} - {format_seconds(end)}"

    return ""

def fill_missing_evidence_references(result: dict[str, Any]) -> None:
    """阶段引用的 evidence_unit 不在该阶段时间内时，补占位单元或就近匹配。

    TODO 原归属"证据校验"章节，但实际是 fill 行为不抛 SystemExit，按用户拆分约束 #7 归 repair。
    """
    understanding = result.get("video_understanding", {})
    for index, stage in enumerate(result.get("stage_analysis", []), start=1):
        for role, code in (("benchmark", "B"), ("creator", "C")):
            key = f"{role}_evidence_ids"
            units = understanding.get(role, {}).get("evidence_units", [])
            if parse_time_range_seconds(stage.get(f"{role}_time_range"), None) is None:
                review_range = _stage_review_range_from_facts(result, role, index - 1, stage)
                if review_range:
                    stage[f"{role}_time_range"] = review_range
            references = {str(item) for item in stage.get(key, [])}
            overlapping = [
                str(unit.get("id"))
                for unit in units
                if isinstance(unit, dict)
                and str(unit.get("id")) in references
                and evidence_overlaps_range(unit, stage.get(f"{role}_time_range"))
            ]
            if overlapping:
                stage[key] = overlapping
                continue
            if not is_effective_voiceover(stage.get(f"{role}_quote")):
                unit_id = f"{code}_NO_STAGE_{index}"
                placeholder = {
                    "id": unit_id,
                    "time_range": str(stage.get(f"{role}_time_range") or ""),
                    "information": f"该视频在 {stage.get('stage', f'S{index}')} 未识别到可独立归因的信息。",
                    "voiceover": "",
                    "voiceover_zh": "",
                    "visual_fact": "当前证据不足以单独支持该阶段结论，需人工复核原视频。",
                    "subtitle_fact": "",
                }
                ensure_evidence_unit(units, placeholder)
                stage[key] = [unit_id]
                stage[f"{role}_support_status"] = "visual_only"
                stage[f"{role}_visual_evidence"] = [placeholder["visual_fact"]]
                continue
            is_silent_role = not any(is_effective_voiceover(unit.get("voiceover")) for unit in units if isinstance(unit, dict))
            if not is_silent_role:
                continue
            best = nearest_evidence_unit(units, stage.get(f"{role}_time_range"))
            if best and best.get("id"):
                stage[key] = [str(best["id"])]

# endregion


# region materialize ---------------------------------------------------------

def materialize_spoken_stage_evidence(result: dict[str, Any]) -> None:
    """阶段有有效口播但没有对应时间内 evidence_unit 时，造一个 stage 占位单元。"""
    understanding = result.get("video_understanding", {})
    for index, stage in enumerate(result.get("stage_analysis", []), start=1):
        for role, code in (("benchmark", "B"), ("creator", "C")):
            quote = str(stage.get(f"{role}_quote") or "").strip()
            if not is_effective_voiceover(quote):
                continue
            units = understanding.get(role, {}).get("evidence_units", [])
            if parse_time_range_seconds(stage.get(f"{role}_time_range"), None) is None:
                review_range = _stage_review_range_from_facts(result, role, index - 1, stage)
                if review_range:
                    stage[f"{role}_time_range"] = review_range
            references = {str(item) for item in stage.get(f"{role}_evidence_ids", [])}
            if any(
                str(unit.get("id")) in references
                and evidence_overlaps_range(unit, stage.get(f"{role}_time_range"))
                for unit in units
                if isinstance(unit, dict)
            ):
                continue
            unit_id = f"{code}_STAGE_{index}"
            visual_fact = "口播传递上述信息；下方关键帧来自同一阶段，静态画面未独立验证该口播主张。"
            units[:] = [unit for unit in units if str(unit.get("id")) != unit_id]
            units.append(
                {
                    "id": unit_id,
                    "time_range": str(stage.get(f"{role}_time_range") or ""),
                    "information": str(stage.get(f"{role}_key_message") or ""),
                    "voiceover": quote,
                    "voiceover_zh": str(stage.get(f"{role}_quote_zh") or ""),
                    "visual_fact": visual_fact,
                    "subtitle_fact": "",
                }
            )
            stage[f"{role}_evidence_ids"] = [unit_id]
            stage[f"{role}_visual_evidence"] = [visual_fact]
            stage[f"{role}_support_status"] = "voice_only"

# endregion


# region deduplicate ---------------------------------------------------------

def deduplicate_stage_quotes(result: dict[str, Any]) -> None:
    """跨阶段去重 quote 子句，避免同一句被多个阶段引用。"""
    stages = result.get("stage_analysis", [])
    for role in ("benchmark", "creator"):
        used: set[str] = set()
        for stage in stages:
            key = f"{role}_quote"
            quote = str(stage.get(key) or "").strip()
            if not quote:
                continue
            clauses = split_quote_clauses(quote)
            retained = [clause for clause in clauses if normalized_quote_clause(clause) not in used]
            used.update(normalized_quote_clause(clause) for clause in retained)
            if len(retained) == len(clauses):
                continue
            stage[key] = " ".join(retained).strip()
            stage[f"{role}_quote_zh"] = ""
            if not retained:
                stage[f"{role}_support_status"] = "visual_only"
                if not stage.get(f"{role}_visual_evidence"):
                    stage[f"{role}_visual_evidence"] = ["该阶段没有独立口播证据，仅依据对应时间段画面阅读。"]


def split_quote_clauses(text: str) -> list[str]:
    return [
        clause.strip()
        for clause in re.split(r"(?<=[。；;.!?])\s*", text)
        if clause.strip()
    ]


def normalized_quote_clause(text: str) -> str:
    return re.sub(r"\W+", "", text, flags=re.UNICODE).lower()

# endregion
