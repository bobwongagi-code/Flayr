"""flayr_core.llm.parse：JSON 解析 + schema 规范化。

职责：
  - 把 LLM 返回的原始文本 / 半结构 dict 解析为合法 JSON
  - 按 references/analysis-output-schema.json 把 dict 字段补齐、归一为 schema 规范结构
  - 提供阶段目录兼容视图 STAGES、口播有效性判断 is_effective_voiceover 等基础工具
    （这些被 postprocess 也复用；放在 parse 是为了让依赖单向：postprocess → parse）

不依赖 postprocess 任何函数；不做业务规则修补；不引用业务规则关键词。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..artifacts import format_seconds, parse_time_range_seconds
from ..multimodal import (
    MULTIMODAL_CHANNELS,
    MULTIMODAL_DOMINANT_CHANNELS,
    MULTIMODAL_EFFECTS,
    MULTIMODAL_IMPACTS,
    MULTIMODAL_RELATIONS,
)
from ..stage_catalog import stage_tuples
from ..structure_modules import canonical_module_id, stage1_event_catalog
from .analysis_contract import AnalysisContractError, validate_raw_analysis_envelope
from .json_codec import escape_unquoted_string_quotes, parse_json_text, remove_trailing_commas
from .product_profile import normalize_category_profile, normalize_product_profile


# 兼容旧 caller 的三元组视图；唯一来源在 stage_catalog.DEFAULT_STAGES。
STAGES = stage_tuples()
EVIDENCE_STRENGTHS = ("direct", "explicit", "inferred", "absent")
S5_SOURCE_STATUSES = ("missing", "uncertain", "explicit_absent", "explicit_present")


# ---------------------------------------------------------------------------
# 字段级 normalize 工具
# ---------------------------------------------------------------------------

def required_text(item: dict[str, Any], key: str) -> str:
    """已弱化为软兜底：缺字段时填占位，让流程跑通，由报告读者人工识别空字段。

    名字保留为 required_text 避免破坏调用点；后续 QA-RULES 实施时可由 R02/R03 接管
    "必填字段"的严格性，届时本函数可改回抛 SystemExit 或拆为 hard/soft 两个版本。
    """
    value = str(item.get(key) or "").strip()
    return value or f"（LLM 未填写 {key}，需人工补充）"


def normalize_evidence(value: Any) -> list[str]:
    if isinstance(value, list):
        evidence = [str(item).strip() for item in value if str(item).strip()]
        return evidence[:5]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def normalize_evidence_strength(value: Any) -> str | None:
    """归一 Stage1 canonical evidence strength；不合法或缺失保持 unknown(None)。"""
    text = str(value or "").strip().lower()
    return text if text in EVIDENCE_STRENGTHS else None


def normalize_proposition_ids(value: Any) -> list[str]:
    """归一命题引用并去重；不在解析层判断 ID 是否属于某阶段。"""
    return list(dict.fromkeys(normalize_evidence(value)))


def normalize_support_status(value: Any, quote: Any) -> str:
    status = str(value or "").strip().lower()
    if status in {"supported", "voice_only", "visual_only", "conflict"}:
        return status
    return "voice_only" if str(quote or "").strip() else "visual_only"


def normalize_demo_flag(value: Any) -> bool | None:
    """演示布尔通用归一（S4 has_effect_demo / S3 has_usage_demo）。返回 True/False；
    非该阶段/null/缺失/无法解析→None；None 不触发确定性 severity 约束。
    容忍模型吐 bool 或 true/false/yes/no/1/0 字符串。"""
    if isinstance(value, bool):  # 必须先于 int 判（Python 中 bool 是 int 子类）
        return value
    if isinstance(value, (int, float)):  # 模型偶吐数字 0/1 当布尔
        return True if value == 1 else False if value == 0 else None
    token = str(value or "").strip().lower()
    if token in {"true", "yes", "1"}:
        return True
    if token in {"false", "no", "0"}:
        return False
    return None


def normalize_multimodal_assessment(value: Any) -> dict[str, Any] | None:
    """归一单侧阶段级跨模态净效果，并收口可由渠道事实确定的机械一致性。"""
    if not isinstance(value, dict):
        return None
    raw_impacts = value.get("channel_impacts") if isinstance(value.get("channel_impacts"), dict) else {}
    raw_evidence = (
        value.get("channel_evidence_ids") if isinstance(value.get("channel_evidence_ids"), dict) else {}
    )
    impacts = {
        channel: normalize_choice(raw_impacts.get(channel), MULTIMODAL_IMPACTS, "unknown")
        for channel in MULTIMODAL_CHANNELS
    }
    dominant = normalize_choice(value.get("dominant_channel"), MULTIMODAL_DOMINANT_CHANNELS, "unknown")
    effect = normalize_choice(value.get("integrated_effect"), MULTIMODAL_EFFECTS, "unknown")
    content_channels = ("visual", "speech", "text")
    positive_channels = [
        channel for channel in content_channels
        if impacts[channel] in {"strong_positive", "positive"}
    ]
    if dominant == "sound_rhythm":
        dominant = "unknown"
    if effect in {"strong", "effective"} and impacts.get(dominant) not in {"strong_positive", "positive"}:
        dominant = next(
            (channel for channel in positive_channels if impacts[channel] == "strong_positive"),
            positive_channels[0] if positive_channels else dominant,
        )
    if effect in {"strong", "effective"} and not positive_channels:
        effect = (
            "weak"
            if any(impacts[channel] not in {"absent", "unknown"} for channel in content_channels)
            else "missing"
        )
    if effect == "strong" and not any(impacts[channel] == "strong_positive" for channel in content_channels):
        effect = "effective" if positive_channels else "weak"
    if effect == "strong" and any(impacts[channel] == "strong_negative" for channel in content_channels):
        effect = "effective"
    compensation = normalize_demo_flag(value.get("compensation_applied")) is True
    if compensation and not (
        impacts.get(dominant) == "strong_positive"
        and any(
            impacts[channel] in {"neutral", "negative", "absent"}
            for channel in content_channels
            if channel != dominant
        )
    ):
        compensation = False
    return {
        "channel_impacts": impacts,
        "channel_evidence_ids": {
            channel: normalize_evidence(raw_evidence.get(channel))
            for channel in MULTIMODAL_CHANNELS
        },
        "dominant_channel": dominant,
        "cross_channel_relation": normalize_choice(
            value.get("cross_channel_relation"), MULTIMODAL_RELATIONS, "unknown"
        ),
        "integrated_effect": effect,
        "compensation_applied": compensation,
        "integration_reason": str(value.get("integration_reason") or "").strip(),
    }


_HOOK_TYPE_LETTERS = {"A", "B", "C", "D", "E", "F", "G"}
_S2_TYPE_LETTERS = {"A", "B", "C", "D"}
_S3_TYPE_LETTERS = {"A", "B", "C", "D", "E"}
_S5_TYPE_LETTERS = {"A", "B", "C", "D", "E"}
_S6_TYPE_LETTERS = {"A", "B", "C", "D", "E"}
_HOOK_TS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*s")
_S3_SCENE_MODES = {"single_scene", "multi_scene", "multi_person", "hybrid", "unknown"}
_S3_PRESENTATION_OVERLAYS = {"step_breakdown", "first_person", "asmr", "closeup", "none"}
_S4_EFFECT_TYPES = {
    "before_after",
    "split_screen",
    "person_vs_person",
    "product_vs_alt",
    "quantified_test",
    "process_visualization",
    "aesthetic_display",
    "none",
}
_S4_EFFECT_SALIENCE = {"none", "subtle", "clear", "strong"}
_S5_TRUST_TYPES = {"hard", "soft", "mixed", "none", "unknown"}
_S5_TRUST_BASES = {
    "authority",
    "traceable_data",
    "independent_user",
    "social_consensus",
    "process_transparency",
    "product_claim",
    "offer_or_spec",
    "none",
    "unknown",
}
_S5_SOURCE_SIGNALS = {"authority", "traceable_data", "independent_user", "social_consensus", "process_transparency"}
_S3_S4_RELATIONSHIPS = {
    "process_creates_effect",
    "process_without_effect",
    "result_without_process",
    "no_process_no_effect",
    "aesthetic_no_effect",
    "trust_substitutes_effect",
    "unknown",
}
_PROMISE_BREAK_POINTS = {"S2", "S3", "S4", "none", "unknown"}


def normalize_hook_type(value: Any) -> str:
    """归一 S1 钩子类型到单字母 A-G（结构库 S1-A~G），无法识别→unknown。
    容忍 'B' / 'S1-B' / 's1_b' / 'S1-B：反差震惊型' 等写法。"""
    s = str(value or "").strip().upper().replace("S1-", "").replace("S1_", "")
    s = s[:1]  # 取首字母，容忍 'B：反差震惊型' 这类带后缀写法
    return s if s in _HOOK_TYPE_LETTERS else "unknown"


def normalize_s2_type(value: Any) -> str:
    """归一 S2 产品引出类型到 A-D（结构库 S2-A~D），无法识别→unknown。"""
    s = str(value or "").strip().upper().replace("S2-", "").replace("S2_", "")
    s = s[:1]
    return s if s in _S2_TYPE_LETTERS else "unknown"


def normalize_s3_type(value: Any) -> str:
    """归一 S3 使用过程类型到 A-E（结构库 S3-A~E），无法识别→unknown。"""
    s = str(value or "").strip().upper().replace("S3-", "").replace("S3_", "")
    s = s[:1]
    return s if s in _S3_TYPE_LETTERS else "unknown"


def normalize_s5_type(value: Any) -> str:
    """归一 S5 信任放大类型到 A-E（结构库 S5-A~E），无法识别→unknown。"""
    s = str(value or "").strip().upper().replace("S5-", "").replace("S5_", "")
    s = s[:1]
    return s if s in _S5_TYPE_LETTERS else "unknown"


def normalize_s6_type(value: Any) -> str:
    """归一 S6 CTA 类型到 A-E（结构库 S6-A~E），无法识别→unknown。"""
    s = str(value or "").strip().upper().replace("S6-", "").replace("S6_", "")
    s = s[:1]
    return s if s in _S6_TYPE_LETTERS else "unknown"


def normalize_s3_scene_mode(value: Any) -> str:
    mode = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return mode if mode in _S3_SCENE_MODES else "unknown"


def normalize_presentation_overlays(value: Any) -> list[str]:
    if isinstance(value, list):
        raw = value
    elif isinstance(value, str) and value.strip():
        raw = re.split(r"[,/，、\s]+", value.strip())
    else:
        raw = []
    overlays: list[str] = []
    for item in raw:
        key = str(item or "").strip().lower().replace("-", "_").replace(" ", "_")
        if key in _S3_PRESENTATION_OVERLAYS and key not in overlays:
            overlays.append(key)
    return overlays or ["none"]


def normalize_s4_effect_type(value: Any) -> str:
    effect_type = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return effect_type if effect_type in _S4_EFFECT_TYPES else "none"


def normalize_s4_effect_salience(value: Any) -> str:
    salience = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return salience if salience in _S4_EFFECT_SALIENCE else "none"


def normalize_s5_trust_type(value: Any) -> str:
    trust_type = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return trust_type if trust_type in _S5_TRUST_TYPES else "unknown"


def normalize_s5_trust_basis(value: Any) -> str:
    """归一 S5 信任来源，产品规格与促销不应伪装成独立背书。"""
    basis = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return basis if basis in _S5_TRUST_BASES else "unknown"


def normalize_s5_source_signals(value: Any) -> list[str]:
    """只保留阶段一实际观察到的独立信任来源类型。"""
    values = value if isinstance(value, list) else []
    return list(dict.fromkeys(
        signal for signal in (str(item or "").strip().lower() for item in values)
        if signal in _S5_SOURCE_SIGNALS
    ))


def normalize_s5_source_status(unit: dict[str, Any]) -> str:
    """保留 S5 来源事实的三态边界，避免把缺失字段压成明确不存在。

    空数组且没有出处是模型明确声明“没有来源”；字段缺失、形状非法、来源类型
    与出处不完整则不能当作 absence。该状态由解析层从原始字段存在性计算，供
    postprocess 的 S5 门禁使用。
    """
    if "trust_source_signals" not in unit:
        return "uncertain" if str(unit.get("trust_source_reference") or "").strip() else "missing"
    raw_signals = unit.get("trust_source_signals")
    reference = str(unit.get("trust_source_reference") or "").strip()
    if not isinstance(raw_signals, list):
        return "uncertain"
    has_raw_signal = any(str(item or "").strip() for item in raw_signals)
    signals = normalize_s5_source_signals(raw_signals)
    if not has_raw_signal and not reference:
        return "explicit_absent"
    if signals and reference:
        return "explicit_present"
    return "uncertain"


def normalize_s3_s4_relationship_type(value: Any) -> str:
    rel = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return rel if rel in _S3_S4_RELATIONSHIPS else "unknown"


def normalize_promise_break_point(value: Any) -> str:
    point = str(value or "").strip().upper()
    if point in {"S2", "S3", "S4"}:
        return point
    point = point.lower()
    return point if point in {"none", "unknown"} else "unknown"


def normalize_hook_boundary_seconds(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number >= 0 else None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        number = float(text)
        return number if number >= 0 else None
    except ValueError:
        return None


def hook_reason_window_leaks(reason: str, boundary_seconds: float | None, tolerance: float = 0.3) -> bool:
    """landing_reason 引用了 hook 边界后的时间戳，说明 S1 判断借用了 S2/S3 材料。"""
    if boundary_seconds is None:
        return False
    vals = [float(m) for m in _HOOK_TS_RE.findall(reason or "")]
    return bool(vals and max(vals) > boundary_seconds + tolerance)


def normalize_hook_flags(value: Any) -> dict[str, Any] | None:
    """归一单侧 S1 钩子结构化 flag。整体缺失（非 dict）→None，derive 见 None 回退模型执行分（优雅降级）。
    形状：{exists: bool|None, type: A-G|unknown, dims:{camera/copy/sound/rhythm: bool}, anchors_proposition: bool|None}。
    四维 dims 用 normalize_bool_flag（缺省 False=未做到）；exists/anchors 用 demo_flag 三态（None=模型没判）。"""
    if not isinstance(value, dict):
        return None
    raw_dims = value.get("dims") if isinstance(value.get("dims"), dict) else {}
    boundary_seconds = normalize_hook_boundary_seconds(value.get("hook_boundary_seconds"))
    landing_reason = str(value.get("landing_reason") or "").strip()
    model_leak = normalize_demo_flag(value.get("landing_window_leak"))
    deterministic_leak = hook_reason_window_leaks(landing_reason, boundary_seconds)
    landing_window_leak = bool(model_leak is True or deterministic_leak)
    landing_met = normalize_demo_flag(value.get("landing_met"))
    if landing_window_leak and landing_met is True:
        landing_met = False
    normalized = {
        "exists": normalize_demo_flag(value.get("exists")),
        "type": normalize_hook_type(value.get("type")),
        "dims": {
            "camera": normalize_bool_flag(raw_dims.get("camera")),
            "copy": normalize_bool_flag(raw_dims.get("copy")),
            "sound": normalize_bool_flag(raw_dims.get("sound")),
            "rhythm": normalize_bool_flag(raw_dims.get("rhythm")),
        },
        # landing_met=钩子有没有"打穿"（type 无关三件套：对象/张力/承诺缺一即 false）。三态：None=模型没判。进 severity。
        "landing_met": landing_met,
        # landing_reason=landing 判定的一句话理由（须引最早窗口证据）。审计 + B2 稳定性看依据用，derive 不消费。
        "landing_reason": landing_reason,
        # window_evidence=钩子最早 3-5 秒带时间戳的观察，作 type 依据（审计 + B2 type_accuracy 用，derive 不消费）。
        "window_evidence": str(value.get("window_evidence") or "").strip(),
        "hook_boundary_seconds": boundary_seconds,
        "hook_boundary_reason": str(value.get("hook_boundary_reason") or "").strip(),
        "s2_start_signal": str(value.get("s2_start_signal") or "").strip(),
        "landing_window_leak": landing_window_leak,
        "anchors_proposition": normalize_demo_flag(value.get("anchors_proposition")),
        "evidence_ids": normalize_evidence(value.get("evidence_ids")),
        "proposition_ids": normalize_proposition_ids(value.get("proposition_ids")),
    }
    return normalized


def normalize_s2_flags(value: Any) -> dict[str, Any] | None:
    """归一 S2 产品引出契约 flag。缺失返回 None，derive/validate 按主链标记决定是否消费。"""
    if not isinstance(value, dict):
        return None
    return {
        "exists": normalize_demo_flag(value.get("exists")),
        "merged_with_s3": normalize_demo_flag(value.get("merged_with_s3")),
        "module_type": normalize_s2_type(value.get("module_type")),
        "handoff_met": normalize_demo_flag(value.get("handoff_met")),
        "s1_s2_compatible": normalize_demo_flag(value.get("s1_s2_compatible")),
        "product_identity_clear": normalize_demo_flag(value.get("product_identity_clear")),
        "product_role_clear": normalize_demo_flag(value.get("product_role_clear")),
        "excluded_or_risky_module": normalize_demo_flag(value.get("excluded_or_risky_module")),
        "start_seconds": normalize_hook_boundary_seconds(value.get("start_seconds")),
        "end_seconds": normalize_hook_boundary_seconds(value.get("end_seconds")),
        "handoff_reason": str(value.get("handoff_reason") or "").strip(),
        "evidence_ids": normalize_evidence(value.get("evidence_ids")),
        "proposition_ids": normalize_proposition_ids(value.get("proposition_ids")),
    }


def normalize_s3_flags(value: Any) -> dict[str, Any] | None:
    """归一 S3 使用过程 flag。缺失返回 None，derive/validate 按主链标记决定是否消费。"""
    if not isinstance(value, dict):
        return None
    usage_process_visible = normalize_demo_flag(value.get("usage_process_visible"))
    if usage_process_visible is None:
        usage_process_visible = normalize_demo_flag(value.get("real_usage_met"))
    # 缺失不是“过程完整”。保留 None，让 derive 将其视为未知而不触发规则。
    process_framing_met = normalize_demo_flag(value.get("process_framing_met"))
    scene_mode = normalize_s3_scene_mode(value.get("scene_mode"))
    overlays = normalize_presentation_overlays(value.get("presentation_overlays"))

    def mode_flag(key: str, applicable: bool) -> bool | None:
        if not applicable:
            return False
        return normalize_demo_flag(value.get(key))

    return {
        "exists": normalize_demo_flag(value.get("exists")),
        "module_type": normalize_s3_type(value.get("module_type")),
        "usage_process_visible": usage_process_visible,
        "result_only_without_process": normalize_demo_flag(value.get("result_only_without_process")),
        "mouth_only_or_static": normalize_demo_flag(value.get("mouth_only_or_static")),
        "real_usage_met": normalize_demo_flag(value.get("real_usage_met")),
        "core_selling_point_visible": normalize_demo_flag(value.get("core_selling_point_visible")),
        "process_framing_met": process_framing_met,
        "action_proof_met": normalize_demo_flag(value.get("action_proof_met")),
        # S3 的"真实使用"需要证明产品/材料真正作用于目标对象，动作本身有新施加/位移/激活或目标状态变化，
        # 且关键状态变化没有被跳剪吞掉。旧结果缺字段时保留 None，由 derive 走既有兼容路径；新主链会被 validate 强制要求。
        "action_target_contact_met": normalize_demo_flag(value.get("action_target_contact_met")),
        "action_application_change_visible": normalize_demo_flag(value.get("action_application_change_visible")),
        "critical_action_continuity_met": normalize_demo_flag(value.get("critical_action_continuity_met")),
        "demonstrated_selling_points": normalize_evidence(value.get("demonstrated_selling_points")),
        "missing_selling_points": normalize_evidence(value.get("missing_selling_points")),
        "scene_mode": scene_mode,
        "usage_context_fit": normalize_demo_flag(value.get("usage_context_fit")),
        "continuity_met": normalize_demo_flag(value.get("continuity_met")),
        "richness_met": normalize_demo_flag(value.get("richness_met")),
        "single_scene_continuity_met": mode_flag("single_scene_continuity_met", scene_mode == "single_scene"),
        "single_scene_variation_met": mode_flag("single_scene_variation_met", scene_mode == "single_scene"),
        "multi_scene_logic_met": mode_flag("multi_scene_logic_met", scene_mode == "multi_scene"),
        "multi_scene_transition_met": mode_flag("multi_scene_transition_met", scene_mode == "multi_scene"),
        "multi_scene_role_adaptation_met": mode_flag("multi_scene_role_adaptation_met", scene_mode == "multi_scene"),
        "role_design_met": mode_flag("role_design_met", scene_mode == "multi_person"),
        "role_interaction_met": mode_flag("role_interaction_met", scene_mode == "multi_person"),
        "distinct_personas_met": mode_flag("distinct_personas_met", scene_mode == "multi_person"),
        "steps_clear_met": mode_flag("steps_clear_met", "step_breakdown" in overlays),
        "pov_immersive_met": mode_flag("pov_immersive_met", "first_person" in overlays),
        "presentation_overlays": overlays,
        "fake_or_staged": normalize_demo_flag(value.get("fake_or_staged")),
        "start_seconds": normalize_hook_boundary_seconds(value.get("start_seconds")),
        "end_seconds": normalize_hook_boundary_seconds(value.get("end_seconds")),
        "usage_reason": str(value.get("usage_reason") or "").strip(),
        "evidence_ids": normalize_evidence(value.get("evidence_ids")),
        "proposition_ids": normalize_proposition_ids(value.get("proposition_ids")),
    }


def normalize_s4_flags(value: Any) -> dict[str, Any] | None:
    """归一 S4 效果因果 flag。缺失返回 None，derive 保留旧 S4 路径。"""
    if not isinstance(value, dict):
        return None
    return {
        "effect_type": normalize_s4_effect_type(value.get("effect_type")),
        "effect_visible": normalize_demo_flag(value.get("effect_visible")),
        "effect_salience": normalize_s4_effect_salience(value.get("effect_salience")),
        "effect_proposition_matched": normalize_demo_flag(value.get("effect_proposition_matched")),
        "comparison_control_met": normalize_demo_flag(value.get("comparison_control_met")),
        "closeup_or_focus_met": normalize_demo_flag(value.get("closeup_or_focus_met")),
        "visual_difference_observed": normalize_demo_flag(value.get("visual_difference_observed")),
        "module_constraints_met": normalize_demo_flag(value.get("module_constraints_met")),
        "effect_maximized": normalize_demo_flag(value.get("effect_maximized")),
        "requires_close_inspection": normalize_demo_flag(value.get("requires_close_inspection")),
        "effect_attribution_supported": normalize_demo_flag(value.get("effect_attribution_supported")),
        "result_only_without_process": normalize_demo_flag(value.get("result_only_without_process")),
        "process_linked_effect": normalize_demo_flag(value.get("process_linked_effect")),
        "tamper_or_cut_risk": normalize_demo_flag(value.get("tamper_or_cut_risk")),
        "effect_reason": str(value.get("effect_reason") or "").strip(),
        "evidence_ids": normalize_evidence(value.get("evidence_ids")),
        "proposition_ids": normalize_proposition_ids(value.get("proposition_ids")),
    }


def normalize_s5_flags(value: Any) -> dict[str, Any] | None:
    """归一 S5 信任放大 flag。缺失返回 None，derive 保守保留模型判断。"""
    if not isinstance(value, dict):
        return None
    return {
        "exists": normalize_demo_flag(value.get("exists")),
        "module_type": normalize_s5_type(value.get("module_type")),
        "trust_evidence_type": normalize_s5_trust_type(value.get("trust_evidence_type")),
        "trust_basis": normalize_s5_trust_basis(value.get("trust_basis")),
        "trust_source_evidence_ids": normalize_evidence(value.get("trust_source_evidence_ids")),
        "trust_source_visible": normalize_demo_flag(value.get("trust_source_visible")),
        "trust_source_credible": normalize_demo_flag(value.get("trust_source_credible")),
        "trust_claim_specific": normalize_demo_flag(value.get("trust_claim_specific")),
        "product_relevance_met": normalize_demo_flag(value.get("product_relevance_met")),
        "independent_trust_purpose": normalize_demo_flag(value.get("independent_trust_purpose")),
        "duplicates_other_stage": normalize_demo_flag(value.get("duplicates_other_stage")),
        "voice_only": normalize_demo_flag(value.get("voice_only")),
        "risky_or_unsupported": normalize_demo_flag(value.get("risky_or_unsupported")),
        "start_seconds": normalize_hook_boundary_seconds(value.get("start_seconds")),
        "end_seconds": normalize_hook_boundary_seconds(value.get("end_seconds")),
        "trust_reason": str(value.get("trust_reason") or "").strip(),
        "evidence_ids": normalize_evidence(value.get("evidence_ids")),
        "proposition_ids": normalize_proposition_ids(value.get("proposition_ids")),
    }


def normalize_s6_flags(value: Any) -> dict[str, Any] | None:
    """归一 S6 CTA flag。缺失返回 None，derive 回退旧执行分。"""
    if not isinstance(value, dict):
        return None
    return {
        "exists": normalize_demo_flag(value.get("exists")),
        "module_type": normalize_s6_type(value.get("module_type")),
        "direct_order_met": normalize_demo_flag(value.get("direct_order_met")),
        "action_path_clear": normalize_demo_flag(value.get("action_path_clear")),
        "soft_purchase_invitation_met": normalize_demo_flag(value.get("soft_purchase_invitation_met")),
        "offer_or_incentive_clear": normalize_demo_flag(value.get("offer_or_incentive_clear")),
        "price_anchor_met": normalize_demo_flag(value.get("price_anchor_met")),
        "urgency_evidence_met": normalize_demo_flag(value.get("urgency_evidence_met")),
        "gift_stack_met": normalize_demo_flag(value.get("gift_stack_met")),
        "guarantee_clear_met": normalize_demo_flag(value.get("guarantee_clear_met")),
        "urgency_met": normalize_demo_flag(value.get("urgency_met")),
        "product_value_recalled": normalize_demo_flag(value.get("product_value_recalled")),
        "module_fit_met": normalize_demo_flag(value.get("module_fit_met")),
        "ending_position_met": normalize_demo_flag(value.get("ending_position_met")),
        "depends_on_valid_s4": normalize_demo_flag(value.get("depends_on_valid_s4")),
        "compliance_risk": normalize_demo_flag(value.get("compliance_risk")),
        "start_seconds": normalize_hook_boundary_seconds(value.get("start_seconds")),
        "end_seconds": normalize_hook_boundary_seconds(value.get("end_seconds")),
        "cta_reason": str(value.get("cta_reason") or "").strip(),
        "evidence_ids": normalize_evidence(value.get("evidence_ids")),
        "proposition_ids": normalize_proposition_ids(value.get("proposition_ids")),
    }


def normalize_absolute_execution_shadow(role: str, value: Any) -> dict[str, Any] | None:
    """归一单侧 absolute-execution shadow，不让可选审计阻断主分析。

    返回 None 表示该次审计无有效输出；调用方应记录失败原因并继续主链。该结果
    只供对照和晋级门槛使用，当前不参与 severity 推导。
    """
    if not isinstance(value, dict):
        return None
    items = value.get("stage_execution")
    if not isinstance(items, list):
        return None
    statuses = {"missing", "weak", "competent", "strong"}
    scores = {0, 0.5, 1, 2}
    normalized: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        code = str(item.get("stage") or "").strip().upper()
        if code not in {"S1", "S2", "S3", "S4"} or code in normalized:
            continue
        raw_score = item.get("score")
        if isinstance(raw_score, bool):
            continue
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            continue
        if score not in scores:
            continue
        status = str(item.get("status") or "").strip().lower()
        if status not in statuses:
            continue
        confidence = str(item.get("confidence") or "").strip().lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "low"
        normalized[code] = {
            "score": int(score) if score.is_integer() else score,
            "status": status,
            "reason": str(item.get("reason") or "").strip(),
            "evidence_ids": normalize_evidence(item.get("evidence_ids")),
            "proposition_ids": normalize_proposition_ids(item.get("proposition_ids")),
            "confidence": confidence,
            "source": "single_video_shadow",
            "role": role,
        }
    if set(normalized) != {"S1", "S2", "S3", "S4"}:
        return None
    return {"role": role, "stages": normalized}


def normalize_base_frame_suitability(value: Any, best_time: Any) -> str:
    status = str(value or "").strip().lower()
    if status in {"usable", "no_suitable_frame"}:
        return status
    return "usable" if str(best_time or "").strip() else "no_suitable_frame"


def normalized_base_frame_time(item: dict[str, Any]) -> str:
    if normalize_base_frame_suitability(item.get("base_frame_suitability"), item.get("best_base_frame_time")) == "no_suitable_frame":
        return ""
    return str(item.get("best_base_frame_time") or "").strip()


def normalize_choice(value: Any, allowed: set[str], fallback: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else fallback


def normalize_task_completion(value: Any) -> str:
    """把 task_completion 归一为 complete|partial|missing（达人侧功能完成度）。

    2026-06-11 门禁 T5 发现：模型原始输出是自由文本（'both_complete'/'completed'/
    'benchmark_complete_creator_partial'/'双方均完成了任务'…），旧 normalize_choice 的
    fallback 把一切压成 partial——语义直接错（both_complete 应为 complete）。
    本函数是过渡 shim：prompt 已强制枚举（治本），shim 兜历史漂移；映射规则覆盖
    门禁实测的全部观察值。语义锚定达人侧：双侧编码取 creator 段。
    """
    text = str(value or "").strip().lower()
    if text in {"complete", "partial", "missing"}:
        return text
    # 英文双侧编码：取 creator 侧状态
    match = re.search(r"creator[_\s]*(completed?|partial|incomplete|missing|only|superior|better|stronger|weaker)", text)
    if match:
        word = match.group(1)
        if word in {"complete", "completed", "only", "superior", "better", "stronger"}:
            return "complete"
        if word == "missing":
            return "missing"
        return "partial"
    # 只提 benchmark 强（隐含达人弱）
    if re.search(r"benchmark[_\s]*(superior|better|stronger)", text):
        return "partial"
    # 中文达人侧
    if "达人" in text:
        creator_part = text.split("达人", 1)[1]
        # "达人未完成/未涉及…"直接开头 = 功能未达成 → missing（"未能充分"类弱否定除外）；
        # "达人完成了X，但未完成Y" 后文才出现否定 = partial。
        if re.match(r"\s*未(?!能)", creator_part) or re.search(r"未涉及|未设计|没有(做|设计|涉及)|未做", creator_part):
            return "missing"
        if re.search(r"未完成|部分|基本完成|不完整|不足|仅", creator_part):
            return "partial"
        if re.search(r"完成|做到", creator_part):
            return "complete"
    # 单值/双方（"均(清晰/出色…)完成"允许间插修饰词，但"均未完成"归 missing 在前已拦）
    if re.search(r"both[_\s]*missing|均未(完成|涉及|设计)|missing|absent|none|未涉及|未设计", text):
        return "missing"
    if re.search(r"both[_\s]*(completed?|done|full)|^(completed?|full|done|finished|good|well|ok)$|(双方)?均(?!未).{0,6}完成|完成出色|出色完成", text):
        return "complete"
    if re.search(r"partial|incomplete|部分|基本完成|不完整|不足|weak", text):
        return "partial"
    return "partial"


def normalize_execution_score(value: Any) -> float | None:
    """单侧执行分归一：0=不执行，0.5=敷衍，1=合格，2=好（4d 推导的输入事实）。

    解析失败返回 None——下游 derive 对 None 优雅跳过、保留模型 severity，
    所以这里宁缺毋滥，不做强行兜底映射。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if float(value) in {0.0, 0.5, 1.0, 2.0} else None
    text = str(value or "").strip().lower()
    if not text:
        return None
    try:
        number = float(text)
        return number if number in {0.0, 0.5, 1.0, 2.0} else None
    except ValueError:
        pass
    # 容忍少量语义词漂移（与 task_completion 自由文本的教训一致：枚举指令挡不全）
    if re.search(r"未执行|不执行|没有执行|缺失|none", text):
        return 0.0
    if re.search(r"敷衍|轻带|几乎无效|perfunctory", text):
        return 0.5
    if re.search(r"合格|完成|adequate|ok", text):
        return 1.0
    if re.search(r"出色|优秀|很好|excellent|strong", text):
        return 2.0
    return None


