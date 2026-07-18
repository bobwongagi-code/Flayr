"""flayr_core.postprocess.validate：通用校验层。

⚠️ 本模块所有 validate_* 函数在校验失败时会抛 SystemExit，
   触发 pipeline 走 repair payload 重跑 LLM。调用方必须感知这个控制流副作用，
   不要把这些函数和 repair.py 里的"纯数据修补"混用。

健康品类专项的 validate_recommendation_safety / validate_creator_script_language
与 sanitize_* 共用同一组健康关键词，因此保留在 health_rewrite.py。
认证阶段归属则统一从 stage_ownership.py 导入，本模块不再维护市场特例。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..artifacts import parse_time_range_seconds
from ..llm.parse import (
    hook_reason_window_leaks,
    is_effective_voiceover,
    normalized_transcript_text,
    read_transcript_text,
)
from ..multimodal import (
    MULTIMODAL_CHANNELS,
    MULTIMODAL_DOMINANT_CHANNELS,
    MULTIMODAL_EFFECTS,
    MULTIMODAL_IMPACTS,
    MULTIMODAL_RELATIONS,
)
from ..stage_ownership import CERTIFICATION_OWNER_STAGE, contains_certification, is_certification_owner_stage
from ..structure_modules import official_module_ids
from .utils import evidence_overlaps_range


def _role_claim_payload(stage: dict[str, Any], role: str) -> dict[str, Any]:
    """提取某侧真正的阶段主张文本，排除只描述阶段边界的元信息。"""
    payload: dict[str, Any] = {}
    for key, value in stage.items():
        if not key.startswith(role):
            continue
        if key.endswith("_hook") and isinstance(value, dict):
            hook = {
                nested_key: nested_value
                for nested_key, nested_value in value.items()
                if nested_key not in {"hook_boundary_reason", "s2_start_signal"}
            }
            payload[key] = hook
            continue
        payload[key] = value
    return payload


def _role_claim_references(stage: dict[str, Any], role: str) -> list[str]:
    """收集某侧阶段主引用和结构化 flag 自带引用，用于校验证据支撑。"""
    references: list[str] = []

    def append_many(items: Any) -> None:
        for item in items if isinstance(items, list) else []:
            ref = str(item).strip()
            if ref and ref not in references:
                references.append(ref)

    append_many(stage.get(f"{role}_evidence_ids"))
    for key, value in stage.items():
        if key.startswith(role) and isinstance(value, dict):
            append_many(value.get("evidence_ids"))
    return references


def validate_evidence_alignment(result: dict[str, Any]) -> None:
    """阶段顺序 + evidence_ids 引用合法性 + 认证主张需证据支撑。"""
    understanding = result.get("video_understanding", {})
    available: dict[str, set[str]] = {}
    for role in ("benchmark", "creator"):
        units = understanding.get(role, {}).get("evidence_units", [])
        available[role] = {str(unit.get("id")) for unit in units if isinstance(unit, dict) and unit.get("id")}
        if not available[role]:
            raise SystemExit(f"video_understanding.{role}.evidence_units 不能为空，必须先完成整片事实清单。")
    for index, stage in enumerate(result.get("stage_analysis", []), start=1):
        expected_stage = f"S{index}"
        if not str(stage.get("stage") or "").strip().startswith(expected_stage):
            raise SystemExit(f"stage_analysis 必须按 S1 到 S6 顺序输出；第 {index} 项不是 {expected_stage}。")
        for role in ("benchmark", "creator"):
            references = stage.get(f"{role}_evidence_ids", [])
            if not references:
                raise SystemExit(f"S{index} 缺少 {role}_evidence_ids，结论无法对应证据。")
            missing = [item for item in references if item not in available[role]]
            if missing:
                raise SystemExit(f"S{index} 引用了不存在的 {role} 证据：{', '.join(missing)}。")
            units = understanding.get(role, {}).get("evidence_units", [])
            is_silent_role = not any(is_effective_voiceover(unit.get("voiceover")) for unit in units if isinstance(unit, dict))
            referenced = [unit for unit in units if str(unit.get("id")) in references]
            if not is_silent_role and not any(
                evidence_overlaps_range(unit, stage.get(f"{role}_time_range")) for unit in referenced
            ):
                raise SystemExit(f"S{index} 的 {role} 证据不在对应阶段时间内，需补充该时段事实单元。")
            stage_text = json.dumps(
                _role_claim_payload(stage, role),
                ensure_ascii=False,
            )
            if contains_certification(stage_text):
                claim_references = set(_role_claim_references(stage, role))
                claim_referenced = [unit for unit in units if str(unit.get("id")) in claim_references]
                source_text = json.dumps(claim_referenced or referenced, ensure_ascii=False)
                if not contains_certification(source_text):
                    raise SystemExit(f"S{index} 的 {role} 认证结论没有被所引用事实单元支持。")


def validate_analysis_dimensions(result: dict[str, Any]) -> None:
    """三步分析契约校验：收集 warnings 到 result['qa_warnings']，不再抛 SystemExit。

    ⚠️ 行为变化（保通流程的短期妥协）：原本会抛 SystemExit 触发 repair，
    现在改为软警告。这样即使 LLM 漏字段也能让报告出结果，由报告读者人工识别。
    后续 QA-RULES 实施时可由 R01/R02 接管严格性，届时可恢复硬校验。
    """
    warnings: list[str] = []

    if not str(result.get("one_line_verdict") or "").strip():
        warnings.append("[Q03] 缺少第一步整体感知的 one_line_verdict。")
    holistic = result.get("holistic_assessment", {})
    if any(value == "未完成评估。" for value in holistic.values()):
        warnings.append("[Q03] 缺少第一步整体感知的五维速评或整体转化预判。")
    visibility = result.get("product_visibility", {})
    if visibility.get("first_appearance_sec") is None or visibility.get("ratio") is None:
        warnings.append("[Q09] 缺少第二步产品可见度统计。")
    computed_loop = result.get("computed_loop_closure")
    if not isinstance(computed_loop, dict) or computed_loop.get("source") != "proposition_trace":
        warnings.append("[Q03] 缺少第二步槽位间闭环校验。")
    for stage in result.get("stage_analysis", []):
        if not stage.get("gap_summary") or not str(stage.get("module_fit_reason") or "").strip():
            warnings.append(f"[Q03] {stage.get('stage')} 缺少模块适配判断或分点差距。")

    improvement_targets = {
        match.group(1).upper()
        for item in result.get("improvements", [])
        if isinstance(item, dict)
        for match in [re.search(r"\b(S[1-6])\b", str(item.get("target_stage") or ""), flags=re.IGNORECASE)]
        if match
    }
    for stage in result.get("stage_analysis", []):
        match = re.match(r"(S[1-6])", str(stage.get("stage") or ""), flags=re.IGNORECASE)
        if match and str(stage.get("severity") or "") == "large" and match.group(1).upper() not in improvement_targets:
            warnings.append(f"[Q13] {match.group(1).upper()} 最终为 large，但 Top 提升点未覆盖该阶段。")

    if warnings:
        existing = result.get("qa_warnings", [])
        if not isinstance(existing, list):
            existing = []
        result["qa_warnings"] = existing + warnings


def validate_required_stage_narratives(result: dict[str, Any]) -> None:
    """报告核心的双侧表现与差距结论不得以解析占位符蒙混通过。"""
    missing: list[str] = []
    for stage in result.get("stage_analysis", []):
        if not isinstance(stage, dict):
            continue
        label = str(stage.get("stage") or "未知阶段")
        for field in ("benchmark_summary", "creator_summary", "gap"):
            value = str(stage.get(field) or "").strip()
            if not value or "LLM 未填写" in value:
                missing.append(f"{label}.{field}")
    if missing:
        raise SystemExit("阶段报告核心字段缺失，需 repair：" + ", ".join(missing))


def validate_quality_contract(result: dict[str, Any], analysis: dict[str, Any]) -> None:
    """执行 QA-RULES.md 里已落地的通用质量契约。

    本函数是 QA-RULES 从"文档"进入主流程的收口点：
      - 可确定为错误的规则抛 SystemExit，触发 repair；
      - 历史结果中常见、且已有下游兜底的弱问题写入 qa_warnings。
    """
    validate_required_stage_narratives(result)
    validate_module_ids(result)
    validate_s1_hook_flags(result, analysis)
    validate_s2_contract_flags(result, analysis)
    validate_s3_usage_flags(result, analysis)
    validate_s4_effect_flags(result, analysis)
    validate_s5_trust_flags(result, analysis)
    validate_s6_cta_flags(result, analysis)
    validate_multimodal_assessments(result, analysis)
    validate_chain_relationships(result, analysis)
    validate_stage_time_coherence(result)
    validate_product_visibility(result, analysis)
    validate_narrative_evidence_consistency(result)
    validate_global_gate_observations(result)


def validate_global_gate_observations(result: dict[str, Any]) -> None:
    """门控观察缺失只能降级为 unknown，绝不能被空数组伪装成 pass。"""
    understanding = result.get("video_understanding") if isinstance(result.get("video_understanding"), dict) else {}
    findings = {
        str(item.get("id") or ""): item
        for item in (result.get("global_diagnosis") or {}).get("findings", [])
        if isinstance(item, dict)
    }
    warnings: list[str] = []
    status_to_gate = {
        "selling_point_route": "selling_point_route",
        "variant_focus": "focus_coherence",
        "attention_scan": "attention_cleanliness",
    }
    for role in ("creator", "benchmark"):
        side = understanding.get(role) if isinstance(understanding.get(role), dict) else {}
        status = side.get("gate_observation_status") if isinstance(side.get("gate_observation_status"), dict) else {}
        for status_key, gate_id in status_to_gate.items():
            if status.get(status_key) == "complete":
                continue
            warnings.append(f"{role}.{status_key} 门控观察未完成，已按 unknown 降级")
            if role == "creator" and str(findings.get(gate_id, {}).get("impact") or "") != "unknown":
                raise SystemExit(f"{gate_id} 在 creator 观察未完成时不得输出确定性结论。")
        invalid_units = [
            str(unit.get("id") or "")
            for unit in side.get("evidence_units") or []
            if (
                isinstance(unit, dict)
                and "_NO_" not in str(unit.get("id") or "").upper()
                and unit.get("variant_data_valid") is not True
            )
        ]
        if invalid_units:
            warnings.append(f"{role} 变体数据不一致：{', '.join(invalid_units)}")
    append_qa_warnings(result, warnings)


# Q19 用：S6 口播/字幕中的购买指令信号，与 repair.align_timed_cta 的关键词口径一致。
_CTA_SIGNAL_RE = re.compile(
    r"\b(beli|troli|klik|cart|checkout|order|link|direct)\b|购买|下单|购物车|点击|check\s*out|bag\s+kuning|beg\s+kuning",
    re.IGNORECASE,
)
_NO_CTA_CLAIM_RE = re.compile(
    r"无\s*CTA|没有(明确的?)?(CTA|购买指令|行动指令|购买路径)|缺乏?(明确的?)?(CTA|购买指令|行动指令)"
    r"|CTA\s*缺失|未(给出|提供|出现)(明确的?)?(CTA|购买指令)|CTA\s*前(就)?结束",
    re.IGNORECASE,
)
_HAS_CTA_CLAIM_RE = re.compile(
    r"明确(的)?(告知|提及|给出|引导|购买指令|行动指令|CTA)|清晰的?购买(路径|指令)",
    re.IGNORECASE,
)
# 比较性指代（"标杆那种""不弱于标杆""像达人"）不是主语，识别主语前先剥掉。
_ROLE_REF_RE = re.compile(r"(像|如|与|和|比|于|借鉴|参考|对比)(达人|标杆)|(达人|标杆)(那种|那样|的|般|式)")


def _subject_clauses(narrative: str) -> list[tuple[str, str]]:
    """把叙事拆成（主语, 子句）对：句内按逗号细分，无主语段继承句内最近主语。

    实证驱动的两种 gap 形态：①"标杆X，达人Y"对比句（逗号分隔、各自主语，整句归一个
    主语会互串）；②"达人…，但明确告知…"（主语只在首段、后段继承）。
    """
    pairs: list[tuple[str, str]] = []
    for sentence in re.split(r"[。；;!?\n]", narrative):
        current = ""
        for segment in re.split(r"[，,]", sentence):
            stripped = _ROLE_REF_RE.sub("", segment)
            has_creator = "达人" in stripped
            has_benchmark = "标杆" in stripped
            if has_creator and has_benchmark:
                # 罕见的段内双主语：两边都查，且不向后继承
                pairs.append(("达人", segment))
                pairs.append(("标杆", segment))
                current = ""
                continue
            if has_creator:
                current = "达人"
            elif has_benchmark:
                current = "标杆"
            if current:
                pairs.append((current, segment))
    return pairs


def validate_narrative_evidence_consistency(result: dict[str, Any]) -> None:
    """Q19：S6 叙事文本与口播证据矛盾时写警告，不阻断。

    实证（2026-06-10，双向）：are_xie S6 假阴性（达人口播近半是购买指令，叙事却写
    "在有效 CTA 前结束"）；kakwan S6 假阳性（达人无任何购买指令，Phase C 叙事却写
    "明确告知链接在购物车"）。quote 有 validate_transcript_attribution 管，叙事文本在此兜底。
    """
    stages = result.get("stage_analysis", [])
    if len(stages) < 6:
        return
    cta = stages[5]
    understanding = result.get("video_understanding", {})
    narrative = "。".join(
        [
            str(cta.get("gap") or ""),
            "。".join(str(item) for item in cta.get("gap_summary") or []),
            str(cta.get("creator_summary") or ""),
            str(cta.get("creator_key_message") or ""),
            str(cta.get("benchmark_summary") or ""),
            str(cta.get("benchmark_key_message") or ""),
        ]
    )
    warnings: list[str] = []
    for role, subject in (("creator", "达人"), ("benchmark", "标杆")):
        # 证据面：阶段 quote/画面 + 所引用 evidence_unit 的口播/字幕/信息。
        references = {str(value) for value in cta.get(f"{role}_evidence_ids", [])}
        unit_texts = [
            f"{unit.get('voiceover') or ''} {unit.get('subtitle_fact') or ''} {unit.get('information') or ''}"
            for unit in understanding.get(role, {}).get("evidence_units", [])
            if isinstance(unit, dict) and str(unit.get("id")) in references
        ]
        evidence = " ".join(
            [
                str(cta.get(f"{role}_quote") or ""),
                " ".join(str(value) for value in cta.get(f"{role}_visual_evidence") or []),
                *unit_texts,
            ]
        )
        has_signal = bool(_CTA_SIGNAL_RE.search(evidence))
        # 只看主语归属该 role 的子句（句内逗号细分+主语继承），避免"标杆有/达人无"互相误伤。
        for clause_subject, clause in _subject_clauses(narrative):
            if clause_subject != subject:
                continue
            # 同一子句两种声称互斥：否定声称（"缺乏明确的购买指令"）内含肯定词组，
            # 必须先判否定、命中即不再判肯定，否则否定子句会误触发"疑似脑补"分支。
            if _NO_CTA_CLAIM_RE.search(clause):
                if has_signal:
                    warnings.append(
                        f"[Q19] S6 叙事称{subject}缺少 CTA，但其证据含明确购买指令，叙事与证据矛盾，需人工复核。"
                    )
                    break
            elif _HAS_CTA_CLAIM_RE.search(clause) and not has_signal:
                warnings.append(
                    f"[Q19] S6 叙事称{subject}有明确 CTA/购买引导，但其证据未见购买指令，疑似脑补，需人工复核。"
                )
                break
    append_qa_warnings(result, warnings)


def validate_module_ids(result: dict[str, Any]) -> None:
    """Q02/G02：module_id 必须来自 structure_library_full.md，且前缀匹配阶段。"""
    valid_ids = official_module_ids()
    invalid: list[str] = []
    for index, stage in enumerate(result.get("stage_analysis", []), start=1):
        expected_prefix = f"S{index}-"
        for role in ("creator", "benchmark"):
            key = f"{role}_module_id"
            module_id = str(stage.get(key) or "").strip()
            if not module_id or module_id == "unknown":
                continue
            if module_id not in valid_ids:
                invalid.append(f"{stage.get('stage')} {key}={module_id} 不在结构库官方编号中")
                continue
            if not module_id.startswith(expected_prefix):
                invalid.append(f"{stage.get('stage')} {key}={module_id} 与阶段前缀 {expected_prefix} 不匹配")
    if invalid:
        raise SystemExit("模块编号不符合 QA-RULES： " + "；".join(invalid))


def validate_s1_hook_flags(result: dict[str, Any], analysis: dict[str, Any]) -> None:
    """S1 hook flag 化硬门禁：新主链必须产出结构化 hook facts。

    历史 analysis_result 可能没有 creator_hook/benchmark_hook，因此只在主链显式标记
    s1_hook_flags_required，或结果已经出现任一 hook 字段时启用。启用后缺字段即触发
    repair，避免 derive 静默回退到模型 0-2 主观执行分。
    """
    stages = result.get("stage_analysis", [])
    if not stages or not isinstance(stages[0], dict):
        return
    s1 = stages[0]
    has_any_hook = isinstance(s1.get("creator_hook"), dict) or isinstance(s1.get("benchmark_hook"), dict)
    if not analysis.get("s1_hook_flags_required") and not has_any_hook:
        return

    errors: list[str] = []
    for role in ("creator", "benchmark"):
        key = f"{role}_hook"
        hook = s1.get(key)
        if not isinstance(hook, dict):
            errors.append(f"S1 缺少 {key}")
            continue
        if hook.get("exists") not in {True, False}:
            errors.append(f"S1 {key}.exists 必须是 bool")
        if str(hook.get("type") or "").strip() not in {"A", "B", "C", "D", "E", "F", "G", "unknown"}:
            errors.append(f"S1 {key}.type 必须是 A-G 或 unknown")
        dims = hook.get("dims")
        if not isinstance(dims, dict):
            errors.append(f"S1 {key}.dims 必须是 object")
        else:
            for dim in ("camera", "copy", "sound", "rhythm"):
                if dims.get(dim) not in {True, False}:
                    errors.append(f"S1 {key}.dims.{dim} 必须是 bool")
        if hook.get("landing_met") not in {True, False}:
            errors.append(f"S1 {key}.landing_met 必须是 bool")
        if not str(hook.get("landing_reason") or "").strip():
            errors.append(f"S1 {key}.landing_reason 不能为空")
        if not str(hook.get("window_evidence") or "").strip():
            errors.append(f"S1 {key}.window_evidence 不能为空")
        boundary = hook.get("hook_boundary_seconds")
        if not isinstance(boundary, (int, float)) or boundary < 0:
            errors.append(f"S1 {key}.hook_boundary_seconds 必须是非负数字")
        if not str(hook.get("hook_boundary_reason") or "").strip():
            errors.append(f"S1 {key}.hook_boundary_reason 不能为空")
        if not str(hook.get("s2_start_signal") or "").strip():
            errors.append(f"S1 {key}.s2_start_signal 不能为空")
        if hook.get("landing_window_leak") not in {True, False}:
            errors.append(f"S1 {key}.landing_window_leak 必须是 bool")
        if hook.get("anchors_proposition") not in {True, False}:
            errors.append(f"S1 {key}.anchors_proposition 必须是 bool")
    if errors:
        raise SystemExit("S1 hook flag 输出不完整：" + "；".join(errors))


def validate_s2_contract_flags(result: dict[str, Any], analysis: dict[str, Any]) -> None:
    """S2 产品引出契约 flag 门禁。

    历史结果可能没有 creator_s2/benchmark_s2，因此只在主链显式要求或结果已含字段时校验。
    """
    stages = result.get("stage_analysis", [])
    if len(stages) < 2 or not isinstance(stages[1], dict):
        return
    s2 = stages[1]
    has_any_s2 = isinstance(s2.get("creator_s2"), dict) or isinstance(s2.get("benchmark_s2"), dict)
    if not analysis.get("s2_flags_required") and not has_any_s2:
        return

    errors: list[str] = []
    for role in ("creator", "benchmark"):
        key = f"{role}_s2"
        flag = s2.get(key)
        if not isinstance(flag, dict):
            errors.append(f"S2 缺少 {key}")
            continue
        for bool_key in (
            "exists",
            "merged_with_s3",
            "handoff_met",
            "s1_s2_compatible",
            "product_identity_clear",
            "product_role_clear",
            "excluded_or_risky_module",
        ):
            if flag.get(bool_key) not in {True, False}:
                errors.append(f"S2 {key}.{bool_key} 必须是 bool")
        if str(flag.get("module_type") or "").strip() not in {"A", "B", "C", "D", "unknown"}:
            errors.append(f"S2 {key}.module_type 必须是 A-D 或 unknown")
        start = flag.get("start_seconds")
        end = flag.get("end_seconds")
        if not isinstance(start, (int, float)) or isinstance(start, bool) or start < 0:
            errors.append(f"S2 {key}.start_seconds 必须是非负数字")
        if not isinstance(end, (int, float)) or isinstance(end, bool) or end < 0:
            errors.append(f"S2 {key}.end_seconds 必须是非负数字")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) and not isinstance(start, bool) and not isinstance(end, bool) and end < start:
            errors.append(f"S2 {key}.end_seconds 必须大于等于 start_seconds")
        if not str(flag.get("handoff_reason") or "").strip():
            errors.append(f"S2 {key}.handoff_reason 不能为空")
        if not flag.get("evidence_ids"):
            errors.append(f"S2 {key}.evidence_ids 不能为空")
    if errors:
        raise SystemExit("S2 产品引出契约 flag 输出不完整：" + "；".join(errors))


def validate_s3_usage_flags(result: dict[str, Any], analysis: dict[str, Any]) -> None:
    """S3 使用过程 flag 门禁。

    历史结果可能没有 creator_s3/benchmark_s3，因此只在主链显式要求或结果已含字段时校验。
    """
    stages = result.get("stage_analysis", [])
    if len(stages) < 3 or not isinstance(stages[2], dict):
        return
    s3 = stages[2]
    has_any_s3 = isinstance(s3.get("creator_s3"), dict) or isinstance(s3.get("benchmark_s3"), dict)
    if not analysis.get("s3_flags_required") and not has_any_s3:
        return

    errors: list[str] = []
    for role in ("creator", "benchmark"):
        key = f"{role}_s3"
        flag = s3.get(key)
        if not isinstance(flag, dict):
            errors.append(f"S3 缺少 {key}")
            continue
        for bool_key in (
            "exists",
            "usage_process_visible",
            "result_only_without_process",
            "mouth_only_or_static",
            "real_usage_met",
            "core_selling_point_visible",
            "process_framing_met",
            "action_proof_met",
            "action_target_contact_met",
            "action_application_change_visible",
            "critical_action_continuity_met",
            "usage_context_fit",
            "continuity_met",
            "richness_met",
            "single_scene_continuity_met",
            "single_scene_variation_met",
            "multi_scene_logic_met",
            "multi_scene_transition_met",
            "multi_scene_role_adaptation_met",
            "role_design_met",
            "role_interaction_met",
            "distinct_personas_met",
            "steps_clear_met",
            "pov_immersive_met",
            "fake_or_staged",
        ):
            if flag.get(bool_key) not in {True, False}:
                errors.append(f"S3 {key}.{bool_key} 必须是 bool")
        if str(flag.get("module_type") or "").strip() not in {"A", "B", "C", "D", "E", "unknown"}:
            errors.append(f"S3 {key}.module_type 必须是 A-E 或 unknown")
        if str(flag.get("scene_mode") or "").strip() not in {"single_scene", "multi_scene", "multi_person", "hybrid", "unknown"}:
            errors.append(f"S3 {key}.scene_mode 必须是 single_scene/multi_scene/multi_person/hybrid/unknown")
        overlays = flag.get("presentation_overlays")
        if not isinstance(overlays, list) or not overlays:
            errors.append(f"S3 {key}.presentation_overlays 必须是非空数组")
        elif any(str(item) not in {"step_breakdown", "first_person", "asmr", "closeup", "none"} for item in overlays):
            errors.append(f"S3 {key}.presentation_overlays 含非法值")
        start = flag.get("start_seconds")
        end = flag.get("end_seconds")
        if not isinstance(start, (int, float)) or isinstance(start, bool) or start < 0:
            errors.append(f"S3 {key}.start_seconds 必须是非负数字")
        if not isinstance(end, (int, float)) or isinstance(end, bool) or end < 0:
            errors.append(f"S3 {key}.end_seconds 必须是非负数字")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) and not isinstance(start, bool) and not isinstance(end, bool) and end < start:
            errors.append(f"S3 {key}.end_seconds 必须大于等于 start_seconds")
        if not str(flag.get("usage_reason") or "").strip():
            errors.append(f"S3 {key}.usage_reason 不能为空")
        needs_evidence = (
            flag.get("exists") is not False
            or flag.get("usage_process_visible") is True
            or flag.get("real_usage_met") is True
            or flag.get("core_selling_point_visible") is True
        )
        if needs_evidence and not flag.get("evidence_ids"):
            errors.append(f"S3 {key}.evidence_ids 不能为空")
    if errors:
        raise SystemExit("S3 使用过程 flag 输出不完整：" + "；".join(errors))


def validate_s4_effect_flags(result: dict[str, Any], analysis: dict[str, Any]) -> None:
    """S4 效果因果 flag 门禁。"""
    stages = result.get("stage_analysis", [])
    if len(stages) < 4 or not isinstance(stages[3], dict):
        return
    s4 = stages[3]
    has_any_s4 = isinstance(s4.get("creator_s4"), dict) or isinstance(s4.get("benchmark_s4"), dict)
    if not analysis.get("s4_flags_required") and not has_any_s4:
        return

    errors: list[str] = []
    for role in ("creator", "benchmark"):
        key = f"{role}_s4"
        flag = s4.get(key)
        if not isinstance(flag, dict):
            errors.append(f"S4 缺少 {key}")
            continue
        for bool_key in (
            "effect_visible",
            "effect_proposition_matched",
            "comparison_control_met",
            "closeup_or_focus_met",
            "visual_difference_observed",
            "module_constraints_met",
            "effect_maximized",
            "requires_close_inspection",
            "effect_attribution_supported",
            "result_only_without_process",
            "process_linked_effect",
            "tamper_or_cut_risk",
        ):
            if flag.get(bool_key) not in {True, False}:
                errors.append(f"S4 {key}.{bool_key} 必须是 bool")
        if str(flag.get("effect_type") or "").strip() not in {
            "before_after",
            "split_screen",
            "person_vs_person",
            "product_vs_alt",
            "quantified_test",
            "process_visualization",
            "aesthetic_display",
            "none",
        }:
            errors.append(f"S4 {key}.effect_type 非法")
        if str(flag.get("effect_salience") or "").strip() not in {"none", "subtle", "clear", "strong"}:
            errors.append(f"S4 {key}.effect_salience 必须是 none/subtle/clear/strong")
        if not str(flag.get("effect_reason") or "").strip():
            errors.append(f"S4 {key}.effect_reason 不能为空")
        effect_type = str(flag.get("effect_type") or "").strip()
        needs_evidence = flag.get("effect_visible") is True or effect_type not in {"", "none", "unknown"}
        if needs_evidence and not flag.get("evidence_ids"):
            errors.append(f"S4 {key}.evidence_ids 不能为空")
    if errors:
        raise SystemExit("S4 效果因果 flag 输出不完整：" + "；".join(errors))


def validate_s5_trust_flags(result: dict[str, Any], analysis: dict[str, Any]) -> None:
    """S5 信任放大 flag 门禁。"""
    stages = result.get("stage_analysis", [])
    if len(stages) < 5 or not isinstance(stages[4], dict):
        return
    s5 = stages[4]
    has_any_s5 = isinstance(s5.get("creator_s5"), dict) or isinstance(s5.get("benchmark_s5"), dict)
    if not analysis.get("s5_flags_required") and not has_any_s5:
        return

    errors: list[str] = []
    source_signals_required = analysis.get("s5_source_signals_required") is True
    valid_bases = {"authority", "traceable_data", "independent_user", "social_consensus", "process_transparency"}
    for role in ("creator", "benchmark"):
        key = f"{role}_s5"
        flag = s5.get(key)
        if not isinstance(flag, dict):
            errors.append(f"S5 缺少 {key}")
            continue
        for bool_key in (
            "exists",
            "trust_source_visible",
            "trust_source_credible",
            "trust_claim_specific",
            "product_relevance_met",
            "independent_trust_purpose",
            "duplicates_other_stage",
            "voice_only",
            "risky_or_unsupported",
        ):
            if flag.get(bool_key) not in {True, False}:
                errors.append(f"S5 {key}.{bool_key} 必须是 bool")
        if str(flag.get("module_type") or "").strip() not in {"A", "B", "C", "D", "E", "unknown"}:
            errors.append(f"S5 {key}.module_type 必须是 A-E 或 unknown")
        if str(flag.get("trust_evidence_type") or "").strip() not in {"hard", "soft", "mixed", "none", "unknown"}:
            errors.append(f"S5 {key}.trust_evidence_type 非法")
        if str(flag.get("trust_basis") or "").strip() not in {
            "authority", "traceable_data", "independent_user", "social_consensus", "process_transparency",
            "product_claim", "offer_or_spec", "none", "unknown",
        }:
            errors.append(f"S5 {key}.trust_basis 非法")
        if str(flag.get("trust_basis") or "") in {"product_claim", "offer_or_spec", "none", "unknown"}:
            if flag.get("exists") is not False:
                errors.append(f"S5 {key}.trust_basis 不构成独立信任时 exists 必须为 false")
            if flag.get("independent_trust_purpose") is not False:
                errors.append(f"S5 {key}.trust_basis 不构成独立信任时 independent_trust_purpose 必须为 false")
        source_ids = flag.get("trust_source_evidence_ids")
        if source_signals_required and not isinstance(source_ids, list):
            errors.append(f"S5 {key}.trust_source_evidence_ids 必须是数组")
        if source_signals_required and str(flag.get("trust_basis") or "") in valid_bases:
            unit_map = {
                str(unit.get("id") or ""): (
                    set(unit.get("trust_source_signals") or []),
                    str(unit.get("trust_source_reference") or "").strip(),
                )
                for unit in result.get("video_understanding", {}).get(role, {}).get("evidence_units", [])
                if isinstance(unit, dict)
            }
            basis = str(flag.get("trust_basis") or "")
            if not source_ids or not any(
                basis in unit_map.get(str(item), (set(), ""))[0]
                and unit_map.get(str(item), (set(), ""))[1]
                for item in source_ids
            ):
                errors.append(f"S5 {key}.{basis} 缺少阶段一同类型信任来源证据")
        start = flag.get("start_seconds")
        end = flag.get("end_seconds")
        if not isinstance(start, (int, float)) or isinstance(start, bool) or start < 0:
            errors.append(f"S5 {key}.start_seconds 必须是非负数字")
        if not isinstance(end, (int, float)) or isinstance(end, bool) or end < 0:
            errors.append(f"S5 {key}.end_seconds 必须是非负数字")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) and not isinstance(start, bool) and not isinstance(end, bool) and end < start:
            errors.append(f"S5 {key}.end_seconds 必须大于等于 start_seconds")
        if not str(flag.get("trust_reason") or "").strip():
            errors.append(f"S5 {key}.trust_reason 不能为空")
        needs_evidence = flag.get("exists") is not False and str(flag.get("trust_evidence_type") or "unknown") not in {"none", "unknown"}
        if needs_evidence and not flag.get("evidence_ids"):
            errors.append(f"S5 {key}.evidence_ids 不能为空")
    if errors:
        raise SystemExit("S5 信任放大 flag 输出不完整：" + "；".join(errors))


def validate_s6_cta_flags(result: dict[str, Any], analysis: dict[str, Any]) -> None:
    """S6 CTA flag 门禁。"""
    stages = result.get("stage_analysis", [])
    if len(stages) < 6 or not isinstance(stages[5], dict):
        return
    s6 = stages[5]
    has_any_s6 = isinstance(s6.get("creator_s6"), dict) or isinstance(s6.get("benchmark_s6"), dict)
    if not analysis.get("s6_flags_required") and not has_any_s6:
        return

    errors: list[str] = []
    for role in ("creator", "benchmark"):
        key = f"{role}_s6"
        flag = s6.get(key)
        if not isinstance(flag, dict):
            errors.append(f"S6 缺少 {key}")
            continue
        for bool_key in (
            "exists",
            "direct_order_met",
            "action_path_clear",
            "soft_purchase_invitation_met",
            "offer_or_incentive_clear",
            "price_anchor_met",
            "urgency_evidence_met",
            "gift_stack_met",
            "guarantee_clear_met",
            "urgency_met",
            "product_value_recalled",
            "module_fit_met",
            "ending_position_met",
            "depends_on_valid_s4",
            "compliance_risk",
        ):
            if flag.get(bool_key) not in {True, False}:
                errors.append(f"S6 {key}.{bool_key} 必须是 bool")
        if str(flag.get("module_type") or "").strip() not in {"A", "B", "C", "D", "E", "unknown"}:
            errors.append(f"S6 {key}.module_type 必须是 A-E 或 unknown")
        start = flag.get("start_seconds")
        end = flag.get("end_seconds")
        if not isinstance(start, (int, float)) or isinstance(start, bool) or start < 0:
            errors.append(f"S6 {key}.start_seconds 必须是非负数字")
        if not isinstance(end, (int, float)) or isinstance(end, bool) or end < 0:
            errors.append(f"S6 {key}.end_seconds 必须是非负数字")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) and not isinstance(start, bool) and not isinstance(end, bool) and end < start:
            errors.append(f"S6 {key}.end_seconds 必须大于等于 start_seconds")
        if not str(flag.get("cta_reason") or "").strip():
            errors.append(f"S6 {key}.cta_reason 不能为空")
        if flag.get("exists") is not False and not flag.get("evidence_ids"):
            errors.append(f"S6 {key}.evidence_ids 不能为空")
    if errors:
        raise SystemExit("S6 CTA flag 输出不完整：" + "；".join(errors))


def validate_multimodal_assessments(result: dict[str, Any], analysis: dict[str, Any]) -> None:
    """跨模态综合门禁：渠道事实必须完整、可追溯，净效果不能与渠道信号自相矛盾。"""
    stages = [stage for stage in result.get("stage_analysis", []) if isinstance(stage, dict)]
    has_any = any(
        isinstance(stage.get(f"{role}_multimodal"), dict)
        for stage in stages
        for role in ("creator", "benchmark")
    )
    if not analysis.get("multimodal_assessment_required") and not has_any:
        return

    errors: list[str] = []
    evidential_impacts = {"strong_positive", "positive", "negative", "strong_negative"}
    stage_functions = {
        "S1": "S1_hook",
        "S2": "S2_intro",
        "S3": "S3_usage",
        "S4": "S4_effect",
        "S5": "S5_trust",
        "S6": "S6_cta",
    }
    understanding = result.get("video_understanding") if isinstance(result.get("video_understanding"), dict) else {}
    for index, stage in enumerate(stages, start=1):
        stage_id = f"S{index}"
        for role in ("creator", "benchmark"):
            key = f"{role}_multimodal"
            assessment = stage.get(key)
            if not isinstance(assessment, dict):
                errors.append(f"{stage_id} 缺少 {key}")
                continue
            impacts = assessment.get("channel_impacts")
            evidence = assessment.get("channel_evidence_ids")
            if not isinstance(impacts, dict) or not isinstance(evidence, dict):
                errors.append(f"{stage_id} {key} 渠道对象不完整")
                continue
            stage_refs = {str(item) for item in stage.get(f"{role}_evidence_ids") or [] if str(item).strip()}
            # 阶段主引用是报告摘要用的代表证据，不必穷举该阶段所有渠道事实。多模态字段
            # 还可引用 Stage1 已锁定为同阶段功能的其它单元；否则摘要只选一帧时会把真实
            # 的声音/画面证据误报成“跨阶段”。没有 functions 的旧结果仍只认主引用。
            role_understanding = understanding.get(role) if isinstance(understanding.get(role), dict) else {}
            locked_stage_refs = {
                str(unit.get("id"))
                for unit in role_understanding.get("evidence_units") or []
                if isinstance(unit, dict)
                and stage_functions[stage_id] in {str(value) for value in unit.get("functions") or []}
            }
            allowed_refs = stage_refs | locked_stage_refs
            for channel in MULTIMODAL_CHANNELS:
                impact = impacts.get(channel)
                if impact not in MULTIMODAL_IMPACTS:
                    errors.append(f"{stage_id} {key}.channel_impacts.{channel} 非法")
                refs = evidence.get(channel)
                if not isinstance(refs, list):
                    errors.append(f"{stage_id} {key}.channel_evidence_ids.{channel} 必须是数组")
                    continue
                if impact in evidential_impacts and not refs:
                    errors.append(f"{stage_id} {key}.{channel} 声称有影响但没有证据")
                missing = [str(item) for item in refs if str(item) not in allowed_refs]
                if missing:
                    errors.append(f"{stage_id} {key}.{channel} 引用非本阶段证据：{','.join(missing)}")

            dominant = assessment.get("dominant_channel")
            relation = assessment.get("cross_channel_relation")
            effect = assessment.get("integrated_effect")
            if dominant not in MULTIMODAL_DOMINANT_CHANNELS:
                errors.append(f"{stage_id} {key}.dominant_channel 非法")
            if relation not in MULTIMODAL_RELATIONS:
                errors.append(f"{stage_id} {key}.cross_channel_relation 非法")
            if effect not in MULTIMODAL_EFFECTS:
                errors.append(f"{stage_id} {key}.integrated_effect 非法")
            if assessment.get("compensation_applied") not in {True, False}:
                errors.append(f"{stage_id} {key}.compensation_applied 必须是 bool")
            if not str(assessment.get("integration_reason") or "").strip():
                errors.append(f"{stage_id} {key}.integration_reason 不能为空")

            dominant_impact = impacts.get(dominant) if dominant in MULTIMODAL_CHANNELS else None
            if effect in {"strong", "effective"} and dominant_impact not in {"strong_positive", "positive"}:
                errors.append(f"{stage_id} {key} 净效果有效但主导渠道不是正向")
            if effect == "strong" and "strong_positive" not in impacts.values():
                errors.append(f"{stage_id} {key} strong 缺少强正向渠道")
            if effect == "strong" and "strong_negative" in impacts.values():
                errors.append(f"{stage_id} {key} 同时存在强负向渠道，不能判 strong")
            if assessment.get("compensation_applied") is True:
                if dominant_impact != "strong_positive":
                    errors.append(f"{stage_id} {key} 补偿成立但主导渠道不够强")
                if not any(impact in {"neutral", "negative", "absent"} for impact in impacts.values()):
                    errors.append(f"{stage_id} {key} 补偿成立但没有被补偿的弱/缺失渠道")
    if errors:
        raise SystemExit("跨模态综合输出不完整：" + "；".join(errors))


def validate_chain_relationships(result: dict[str, Any], analysis: dict[str, Any]) -> None:
    """S3/S4 关系与 S1-S4 承诺闭环门禁。"""
    if not (analysis.get("s3_flags_required") or analysis.get("s4_flags_required")):
        return
    errors: list[str] = []
    rel = result.get("s3_s4_relationship")
    allowed_rel = {
        "process_creates_effect",
        "process_without_effect",
        "result_without_process",
        "no_process_no_effect",
        "aesthetic_no_effect",
        "trust_substitutes_effect",
        "unknown",
    }
    if not isinstance(rel, dict):
        errors.append("缺少 s3_s4_relationship")
    else:
        for key in ("creator_relationship", "benchmark_relationship"):
            if str(rel.get(key) or "").strip() not in allowed_rel:
                errors.append(f"s3_s4_relationship.{key} 非法")
        for key in ("creator_reason", "benchmark_reason"):
            if not str(rel.get(key) or "").strip():
                errors.append(f"s3_s4_relationship.{key} 不能为空")

    chain = result.get("promise_chain")
    if not isinstance(chain, dict):
        errors.append("缺少 promise_chain")
    else:
        for key in ("s1_promise", "s2_answer", "s3_proof_target", "s4_outcome", "break_reason"):
            if not str(chain.get(key) or "").strip():
                errors.append(f"promise_chain.{key} 不能为空")
        if chain.get("chain_closed") not in {True, False}:
            errors.append("promise_chain.chain_closed 必须是 bool")
        if str(chain.get("broken_at") or "").strip() not in {"S2", "S3", "S4", "none", "unknown"}:
            errors.append("promise_chain.broken_at 必须是 S2/S3/S4/none/unknown")
        break_reason = str(chain.get("break_reason") or "")
        # “转化链条”本身不等于 S5/S6；S1-S4 的承诺、证明和效果也可构成转化链路。
        if any(token in break_reason for token in ("S5", "S6", "CTA", "促单", "下单", "购买指令")):
            errors.append("promise_chain.break_reason 只能审计 S1-S4，不得把 S5/S6/CTA 作为断点")
    if errors:
        raise SystemExit("S3/S4 关系或 S1-S4 承诺链输出不完整：" + "；".join(errors))


def validate_stage_time_coherence(result: dict[str, Any]) -> None:
    """Q09/G03：校验时间可解析，并按功能阶段语义审计重叠。

    S1/S2 可在承接点短暂重叠，S3/S4 可共用同段证据，S5 是可出现在任意位置的
    信任支持节点；只有 S2/S3 未声明合并却明显重叠时提示复核。
    """
    warnings: list[str] = []
    for role in ("benchmark", "creator"):
        ranges: dict[str, tuple[float, float]] = {}
        for index, stage in enumerate(result.get("stage_analysis", []), start=1):
            label = str(stage.get("stage") or "")
            time_range = stage.get(f"{role}_time_range")
            start, end = parse_time_range_seconds(time_range, None)
            if end <= start:
                raise SystemExit(f"{label} 的 {role}_time_range 无法形成有效时间段：{time_range}")
            ranges[f"S{index}"] = (start, end)

        if "S2" in ranges and "S3" in ranges and len(result.get("stage_analysis", [])) >= 2:
            s2_start, s2_end = ranges["S2"]
            s3_start, s3_end = ranges["S3"]
            overlap = min(s2_end, s3_end) - max(s2_start, s3_start)
            s2_stage = result["stage_analysis"][1]
            s2_flag = s2_stage.get(f"{role}_s2") if isinstance(s2_stage, dict) else None
            merged = isinstance(s2_flag, dict) and s2_flag.get("merged_with_s3") is True
            if overlap > 0.5 and not merged:
                warnings.append(
                    f"[Q09] {role} S2/S3 重叠 {overlap:.1f}s，但 merged_with_s3=false，需复核引出与使用边界。"
                )
    append_qa_warnings(result, warnings)


def validate_product_visibility(result: dict[str, Any], analysis: dict[str, Any]) -> None:
    """Q09/G04：产品出镜统计必须数值自洽；缺失统计先 warning，不阻断报告。"""
    visibility = result.get("product_visibility", {})
    if not isinstance(visibility, dict):
        raise SystemExit("product_visibility 必须是 object。")

    first = numeric_value(visibility.get("first_appearance_sec"))
    total = numeric_value(visibility.get("total_screen_time_sec"))
    duration = numeric_value(visibility.get("video_duration_sec"))
    ratio = numeric_value(visibility.get("ratio"))
    note = str(visibility.get("estimation_note") or "")

    if any(value is None for value in (first, total, duration, ratio)):
        raise SystemExit("product_visibility 必须包含可解析的 first_appearance_sec、total_screen_time_sec、video_duration_sec、ratio。")
    assert first is not None and total is not None and duration is not None and ratio is not None

    if first < 0 or total < 0 or ratio < 0 or ratio > 1:
        raise SystemExit("product_visibility 数值越界：first/total/ratio 必须非负，ratio 必须在 0~1。")
    if duration < 0:
        raise SystemExit("product_visibility.video_duration_sec 不能为负数。")
    if duration > 0 and (first > duration + 1.0 or total > duration + 1.0):
        raise SystemExit("product_visibility 出镜时间超出视频时长。")
    if duration > 0 and abs(ratio - (total / duration)) > 0.05:
        raise SystemExit("product_visibility.ratio 与 total_screen_time_sec / video_duration_sec 不一致。")

    expected_duration = max(
        numeric_value(analysis.get("videos", {}).get("benchmark", {}).get("duration_seconds")) or 0.0,
        numeric_value(analysis.get("videos", {}).get("creator", {}).get("duration_seconds")) or 0.0,
    )
    warnings: list[str] = []
    if duration == 0 or ("未输出" in note or "需人工复核" in note):
        warnings.append("[Q09] product_visibility 未完成有效统计，报告中的产品出镜数据需人工复核。")
    elif expected_duration and duration > expected_duration + 1.0:
        warnings.append("[Q09] product_visibility.video_duration_sec 大于输入视频时长，需复核统计口径。")
    append_qa_warnings(result, warnings)


def numeric_value(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def append_qa_warnings(result: dict[str, Any], warnings: list[str]) -> None:
    if not warnings:
        return
    existing = result.get("qa_warnings", [])
    if not isinstance(existing, list):
        existing = []
    result["qa_warnings"] = list(dict.fromkeys([*existing, *warnings]))


def validate_transcript_attribution(result: dict[str, Any], analysis: dict[str, Any]) -> None:
    """禁止 benchmark 的 evidence_unit 引用 creator 的口播原文，反之亦然。"""
    transcript_text = {
        role: normalized_transcript_text(read_transcript_text(info))
        for role, info in analysis.get("videos", {}).items()
        if role in {"benchmark", "creator"}
    }
    if not transcript_text.get("benchmark") or not transcript_text.get("creator"):
        return
    for role, other_role in (("benchmark", "creator"), ("creator", "benchmark")):
        units = result.get("video_understanding", {}).get(role, {}).get("evidence_units", [])
        for unit in units:
            quote = normalized_transcript_text(str(unit.get("voiceover") or "")) if isinstance(unit, dict) else ""
            if len(quote) < 12 or quote in transcript_text[role]:
                continue
            if quote in transcript_text[other_role]:
                raise SystemExit(
                    f"{role} 证据 {unit.get('id')} 的口播实际来自 {other_role} 转写，禁止跨视频串证据。"
                )


def validate_stage_ownership(result: dict[str, Any]) -> None:
    """校验共享阶段归属策略：认证只能归 S5，且不得跨阶段重复。"""
    stages = result.get("stage_analysis", [])
    if not stages:
        return
    for role, label in (("benchmark", "标杆"), ("creator", "达人")):
        hook_text = json.dumps(_role_claim_payload(stages[0], role), ensure_ascii=False)
        if contains_certification(hook_text):
            raise SystemExit(f"S1 Hook 不得承载 {label}的 KKM/认证信息；第三方认证是外部背书，按功能归入 S5 信任放大，并标明画面是否验证。")
        certification_stages = [
            str(stage.get("stage") or f"S{index}")
            for index, stage in enumerate(stages, start=1)
            if contains_certification(json.dumps(_role_claim_payload(stage, role), ensure_ascii=False))
        ]
        if len(certification_stages) > 1:
            raise SystemExit(
                f"{label}认证信息不得重复归入多个阶段；请选择其主要作用阶段一次呈现。"
                f"当前重复阶段：{', '.join(certification_stages)}。"
            )
        if certification_stages and not is_certification_owner_stage(certification_stages[0]):
            raise SystemExit(f"第三方认证是外部背书，只能归入 {CERTIFICATION_OWNER_STAGE} 信任放大，不得归入其他阶段。")