def normalize_painpoint_relevance(value: Any) -> str | None:
    """痛点相关性归一；缺失保持 unknown，不在 severity 层转成 false。"""
    text = str(value or "").strip().lower()
    return text if text in {"benchmark_only", "creator_only", "both", "none"} else None


def normalize_stage_standard_delivery(value: Any) -> str | None:
    """到位标准达成归一（全阶段统一，泛化自 proposition_delivery）：四值枚举；
    该阶段双方是否有效达到本阶段的『本品到位标准』（锚点按阶段查，见 prompt 对照表）。
    先作为事实收集，暂不参与 derive 卡分；缺失/不合法返回 None。"""
    text = str(value or "").strip().lower()
    return text if text in {"benchmark_only", "creator_only", "both", "none"} else None


def normalize_bool_flag(value: Any) -> bool:
    """把模型可能输出的 true/"yes"/1/"是" 等统一成 bool。"""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "yes", "1", "是", "有"}


def normalize_product_coverage(value: Any) -> str:
    return normalize_choice(value, {"none", "low", "medium", "high"}, "none")


def normalize_module_id(value: Any, index: int) -> str:
    return canonical_module_id(value, index)


def normalize_voice_performance(value: Any) -> dict[str, Any]:
    item = value if isinstance(value, dict) else {}
    return {
        "pace": str(item.get("pace") or "未评估").strip(),
        "energy": str(item.get("energy") or "未评估").strip(),
        "key_pause": bool(item.get("key_pause", False)),
        "note": str(item.get("note") or "未提供口播表现判断。").strip(),
    }


def normalize_holistic_assessment(value: Any) -> dict[str, str]:
    item = value if isinstance(value, dict) else {}
    keys = (
        "structure_integrity",
        "selling_point_efficiency",
        "audience_resonance",
        "pace_and_emotion",
        "trust_and_purchase_impulse",
        "conversion_prediction",
    )
    return {key: str(item.get(key) or "未完成评估。").strip() for key in keys}


def normalize_product_visibility(value: Any) -> dict[str, Any]:
    item = value if isinstance(value, dict) else {}
    return {
        "first_appearance_sec": item.get("first_appearance_sec"),
        "total_screen_time_sec": item.get("total_screen_time_sec"),
        "video_duration_sec": item.get("video_duration_sec"),
        "ratio": item.get("ratio"),
        "estimation_note": str(item.get("estimation_note") or "未提供统计依据。").strip(),
    }


def normalize_loop_closure(value: Any) -> dict[str, Any]:
    item = value if isinstance(value, dict) else {}
    return {
        "pain_resolved_in_s4": bool(item.get("pain_resolved_in_s4", False)),
        "benefit_delivered_in_s6": bool(item.get("benefit_delivered_in_s6", False)),
        "suspense_revealed": bool(item.get("suspense_revealed", False)),
        "suspense_reveal_time": item.get("suspense_reveal_time"),
        "note": str(item.get("note") or "未完成闭环校验。").strip(),
    }


def normalize_s3_s4_relationship(value: Any) -> dict[str, Any]:
    item = value if isinstance(value, dict) else {}
    return {
        "creator_relationship": normalize_s3_s4_relationship_type(item.get("creator_relationship")),
        "benchmark_relationship": normalize_s3_s4_relationship_type(item.get("benchmark_relationship")),
        "creator_reason": str(item.get("creator_reason") or "未完成 S3/S4 关系审计。").strip(),
        "benchmark_reason": str(item.get("benchmark_reason") or "未完成 S3/S4 关系审计。").strip(),
    }


def normalize_promise_chain(value: Any) -> dict[str, Any]:
    item = value if isinstance(value, dict) else {}
    return {
        "s1_promise": str(item.get("s1_promise") or "未完成 S1 承诺审计。").strip(),
        "s2_answer": str(item.get("s2_answer") or "未完成 S2 接应审计。").strip(),
        "s3_proof_target": str(item.get("s3_proof_target") or "未完成 S3 证明目标审计。").strip(),
        "s4_outcome": str(item.get("s4_outcome") or "未完成 S4 结果兑现审计。").strip(),
        "chain_closed": normalize_bool_flag(item.get("chain_closed")),
        "broken_at": normalize_promise_break_point(item.get("broken_at")),
        "break_reason": str(item.get("break_reason") or "未完成 S1-S4 承诺闭环审计。").strip(),
    }


def normalize_temporal_evidence_mode(value: Any) -> str:
    mode = str(value or "unknown").strip().lower()
    return mode if mode in {"full_temporal", "focused_temporal", "static_only", "unknown"} else "unknown"


def normalize_ratio(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def normalize_ratio_map(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key).strip(): normalize_ratio(ratio)
        for key, ratio in value.items()
        if str(key).strip()
    }


def normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def normalize_selling_point_observations(value: Any, valid_ids: set[str]) -> list[dict[str, Any]]:
    observations = value if isinstance(value, list) else []
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(observations[:6], start=1):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        normalized.append(
            {
                "id": str(item.get("id") or f"SP{index}").strip(),
                "candidate_id": str(item.get("candidate_id") or "").strip(),
                "text": text,
                "visual_share": normalize_ratio(item.get("visual_share")),
                "speech_share": normalize_ratio(item.get("speech_share")),
                "proof_mode_observed": str(item.get("proof_mode_observed") or "unknown").strip(),
                "proof_signal_present": normalize_bool_flag(item.get("proof_signal_present")),
                "evidence_ids": [
                    str(evidence_id).strip()
                    for evidence_id in item.get("evidence_ids") or []
                    if str(evidence_id).strip() in valid_ids
                ],
            }
        )
    return normalized


def normalize_variant_decision_rule(value: Any, valid_ids: set[str]) -> dict[str, Any]:
    item = value if isinstance(value, dict) else {}
    return {
        "speech_explains_choice": normalize_bool_flag(item.get("speech_explains_choice")),
        "visual_comparison_present": normalize_bool_flag(item.get("visual_comparison_present")),
        "reason": str(item.get("reason") or "").strip(),
        "evidence_ids": [
            str(evidence_id).strip()
            for evidence_id in item.get("evidence_ids") or []
            if str(evidence_id).strip() in valid_ids
        ],
    }


def normalize_attention_competitors(value: Any, valid_ids: set[str]) -> list[dict[str, Any]]:
    competitors = value if isinstance(value, list) else []
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(competitors[:6], start=1):
        if not isinstance(item, dict):
            continue
        label = str(item.get("object_label") or "").strip()
        if not label:
            continue
        normalized.append(
            {
                "id": str(item.get("id") or f"AC{index}").strip(),
                "object_label": label,
                "time_ranges": normalize_string_list(item.get("time_ranges")),
                "persistent_motion": normalize_bool_flag(item.get("persistent_motion")),
                "high_salience": normalize_bool_flag(item.get("high_salience")),
                "participates_in_product_task": normalize_bool_flag(item.get("participates_in_product_task")),
                "occludes_proof_area": normalize_bool_flag(item.get("occludes_proof_area")),
                "evidence_ids": [
                    str(evidence_id).strip()
                    for evidence_id in item.get("evidence_ids") or []
                    if str(evidence_id).strip() in valid_ids
                ],
            }
        )
    return normalized


def normalize_attention_scan_audit(value: Any, valid_ids: set[str]) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    return {
        "recording_equipment_visible": normalize_bool_flag(source.get("recording_equipment_visible")),
        "foreground_non_task_object_visible": normalize_bool_flag(source.get("foreground_non_task_object_visible")),
        "notes": str(source.get("notes") or "").strip(),
        "evidence_ids": [
            str(evidence_id).strip()
            for evidence_id in source.get("evidence_ids") or []
            if str(evidence_id).strip() in valid_ids
        ],
    }


def normalize_variant_unit_fields(unit: dict[str, Any]) -> dict[str, Any]:
    variant_ids = normalize_string_list(unit.get("variant_ids"))
    raw_visual = unit.get("variant_visual_shares")
    raw_speech = unit.get("variant_speech_shares")
    visual_shares = normalize_ratio_map(raw_visual)
    speech_shares = normalize_ratio_map(raw_speech)
    allowed = set(variant_ids)
    keys_valid = set(visual_shares).issubset(allowed) and set(speech_shares).issubset(allowed)
    values_valid = all(
        isinstance(value, (int, float)) and 0 <= float(value) <= 1
        for source in (raw_visual, raw_speech)
        if isinstance(source, dict)
        for value in source.values()
    )
    totals_valid = sum(visual_shares.values()) <= 1.05 and sum(speech_shares.values()) <= 1.05
    relation_mode = str(unit.get("variant_relation_mode") or "none").strip().lower()
    if relation_mode not in {"single_focus", "explicit_comparison", "sequence", "ambiguous", "none"}:
        relation_mode = "ambiguous"
    comparison_purpose_explicit = normalize_bool_flag(unit.get("comparison_purpose_explicit"))
    required_fields_present = all(
        key in unit
        for key in (
            "variant_ids", "variant_visual_shares", "variant_speech_shares",
            "variant_relation_mode", "comparison_purpose_explicit",
        )
    )
    has_variant_observation = bool(variant_ids) and bool(visual_shares or speech_shares)
    shape_valid = (
        (not variant_ids and relation_mode == "none" and not visual_shares and not speech_shares)
        or (relation_mode == "single_focus" and has_variant_observation and bool(visual_shares))
        # 比较和顺序可以跨相邻 evidence unit 完成；当前单元只出现一个变体仍是合法观察。
        or (relation_mode == "explicit_comparison" and has_variant_observation and comparison_purpose_explicit is True)
        or (relation_mode in {"sequence", "ambiguous", "none"} and has_variant_observation)
    )
    variant_data_valid = required_fields_present and keys_valid and values_valid and totals_valid and shape_valid
    primary_variant_id = ""
    confident = False
    if variant_data_valid and relation_mode == "single_focus" and visual_shares:
        candidate_id, share = max(visual_shares.items(), key=lambda item: item[1])
        if share >= 0.70:
            primary_variant_id = candidate_id
            confident = True
    elif variant_data_valid and relation_mode == "explicit_comparison" and comparison_purpose_explicit is True:
        confident = True
    return {
        "variant_ids": variant_ids,
        "variant_visual_shares": visual_shares,
        "variant_speech_shares": speech_shares,
        "variant_relation_mode": relation_mode,
        "comparison_purpose_explicit": comparison_purpose_explicit,
        "primary_variant_id": primary_variant_id,
        "variant_attribution_confident": confident,
        "variant_data_valid": variant_data_valid,
        "attention_competitor_ids": normalize_string_list(unit.get("attention_competitor_ids")),
    }


def normalize_video_understanding(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    normalized: dict[str, Any] = {}
    for role in ("benchmark", "creator"):
        item = source.get(role) if isinstance(source.get(role), dict) else {}
        units = item.get("evidence_units") if isinstance(item.get("evidence_units"), list) else []
        normalized_units = []
        for index, unit in enumerate(units, start=1):
            if not isinstance(unit, dict):
                continue
            normalized_unit = {
                "id": str(unit.get("id") or f"{role[0].upper()}{index}").strip(),
                "time_range": str(unit.get("time_range") or "").strip(),
                "information": str(unit.get("information") or "").strip(),
                "voiceover": str(unit.get("voiceover") or "").strip(),
                "voiceover_zh": str(unit.get("voiceover_zh") or "").strip(),
                "visual_fact": str(unit.get("visual_fact") or "").strip(),
                "subtitle_fact": str(unit.get("subtitle_fact") or "").strip(),
                "audio_fact": str(unit.get("audio_fact") or "").strip(),
                "evidence_strength": normalize_evidence_strength(unit.get("evidence_strength")),
                "product_visible": normalize_bool_flag(unit.get("product_visible")),
                "product_coverage": normalize_product_coverage(unit.get("product_coverage")),
                # F 项背书劈成两个纯观察信道（替代焊死判断的 third_party_endorsement）：
                "endorsement_verbal": normalize_demo_flag(unit.get("endorsement_verbal")),
                "endorsement_visual": normalize_demo_flag(unit.get("endorsement_visual")),
                "trust_source_signals": normalize_s5_source_signals(unit.get("trust_source_signals")),
                "trust_source_reference": str(unit.get("trust_source_reference") or "").strip(),
                "trust_source_status": normalize_s5_source_status(unit),
                "functions": normalize_functions(unit.get("functions")),
            }
            normalized_unit.update(normalize_variant_unit_fields(unit))
            normalized_units.append(normalized_unit)
        normalized_units = normalized_units[:20]
        valid_ids = {unit["id"] for unit in normalized_units}
        raw_gate_status = item.get("gate_observation_status") if isinstance(item.get("gate_observation_status"), dict) else {}
        normalized[role] = {
            "content_summary": str(item.get("content_summary") or "").strip(),
            "communication_strategy": str(item.get("communication_strategy") or "").strip(),
            "temporal_evidence_mode": normalize_temporal_evidence_mode(item.get("temporal_evidence_mode")),
            "selling_point_observations": normalize_selling_point_observations(item.get("selling_point_observations"), valid_ids),
            "variant_decision_rule": normalize_variant_decision_rule(item.get("variant_decision_rule"), valid_ids),
            "attention_scan_audit": normalize_attention_scan_audit(item.get("attention_scan_audit"), valid_ids),
            "attention_competitors": normalize_attention_competitors(item.get("attention_competitors"), valid_ids),
            "gate_observation_status": normalize_gate_observation_status(raw_gate_status, item, normalized_units),
            "evidence_units": normalized_units,
            # Stage1 锁定 facts 的审计字段必须进入最终产物；不得由 Stage2 回显内容覆盖。
            "evidence_checklist": normalize_fact_evidence_checklist(item.get("evidence_checklist"), valid_ids),
            "structure_event_checks": normalize_structure_event_checks(item.get("structure_event_checks"), valid_ids),
        }
    return normalized


def normalize_severity(value: Any) -> str:
    severity = str(value or "medium").strip().lower()
    # LLM 有时输出 high/low 而非 large/small，做兼容映射
    alias = {"high": "large", "low": "small", "big": "large", "minor": "small"}
    severity = alias.get(severity, severity)
    if severity not in {"large", "medium", "small"}:
        return "medium"
    return severity


def normalize_time_range_value(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        parsed = parse_time_range_seconds(value, None)
        if parsed is not None:
            start, end = parsed
            return f"{format_seconds(start)} - {format_seconds(end)}"
        return ""
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = parse_time_range_seconds(raw, None)
    if parsed is not None:
        start, end = parsed
        return f"{format_seconds(start)} - {format_seconds(end)}"
    # 结尾相对表达式需要视频时长才能解析；只保留严格形态，其他文本清空。
    if re.fullmatch(
        r"(?:最后|末尾|结尾|CTA)\s*\d+(?:\.\d+)?\s*(?:秒|s)?"
        r"(?:\s*(?:[-~至到])\s*\d+(?:\.\d+)?\s*(?:秒|s)?)?",
        raw,
        flags=re.IGNORECASE,
    ):
        return raw
    return ""


def normalize_priority(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


# ---------------------------------------------------------------------------
# 顶层 schema 归一化
# ---------------------------------------------------------------------------

def adapt_misnested_analysis_result(result: dict[str, Any]) -> dict[str, Any]:
    """处理 LLM 偶发的字段嵌套错位，在严格校验前先做兜底重排。"""
    adapted = dict(result)
    if "stage_analysis" not in adapted and isinstance(adapted.get("product_visibility"), list):
        adapted["stage_analysis"] = adapted["product_visibility"]
        adapted["product_visibility"] = {
            "first_appearance_sec": 0.0,
            "total_screen_time_sec": 0.0,
            "video_duration_sec": 0.0,
            "ratio": 0.0,
            "estimation_note": "模型将阶段数组误写入产品可见度字段；此处需人工结合报告帧复核。",
        }
    # 通用修复：product_visibility 缺 first_appearance_sec 时视为字段错位/缺失
    # （观察到 LLM 会写成 stage-keyed dict、evidence_units 列表、或完全省略）
    pv = adapted.get("product_visibility")
    if not isinstance(pv, dict) or "first_appearance_sec" not in pv:
        if pv is not None:
            adapted["misplaced_product_visibility"] = pv
        adapted["product_visibility"] = {
            "first_appearance_sec": 0.0,
            "total_screen_time_sec": 0.0,
            "video_duration_sec": 0.0,
            "ratio": 0.0,
            "estimation_note": "LLM 未输出可识别的 product_visibility 字段（first_appearance_sec 等），需人工复核。原数据保留在 misplaced_product_visibility。",
        }
    if isinstance(adapted.get("holistic_assessment"), str):
        text = adapted["holistic_assessment"]
        adapted["holistic_assessment"] = {
            "structure_integrity": text,
            "selling_point_efficiency": text,
            "audience_resonance": text,
            "pace_and_emotion": text,
            "trust_and_purchase_impulse": text,
            "conversion_prediction": text,
        }
    if not adapted.get("one_line_summary"):
        adapted["one_line_summary"] = adapted.get("executive_summary") or adapted.get("one_line_verdict") or "基于视频证据完成结构对比。"
    if not adapted.get("executive_summary"):
        adapted["executive_summary"] = adapted.get("one_line_summary")
    if not adapted.get("loop_closure"):
        adapted["loop_closure"] = {
            "pain_resolved_in_s4": False,
            "benefit_delivered_in_s6": False,
            "suspense_revealed": False,
            "suspense_reveal_time": None,
            "note": "模型未单独输出闭环字段，需结合阶段证据复核。",
        }
    return adapted


def normalize_analysis_result(result: dict[str, Any]) -> dict[str, Any]:
    """把 LLM dict 归一为 schema 规范结构；缺字段或阶段数不对会抛 SystemExit。"""
    result = adapt_misnested_analysis_result(result)
    try:
        result = validate_raw_analysis_envelope(result)
    except AnalysisContractError as exc:
        raise SystemExit(str(exc)) from exc
    stage_analysis = result["stage_analysis"]
    improvements = result["improvements"]
    executive_summary = str(result.get("one_line_summary") or result.get("executive_summary") or "").strip()

    normalized_stages = []
    for index, item in enumerate(stage_analysis):
        if not isinstance(item, dict):
            raise SystemExit("Each stage_analysis item must be an object.")
        stage_name, _default_range, core_question = STAGES[index]
        # Stage-specific ranges are evidence, not presentation defaults. Missing
        # or malformed ranges remain empty and are rejected by time coherence
        # validation instead of being silently assigned a catalog window.
        benchmark_time_range = normalize_time_range_value(item.get("benchmark_time_range"))
        creator_time_range = normalize_time_range_value(item.get("creator_time_range"))
        normalized_stages.append(
            {
                "stage": str(item.get("stage") or stage_name),
                "time_range": str(item.get("time_range") or f"标杆 {benchmark_time_range} / 达人 {creator_time_range}"),
                "benchmark_time_range": benchmark_time_range,
                "creator_time_range": creator_time_range,
                "core_question": str(item.get("core_question") or core_question),
                "creator_module_id": normalize_module_id(item.get("creator_module_id"), index + 1),
                "benchmark_module_id": normalize_module_id(item.get("benchmark_module_id"), index + 1),
                "module_fit": normalize_choice(item.get("module_fit"), {"fit", "degraded", "unfit", "unknown"}, "unknown"),
                "module_fit_reason": str(item.get("module_fit_reason") or "").strip(),
                "task_completion": normalize_task_completion(item.get("task_completion")),
                "gap_type": normalize_choice(item.get("gap_type"), {"structural", "execution", "resource"}, "structural"),
                "gap_summary": normalize_evidence(item.get("gap_summary")),
                "voice_performance": normalize_voice_performance(item.get("voice_performance")),
                "benchmark_summary": required_text(item, "benchmark_summary"),
                "benchmark_key_message": str(item.get("benchmark_key_message") or item.get("benchmark_summary") or "").strip(),
                "benchmark_evidence_ids": normalize_evidence(item.get("benchmark_evidence_ids")),
                "benchmark_visual_evidence": normalize_evidence(item.get("benchmark_visual_evidence")),
                "benchmark_support_status": normalize_support_status(item.get("benchmark_support_status"), item.get("benchmark_quote")),
                # S4 效果呈现布尔（结构库 S4-A~F 判定）：缺失为 None，不触发确定性约束。
                "benchmark_has_effect_demo": normalize_demo_flag(item.get("benchmark_has_effect_demo")),
                # S3 使用过程布尔（结构库 S3-A~E 判定）：缺失为 None → derive S3 放大器不触发（保留旧空白行为）
                "benchmark_has_usage_demo": normalize_demo_flag(item.get("benchmark_has_usage_demo")),
                "benchmark_quote": str(item.get("benchmark_quote") or "").strip(),
                "benchmark_quote_zh": str(item.get("benchmark_quote_zh") or "").strip(),
                "creator_summary": required_text(item, "creator_summary"),
                "creator_key_message": str(item.get("creator_key_message") or item.get("creator_summary") or "").strip(),
                "creator_evidence_ids": normalize_evidence(item.get("creator_evidence_ids")),
                "creator_visual_evidence": normalize_evidence(item.get("creator_visual_evidence")),
                "creator_support_status": normalize_support_status(item.get("creator_support_status"), item.get("creator_quote")),
                "creator_has_effect_demo": normalize_demo_flag(item.get("creator_has_effect_demo")),
                "creator_has_usage_demo": normalize_demo_flag(item.get("creator_has_usage_demo")),
                "creator_quote": str(item.get("creator_quote") or "").strip(),
                "creator_quote_zh": str(item.get("creator_quote_zh") or "").strip(),
                "gap": required_text(item, "gap"),
                "evidence": normalize_evidence(item.get("evidence")),
                "severity": normalize_severity(item.get("severity")),
                # 模型直判快照（归一时定格）：severity 后续会被 stabilize/derive 改写，
                # 校准对照口径必须用这份，不能用链上任何一步之后的值（code review #5）
                "model_severity": normalize_severity(item.get("severity")),
                # 4d：两侧独立执行分（0/0.5/1/2），缺失为 None → derive 优雅跳过
                "creator_execution": normalize_execution_score(item.get("creator_execution")),
                "benchmark_execution": normalize_execution_score(item.get("benchmark_execution")),
                # 痛点相关性是商业优先级事实，缺失为 None，不进入 severity resolver。
                "painpoint_relevance": normalize_painpoint_relevance(item.get("painpoint_relevance")),
                # 到位标准达成事实（全阶段统一，见 prompt 对照表；先收集，暂不卡分）
                "stage_standard_delivery": normalize_stage_standard_delivery(item.get("stage_standard_delivery")),
                # 阶段级跨模态综合：保留各渠道事实，并显式记录主导渠道、交互关系和净效果。
                # 缺失时为 None，旧结果继续走既有 stage flags，保证历史兼容。
                "creator_multimodal": normalize_multimodal_assessment(item.get("creator_multimodal")),
                "benchmark_multimodal": normalize_multimodal_assessment(item.get("benchmark_multimodal")),
                # S1 Hook 结构化 flag（仅 S1 有意义）：四维 dims 推执行分、exists 红线、anchors 命题锚。
                # 缺失为 None → derive 回退模型执行分（优雅降级）。模型在 Stage2 产出（切片 B 接线）。
                "creator_hook": normalize_hook_flags(item.get("creator_hook")),
                "benchmark_hook": normalize_hook_flags(item.get("benchmark_hook")),
                # S2 产品引出契约 flag：只判 S1→S2 是否自然交接、产品身份/角色是否明确。
                # 不做四维执行分，缺失为 None → derive 保守降级。
                "creator_s2": normalize_s2_flags(item.get("creator_s2")),
                "benchmark_s2": normalize_s2_flags(item.get("benchmark_s2")),
                # S3 使用过程 flag：只判真实使用中核心卖点是否被动作演示出来。
                # 缺失为 None → derive 保守降级。
                "creator_s3": normalize_s3_flags(item.get("creator_s3")),
                "benchmark_s3": normalize_s3_flags(item.get("benchmark_s3")),
                # S4 效果因果 flag：只约束"效果是否可信地由产品造成"，缺失则保留旧 S4 判断。
                "creator_s4": normalize_s4_flags(item.get("creator_s4")),
                "benchmark_s4": normalize_s4_flags(item.get("benchmark_s4")),
                # S5 信任放大 flag：只判信任材料是否可见、可信、与本品相关；缺失则保留旧 S5 判断。
                "creator_s5": normalize_s5_flags(item.get("creator_s5")),
                "benchmark_s5": normalize_s5_flags(item.get("benchmark_s5")),
                # S6 CTA flag：只判购买指令、路径、利益/紧迫/保障是否成立；缺失则保留旧 S6 判断。
                "creator_s6": normalize_s6_flags(item.get("creator_s6")),
                "benchmark_s6": normalize_s6_flags(item.get("benchmark_s6")),
            }
        )

    normalized_improvements = []
    for index, item in enumerate(improvements, start=1):
        if not isinstance(item, dict):
            raise SystemExit("Each improvement item must be an object.")
        creator_time_range = str(item.get("creator_time_range") or item.get("time_range") or "").strip()
        benchmark_time_range = str(item.get("benchmark_time_range") or item.get("time_range") or "").strip()
        normalized_improvements.append(
            {
                "title": required_text(item, "title"),
                "target_stage": str(item.get("target_stage") or "").strip(),
                "gmv_impact": str(item.get("gmv_impact") or "").strip(),
                "gap_type": normalize_choice(item.get("gap_type"), {"structural", "execution", "resource"}, "structural"),
                "time_range": required_text(item, "time_range"),
                "creator_time_range": creator_time_range or required_text(item, "time_range"),
                "benchmark_time_range": benchmark_time_range or required_text(item, "time_range"),
                "problem": required_text(item, "problem"),
                "benchmark_reference": required_text(item, "benchmark_reference"),
                "benchmark_evidence_ids": normalize_evidence(item.get("benchmark_evidence_ids")),
                "suggestion": required_text(item, "suggestion"),
                "actions": normalize_evidence(item.get("actions")) or normalize_evidence(item.get("suggestion")),
                "gmv_reason": required_text(item, "gmv_reason"),
                "evidence": normalize_evidence(item.get("evidence")),
                "creator_script": str(item.get("creator_script") or "").strip(),
                "creator_script_zh": str(item.get("creator_script_zh") or "").strip(),
                "base_frame_suitability": normalize_base_frame_suitability(item.get("base_frame_suitability"), item.get("best_base_frame_time")),
                "best_base_frame_time": normalized_base_frame_time(item),
                "base_frame_evidence_id": str(item.get("base_frame_evidence_id") or "").strip(),
                "base_frame_reason": str(item.get("base_frame_reason") or "").strip(),
                "expected_effect": str(item.get("expected_effect") or item.get("gmv_reason") or "").strip(),
                "priority": normalize_priority(item.get("priority"), index),
            }
        )

    # key_conclusions：消费者视角关键结论（1-5 条）
    raw_conclusions = result.get("key_conclusions")
    key_conclusions: list[str] = []
    if isinstance(raw_conclusions, list):
        for item in raw_conclusions:
            text = str(item).strip()
            if text:
                key_conclusions.append(text)
        key_conclusions = key_conclusions[:5]

    return {
        "one_line_verdict": str(result.get("one_line_verdict") or "").strip(),
        "one_line_summary": executive_summary,
        "executive_summary": executive_summary,
        "holistic_assessment": normalize_holistic_assessment(result.get("holistic_assessment")),
        "key_conclusions": key_conclusions,
        "comparison_contract": normalize_comparison_contract(
            result.get("comparison_contract") or result.get("comparison_eligibility")
        ),
        "comparison_eligibility": normalize_comparison_eligibility(
            result.get("comparison_contract") or result.get("comparison_eligibility")
        ),
        "product_visibility": normalize_product_visibility(result.get("product_visibility")),
        "category_profile": normalize_category_profile(result.get("category_profile")),
        "product_profile": normalize_product_profile(result.get("product_profile")),
        "loop_closure": normalize_loop_closure(result.get("loop_closure")),
        "s3_s4_relationship": normalize_s3_s4_relationship(result.get("s3_s4_relationship")),
        "promise_chain": normalize_promise_chain(result.get("promise_chain")),
        "video_understanding": normalize_video_understanding(result.get("video_understanding")),
        "stage_analysis": normalized_stages,
        "improvements": normalized_improvements,
    }


# ---------------------------------------------------------------------------
# 单视频事实抽取归一化（fact extraction 模式专用）
# ---------------------------------------------------------------------------

_FUNCTION_ENUM = {"S1_hook", "S2_intro", "S3_usage", "S4_effect", "S5_trust", "S6_cta"}
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
        "variant_attributes": normalize_evidence(value.get("variant_attributes")),
        "core_job": str(value.get("core_job") or "").strip(),
        "target_object": str(value.get("target_object") or "").strip(),
        "use_mechanism": str(value.get("use_mechanism") or "").strip(),
        "desired_outcome": str(value.get("desired_outcome") or "").strip(),
        "identity_basis": normalize_choice(value.get("identity_basis"), _IDENTITY_BASIS, "unknown"),
        "confidence": normalize_choice(value.get("confidence"), _IDENTITY_CONFIDENCE, "low"),
    }


def normalize_comparison_contract(value: Any) -> dict[str, Any]:
    """归一商品关系与阶段级可比合同，并由合同派生旧 scope 兼容视图。"""
    value = value if isinstance(value, dict) else {}
    legacy_scope = normalize_choice(value.get("scope"), _COMPARISON_SCOPES, "uncertain")
    identity_relation = normalize_choice(value.get("identity_relation"), _IDENTITY_RELATIONS, "uncertain")
    substitution_relation = normalize_choice(value.get("substitution_relation"), _SUBSTITUTION_RELATIONS, "uncertain")

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
        "same_consumer_job": normalize_bool_flag(shared.get("same_consumer_job")),
        "same_target_object": normalize_bool_flag(shared.get("same_target_object")),
        "same_desired_outcome": normalize_bool_flag(shared.get("same_desired_outcome")),
        "same_purchase_decision": normalize_bool_flag(shared.get("same_purchase_decision")),
        "complement_or_dependency": normalize_bool_flag(shared.get("complement_or_dependency")),
        "reason": str(shared.get("reason") or "").strip(),
        "evidence_ids": normalize_evidence(shared.get("evidence_ids")),
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
        status = normalize_choice(raw.get("status"), _STAGE_COMPARISON_STATUSES, "not_comparable")
        if not raw and stage in legacy_stages:
            status = "direct" if identity_relation in {"exact_product", "same_product_family"} else "structural"
        if identity_relation in {"exact_product", "same_product_family"}:
            status = "direct"
        elif identity_relation == "uncertain" or substitution_relation in {"none", "uncertain"}:
            status = "not_comparable"
        elif status == "direct":
            status = "structural"
        stage_eligibility[stage] = {
            "status": status,
            "basis": str(raw.get("basis") or "").strip(),
            "shared_contract": str(raw.get("shared_contract") or "").strip(),
            "restrictions": normalize_evidence(raw.get("restrictions")),
            "evidence_ids": normalize_evidence(raw.get("evidence_ids")),
        }

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
        "scope_origin": normalize_choice(value.get("scope_origin"), _COMPARISON_SCOPE_ORIGINS, "facts"),
        "facts_scope": normalize_choice(value.get("facts_scope"), _COMPARISON_SCOPES, "uncertain"),
        "facts_reason": str(value.get("facts_reason") or "").strip(),
        "evidence_ids": normalize_evidence(value.get("evidence_ids")),
        "confidence": normalize_choice(value.get("confidence"), _IDENTITY_CONFIDENCE, "low"),
    }


def normalize_comparison_eligibility(value: Any) -> dict[str, Any]:
    """旧字段兼容入口；返回同一份三层合同，scope 仅为派生视图。"""
    return normalize_comparison_contract(value)


def normalize_functions(value: Any) -> list[str] | None:
    """evidence_unit 的 functions 多选标记（这段画面支撑哪些带货功能，描述性非评价）。
    缺失/None/非 list/无合法值 → None（老 facts 无此字段，nullable 不强制回填）；
    过滤非法枚举、去重保序。"""
    if not isinstance(value, list):
        return None
    out: list[str] = []
    for v in value:
        token = str(v or "").strip()
        if token in _FUNCTION_ENUM and token not in out:
            out.append(token)
    return out or None


def normalize_fact_evidence_checklist(value: Any, valid_ids: set[str]) -> list[dict[str, Any]]:
    """归一品命题检查表，只保留当前锁定 evidence_units 的引用。"""
    checklist = value if isinstance(value, list) else []
    return [
        {
            "item": str(item.get("item") or "").strip(),
            "covered": normalize_bool_flag(item.get("covered")) is True,
            "evidence_ids": [
                str(evidence_id).strip()
                for evidence_id in item.get("evidence_ids") or []
                if str(evidence_id).strip() in valid_ids
            ],
            "channels": [
                str(channel).strip()
                for channel in item.get("channels") or []
                if str(channel).strip() in {"visual", "voiceover", "subtitle"}
            ],
        }
        for item in checklist
        if isinstance(item, dict) and str(item.get("item") or "").strip()
    ]


def normalize_structure_event_checks(value: Any, valid_ids: set[str]) -> list[dict[str, Any]]:
    """按结构库事件目录补齐 Stage1 观测，防止模型省略 false 项造成不可比。"""
    catalog = stage1_event_catalog()
    catalog_ids = {str(item["id"]) for item in catalog}
    raw_checks = value if isinstance(value, list) else []
    event_by_module = {
        str(item.get("module_id") or "").strip().upper(): item
        for item in raw_checks
        if isinstance(item, dict) and str(item.get("module_id") or "").strip().upper() in catalog_ids
    }
    normalized: list[dict[str, Any]] = []
    for catalog_item in catalog:
        module_id = str(catalog_item["id"])
        item = event_by_module.get(module_id)
        if not isinstance(item, dict):
            normalized.append({"module_id": module_id, "present": False, "evidence_ids": []})
            continue
        normalized.append(
            {
                "module_id": module_id,
                "present": normalize_bool_flag(item.get("present")) is True,
                "evidence_ids": [
                    str(evidence_id).strip()
                    for evidence_id in item.get("evidence_ids") or []
                    if str(evidence_id).strip() in valid_ids
                ],
            }
        )
    return normalized


def normalize_video_fact_result(role: str, result: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    code = "B" if role == "benchmark" else "C"
    units = result.get("evidence_units")
    if not isinstance(units, list) or not units:
        raise SystemExit(f"{role} fact extraction returned no evidence_units.")
    normalized = {
        "content_summary": str(result.get("content_summary") or "").strip(),
        "communication_strategy": str(result.get("communication_strategy") or "").strip(),
        "product_identity": normalize_video_product_identity(result.get("product_identity")),
        "temporal_evidence_mode": normalize_temporal_evidence_mode(result.get("temporal_evidence_mode")),
        "evidence_units": [],
    }
    for index, unit in enumerate(units[:8], start=1):
        if not isinstance(unit, dict):
            continue
        information = str(unit.get("information") or "").strip()
        if not information:
            # information 只作检索摘要；模型漏填时从同一锁定事实单元回填，不能因此丢掉完整感官证据。
            information = "；".join(
                text
                for text in (
                    str(unit.get("visual_fact") or "").strip(),
                    str(unit.get("voiceover_zh") or unit.get("voiceover") or "").strip(),
                    str(unit.get("subtitle_fact") or "").strip(),
                    str(unit.get("audio_fact") or "").strip(),
                )
                if text
            )
        normalized_unit = {
                "id": normalized_fact_id(unit.get("id"), code, index),
                "time_range": str(unit.get("time_range") or "").strip(),
                "information": information,
                "voiceover": str(unit.get("voiceover") or "").strip(),
                "voiceover_zh": str(unit.get("voiceover_zh") or "").strip(),
                "visual_fact": str(unit.get("visual_fact") or "").strip(),
                "subtitle_fact": str(unit.get("subtitle_fact") or "").strip(),
                "audio_fact": str(unit.get("audio_fact") or "").strip(),
                "evidence_strength": normalize_evidence_strength(unit.get("evidence_strength")),
                "product_visible": normalize_bool_flag(unit.get("product_visible")),
                "product_coverage": normalize_product_coverage(unit.get("product_coverage")),
                # F 项背书劈成两个纯观察信道（替代焊死判断的 third_party_endorsement）：
                "endorsement_verbal": normalize_demo_flag(unit.get("endorsement_verbal")),
                "endorsement_visual": normalize_demo_flag(unit.get("endorsement_visual")),
                "trust_source_signals": normalize_s5_source_signals(unit.get("trust_source_signals")),
                "trust_source_reference": str(unit.get("trust_source_reference") or "").strip(),
                "trust_source_status": normalize_s5_source_status(unit),
                # 这段支撑哪些带货功能（多选，描述性）；nullable，老 facts 缺失为 None
                "functions": normalize_functions(unit.get("functions")),
            }
        normalized_unit.update(normalize_variant_unit_fields(unit))
        normalized["evidence_units"].append(normalized_unit)
    validate_single_video_facts(role, normalized, analysis)
    valid_ids = {unit["id"] for unit in normalized["evidence_units"]}
    normalized["selling_point_observations"] = normalize_selling_point_observations(
        result.get("selling_point_observations"), valid_ids
    )
    normalized["variant_decision_rule"] = normalize_variant_decision_rule(result.get("variant_decision_rule"), valid_ids)
    normalized["attention_scan_audit"] = normalize_attention_scan_audit(result.get("attention_scan_audit"), valid_ids)
    normalized["attention_competitors"] = normalize_attention_competitors(result.get("attention_competitors"), valid_ids)
    raw_gate_status = result.get("gate_observation_status") if isinstance(result.get("gate_observation_status"), dict) else {}
    normalized["gate_observation_status"] = normalize_gate_observation_status(
        raw_gate_status, result, normalized["evidence_units"]
    )
    normalized["evidence_checklist"] = normalize_fact_evidence_checklist(result.get("evidence_checklist"), valid_ids)
    normalized["structure_event_checks"] = normalize_structure_event_checks(result.get("structure_event_checks"), valid_ids)
    return normalized


def normalize_gate_observation_status(
    value: dict[str, Any],
    raw_side: dict[str, Any],
    units: list[dict[str, Any]],
) -> dict[str, str]:
    """门控观察完整性不能由空数组推断；模型必须显式完成扫描且数据形状合法。"""
    selling_complete = (
        value.get("selling_point_route") == "complete"
        and isinstance(raw_side.get("selling_point_observations"), list)
        and bool(raw_side.get("selling_point_observations"))
    )
    observed_units = [
        unit for unit in units
        if "_NO_" not in str(unit.get("id") or "").upper()
    ]
    focus_complete = (
        value.get("variant_focus") == "complete"
        and isinstance(raw_side.get("variant_decision_rule"), dict)
        and bool(observed_units)
        and all(unit.get("variant_data_valid") is True for unit in observed_units)
    )
    attention_audit = raw_side.get("attention_scan_audit") if isinstance(raw_side.get("attention_scan_audit"), dict) else {}
    recording_checked = isinstance(attention_audit.get("recording_equipment_visible"), bool)
    foreground_checked = isinstance(attention_audit.get("foreground_non_task_object_visible"), bool)
    visible_competitor = (
        attention_audit.get("recording_equipment_visible") is True
        or attention_audit.get("foreground_non_task_object_visible") is True
    )
    attention_complete = (
        value.get("attention_scan") == "complete"
        and isinstance(raw_side.get("attention_competitors"), list)
        and recording_checked
        and foreground_checked
        and (not visible_competitor or bool(raw_side.get("attention_competitors")))
    )
    return {
        "selling_point_route": "complete" if selling_complete else "unknown",
        "variant_focus": "complete" if focus_complete else "unknown",
        "attention_scan": "complete" if attention_complete else "unknown",
    }


def normalized_fact_id(value: Any, code: str, index: int) -> str:
    text = str(value or "").strip().upper()
    return text if re.fullmatch(rf"{code}[A-Z0-9_]*\d*", text) else f"{code}{index}"


def validate_single_video_facts(role: str, facts: dict[str, Any], analysis: dict[str, Any]) -> None:
    """单视频 fact 校验：拒绝跨视频串证据；缺失 information 直接 fail。"""
    info = analysis.get("videos", {}).get(role, {})
    transcript = normalized_transcript_text(read_transcript_text(info))
    other_role = "creator" if role == "benchmark" else "benchmark"
    other_transcript = normalized_transcript_text(read_transcript_text(analysis.get("videos", {}).get(other_role, {})))
    for unit in facts.get("evidence_units", []):
        quote = str(unit.get("voiceover") or "").strip()
        normalized_quote = normalized_transcript_text(quote)
        if quote and len(normalized_quote) >= 12 and normalized_quote not in transcript:
            if normalized_quote in other_transcript:
                raise SystemExit(f"{role} fact {unit.get('id')} voiceover is from {other_role} transcript.")
            subtitle = str(unit.get("subtitle_fact") or "").strip()
            if quote not in subtitle:
                unit["subtitle_fact"] = f"{subtitle}；{quote}".strip("；")
            unit["voiceover"] = ""
            unit["voiceover_zh"] = ""
        if not str(unit.get("information") or "").strip():
            raise SystemExit(f"{role} fact {unit.get('id')} missing information.")


# ---------------------------------------------------------------------------
# 共享工具：被 parse 和 postprocess 都需要
# ---------------------------------------------------------------------------

def is_effective_voiceover(value: Any) -> bool:
    """判断字符串是否是有效口播（排除音乐占位、空字符串、显式无效声明）。"""
    text = str(value or "").strip().lower()
    if not text or text in {"*outro music*", "[music]", "(music)", "music", "（音乐渐弱）"}:
        return False
    if "无有效口播" in text or ("音乐" in text and "环境声" in text):
        return False
    return True


def read_transcript_text(info: dict[str, Any]) -> str:
    path = Path(str(info.get("transcript_path") or Path(str(info.get("work_dir") or "")) / "transcript.txt"))
    return path.read_text(encoding="utf-8", errors="ignore") if path.is_file() else ""


def normalized_transcript_text(value: str) -> str:
    """把转写文本归一为只包含字母数字的小写形式，用于跨视频字符串比对。"""
    return re.sub(r"[\W_]+", "", value.lower(), flags=re.UNICODE)
