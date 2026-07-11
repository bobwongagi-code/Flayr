"""flayr_core.llm.parse：JSON 解析 + schema 规范化。

职责：
  - 把 LLM 返回的原始文本 / 半结构 dict 解析为合法 JSON
  - 按 references/analysis-output-schema.json 把 dict 字段补齐、归一为 schema 规范结构
  - 提供阶段常量 STAGES、口播有效性判断 is_effective_voiceover 等基础工具
    （这些被 postprocess 也复用；放在 parse 是为了让依赖单向：postprocess → parse）

不依赖 postprocess 任何函数；不做业务规则修补；不引用业务规则关键词。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..artifacts import format_seconds


# 阶段固定列表：S1-S6，作为 stage_analysis 数组长度与顺序校验依据。
# 这里的"参考时间段"（如 "0~3s"）仅在 LLM 未给出真实 time_range 时作为兜底回填字符串，
# 不参与下游的帧选取计算——帧选取应以 LLM 输出的真实 benchmark_time_range / creator_time_range 为准。
STAGES = [
    ("S1 Hook", "0~3s", "用户凭什么停下来"),
    ("S2 产品引出", "3~6s", "产品为什么现在出现"),
    ("S3 使用过程", "6~15s", "用户能不能看懂怎么用"),
    ("S4 效果呈现", "15~23s", "用户能不能看见价值"),
    ("S5 信任放大", "23~27s", "用户凭什么相信"),
    ("S6 CTA", "最后 3~5s", "用户为什么现在下单"),
]


# ---------------------------------------------------------------------------
# JSON 文本解析
# ---------------------------------------------------------------------------

def parse_json_text(text: str) -> dict[str, Any]:
    """解析 LLM 返回的 JSON 文本，必要时做轻度修复。

    处理顺序：
      1. 去掉 ```json fence
      2. 去尾随逗号
      3. 第一次 json.loads
      4. 失败则尝试转义未配对的引号后再 loads
      5. 仍失败则 fail loud
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = remove_trailing_commas(cleaned)
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        repaired = remove_trailing_commas(escape_unquoted_string_quotes(cleaned))
        try:
            result = json.loads(repaired)
        except json.JSONDecodeError:
            raise SystemExit(f"LLM output is not valid JSON: {exc}") from exc
    if not isinstance(result, dict):
        raise SystemExit("LLM output JSON must be an object.")
    return result


def remove_trailing_commas(text: str) -> str:
    return re.sub(r",(\s*[}\]])", r"\1", text)


def escape_unquoted_string_quotes(text: str) -> str:
    """转义 LLM 常误产生的字符串内部未转义引号。"""
    repaired: list[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if not in_string:
            repaired.append(char)
            if char == '"':
                in_string = True
            continue
        if escaped:
            repaired.append(char)
            escaped = False
            continue
        if char == "\\":
            repaired.append(char)
            escaped = True
            continue
        if char != '"':
            repaired.append(char)
            continue

        remainder = text[index + 1 :]
        next_nonspace = next((item for item in remainder if not item.isspace()), "")
        if next_nonspace in {":", ",", "}", "]"}:
            repaired.append(char)
            in_string = False
        else:
            repaired.append('\\"')
    return "".join(repaired)


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


def normalize_support_status(value: Any, quote: Any) -> str:
    status = str(value or "").strip().lower()
    if status in {"supported", "voice_only", "visual_only", "conflict"}:
        return status
    return "voice_only" if str(quote or "").strip() else "visual_only"


def normalize_demo_flag(value: Any) -> bool | None:
    """演示布尔通用归一（S4 has_effect_demo / S3 has_usage_demo）。返回 True/False；
    非该阶段/null/缺失/无法解析→None（derive 见 None：S4 回退 _DEMO_RE 兜底，S3 不放大）。
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
_PROOF_MODES = {
    "instant_visual",
    "process_result",
    "sensory_proxy",
    "aesthetic_value",
    "social_reaction",
    "long_term_record",
    "trust_substituted",
    "low_decision_light_proof",
}
_EFFECT_REQUIRES_PROCESS = {"true", "false", "partial"}
_PROOF_SIGNAL_TYPES = {
    "state_change",
    "process_event",
    "sensory_response",
    "aesthetic_appeal",
    "social_response",
    "long_term_record",
    "trust_evidence",
    "light_proof",
}
_PROOF_PLAN_RANKS = {"low": 1, "medium": 2, "high": 3}
_PROOF_PLAN_STAGES = {"S2", "S3", "S4", "S5"}
_PROOF_PLAN_SOURCES = {"model_category_default", "operator_priority", "curated_priority"}
_DIRECT_VISUAL_PROOF_MODES = {"instant_visual", "process_result"}
_SIGNAL_TYPES_BY_MODE = {
    "instant_visual": {"state_change"},
    "process_result": {"state_change", "process_event"},
    "sensory_proxy": {"sensory_response"},
    "aesthetic_value": {"aesthetic_appeal"},
    "social_reaction": {"social_response"},
    "long_term_record": {"long_term_record"},
    "trust_substituted": {"trust_evidence"},
    "low_decision_light_proof": {"light_proof"},
}
_CAPTURE_CONDITION_RE = re.compile(r"(?:同一?光|同机位|同距离|同构图|特写|近景|微距|高清|拍摄|镜头|构图|光线)")
_OBSERVABLE_SIGNAL_RE = re.compile(
    r"(?:变化|差异|状态|反光|纹理|污渍|颜色|色泽|水珠|残留|覆盖|泡沫|体积|速度|反应|记录|认证|评论|评分|前后|vs|→|->)",
    re.I,
)
_COMPOUND_PRIMARY_RE = re.compile(r"[、，,；;+]|(?:与|及|和|且|并|同时)")
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


def normalize_proof_mode(value: Any) -> str:
    mode = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return mode if mode in _PROOF_MODES else "instant_visual"


def normalize_effect_requires_process(value: Any) -> str:
    token = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if token in {"yes", "1", "是"}:
        return "true"
    if token in {"no", "0", "否"}:
        return "false"
    return token if token in _EFFECT_REQUIRES_PROCESS else "partial"


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
    return {
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
    }


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
    }


def normalize_s3_flags(value: Any) -> dict[str, Any] | None:
    """归一 S3 使用过程 flag。缺失返回 None，derive/validate 按主链标记决定是否消费。"""
    if not isinstance(value, dict):
        return None
    usage_process_visible = normalize_demo_flag(value.get("usage_process_visible"))
    if usage_process_visible is None:
        usage_process_visible = normalize_demo_flag(value.get("real_usage_met"))
    process_framing_met = normalize_demo_flag(value.get("process_framing_met"))
    if process_framing_met is None:
        process_framing_met = True
    return {
        "exists": normalize_demo_flag(value.get("exists")),
        "module_type": normalize_s3_type(value.get("module_type")),
        "usage_process_visible": usage_process_visible,
        "result_only_without_process": normalize_demo_flag(value.get("result_only_without_process")),
        "mouth_only_or_static": normalize_demo_flag(value.get("mouth_only_or_static")),
        "real_usage_met": normalize_demo_flag(value.get("real_usage_met")),
        "core_selling_point_visible": normalize_demo_flag(value.get("core_selling_point_visible")),
        "process_framing_met": process_framing_met,
        "demonstrated_selling_points": normalize_evidence(value.get("demonstrated_selling_points")),
        "missing_selling_points": normalize_evidence(value.get("missing_selling_points")),
        "scene_mode": normalize_s3_scene_mode(value.get("scene_mode")),
        "usage_context_fit": normalize_demo_flag(value.get("usage_context_fit")),
        "continuity_met": normalize_demo_flag(value.get("continuity_met")),
        "richness_met": normalize_demo_flag(value.get("richness_met")),
        "single_scene_continuity_met": normalize_demo_flag(value.get("single_scene_continuity_met")),
        "single_scene_variation_met": normalize_demo_flag(value.get("single_scene_variation_met")),
        "multi_scene_logic_met": normalize_demo_flag(value.get("multi_scene_logic_met")),
        "multi_scene_transition_met": normalize_demo_flag(value.get("multi_scene_transition_met")),
        "multi_scene_role_adaptation_met": normalize_demo_flag(value.get("multi_scene_role_adaptation_met")),
        "role_design_met": normalize_demo_flag(value.get("role_design_met")),
        "role_interaction_met": normalize_demo_flag(value.get("role_interaction_met")),
        "presentation_overlays": normalize_presentation_overlays(value.get("presentation_overlays")),
        "fake_or_staged": normalize_demo_flag(value.get("fake_or_staged")),
        "start_seconds": normalize_hook_boundary_seconds(value.get("start_seconds")),
        "end_seconds": normalize_hook_boundary_seconds(value.get("end_seconds")),
        "usage_reason": str(value.get("usage_reason") or "").strip(),
        "evidence_ids": normalize_evidence(value.get("evidence_ids")),
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
    }


def normalize_s5_flags(value: Any) -> dict[str, Any] | None:
    """归一 S5 信任放大 flag。缺失返回 None，derive 回退旧执行分。"""
    if not isinstance(value, dict):
        return None
    return {
        "exists": normalize_demo_flag(value.get("exists")),
        "module_type": normalize_s5_type(value.get("module_type")),
        "trust_evidence_type": normalize_s5_trust_type(value.get("trust_evidence_type")),
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
        "offer_or_incentive_clear": normalize_demo_flag(value.get("offer_or_incentive_clear")),
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
    }


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
    """痛点命中归一（4d）：四值枚举；缺失/不合法返回 None → derive 退回词法匹配兜底。"""
    text = str(value or "").strip().lower()
    return text if text in {"benchmark_only", "creator_only", "both", "none"} else None


def normalize_stage_standard_delivery(value: Any) -> str | None:
    """到位标准达成归一（全阶段统一，泛化自 proposition_delivery）：四值枚举；
    该阶段双方是否有效达到本阶段的『本品到位标准』（锚点按阶段查，见 prompt 对照表）。
    先作为事实收集，暂不参与 derive 卡分；缺失/不合法返回 None。"""
    text = str(value or "").strip().lower()
    return text if text in {"benchmark_only", "creator_only", "both", "none"} else None


def normalize_category_profile(value: Any) -> dict[str, Any] | None:
    """品类画像归一（4d）：模型只报事实与世界知识，权重政策在代码（postprocess/derive.py）。"""
    if not isinstance(value, dict):
        return None
    painpoints = [str(p).strip() for p in value.get("painpoints") or [] if str(p).strip()][:10]
    return {
        "category_name": str(value.get("category_name") or "").strip(),
        "price_tier": normalize_choice(value.get("price_tier"), {"low", "mid", "high"}, "mid"),
        # 来源占位（model_fallback）：postprocess 若发现运营档位会改写为 operator 并填 price
        "price_tier_source": "model_fallback",
        "price": "",
        "decision_threshold": normalize_choice(value.get("decision_threshold"), {"impulse", "considered"}, "considered"),
        "drive_type": normalize_choice(value.get("drive_type"), {"emotional", "functional", "mixed"}, "functional"),
        "painpoints": painpoints,
    }


def normalize_product_profile(value: Any) -> dict[str, Any] | None:
    """产品商业 DNA 归一：判分前模型先立的"本品视觉命题"尺子。

    visual_proof_points.primary 是 S4 执行分主锚；core_visual_proposition 只做旧结果回退。
    模型只报产品事实 + 品类世界知识；后续运营/DNA 库可经 postprocess 覆盖（同 price_tier 降级链）。
    """
    if not isinstance(value, dict):
        return None
    multipliers = [str(m).strip() for m in value.get("trust_multipliers") or [] if str(m).strip()][:6]
    dimensions = [str(d).strip() for d in value.get("visual_diff_dimensions") or [] if str(d).strip()][:3]
    selling_points = [str(s).strip() for s in value.get("core_selling_points") or [] if str(s).strip()][:6]
    proof_plan = normalize_short_video_proof_plan(value.get("short_video_proof_plan"))
    proof_contract = normalize_proof_contract(value.get("proof_contract"))
    if proof_plan is not None and proof_contract is not None and proof_contract["valid"]:
        plan_error = validate_proof_contract_against_plan(proof_contract, proof_plan)
        if plan_error:
            proof_contract["valid"] = False
            proof_contract["validation_reason"] = plan_error
    visual_proof_points = normalize_visual_proof_points(value.get("visual_proof_points"))
    proof_mode = normalize_proof_mode(value.get("proof_mode"))
    if proof_contract is not None:
        proof_mode = proof_contract["mode"]
        if proof_contract["valid"] and proof_mode in _DIRECT_VISUAL_PROOF_MODES:
            visual_proof_points = _contract_primary_visual_proof(proof_contract, visual_proof_points)
        elif proof_contract["valid"]:
            # 非直接视觉证明不允许伪造 S4-A~F 的 before/after 主锚；其证据由对应模式消费。
            visual_proof_points = [point for point in visual_proof_points if point["priority"] != "primary"]
        else:
            # 合同未通过时不允许旧字段绕过门禁继续充当 S4 强主锚。
            visual_proof_points = []
    return {
        # 可视化分叉：no（香水/保健品等效果拍不出）时 S4 视觉审计失效，判断权重应转 S5/达人可信度
        "visualizable": normalize_choice(value.get("visualizable"), {"yes", "no"}, "yes"),
        "physical_task": str(value.get("physical_task") or "").strip(),
        # S1 钩子命题：本品最有拦截力的点（模型推，运营可经降级链覆盖）
        "hook_proposition": str(value.get("hook_proposition") or "").strip(),
        # S3 主轴：本品核心卖点（使用过程要演示传递的对象，模型推/运营可供给）
        "core_selling_points": selling_points,
        # S3 场景层：本品典型使用场景（卖点演示的舞台，判场景适配/丰富/连贯的基准）
        "usage_context": str(value.get("usage_context") or "").strip(),
        "core_visual_proposition": str(value.get("core_visual_proposition") or "").strip(),
        "visual_proof_points": visual_proof_points,
        # 产品可以有多个商业卖点。该计划只负责选出 S4 的单一可测视觉锚点，并记录其余卖点应在哪一阶段传递。
        "short_video_proof_plan": proof_plan,
        # proof_contract 是 Step-0 的权威产品证明合同；旧字段保留，只供历史结果降级。
        "proof_contract": proof_contract,
        # 证明模式：S4 如何证明价值。视觉即时效果只是其中一种，低价颜值品/长周期品/感官品要避免硬套 before-after。
        "proof_mode": proof_mode,
        "effect_requires_process": normalize_effect_requires_process(value.get("effect_requires_process")),
        # before/after 应变化的视觉维度（S4 核验对比只看这些；未来 CV 检测层的维度钩子）
        "visual_diff_dimensions": dimensions,
        "trust_multipliers": multipliers,
        "shooting_requirement": str(value.get("shooting_requirement") or "").strip(),
        # 来源占位（model_inferred）：postprocess 命中 DNA 库或运营供给时改写为 library/operator
        "dna_source": "model_inferred",
        "confidence": normalize_choice(value.get("confidence"), {"high", "low"}, "high"),
    }


def normalize_proof_contract(value: Any) -> dict[str, Any] | None:
    """归一 Step-0 产品证明合同，校验字段职责而不猜某个品的正确卖点。"""
    if not isinstance(value, dict):
        return None
    raw_mode = str(value.get("mode") or "").strip().lower().replace("-", "_").replace(" ", "_")
    mode = normalize_proof_mode(raw_mode)
    signal_type = str(value.get("signal_type") or "").strip().lower().replace("-", "_").replace(" ", "_")
    contract = {
        "anchor_candidate_id": str(value.get("anchor_candidate_id") or "").strip(),
        "mode": mode,
        "consumer_outcome": str(value.get("consumer_outcome") or "").strip(),
        "signal_type": signal_type,
        "observable_signal": str(value.get("observable_signal") or "").strip(),
        "before_state": str(value.get("before_state") or "").strip(),
        "after_state": str(value.get("after_state") or "").strip(),
        "proof_condition": str(value.get("proof_condition") or "").strip(),
        "valid": False,
        "validation_reason": "",
    }
    if raw_mode not in _PROOF_MODES:
        contract["validation_reason"] = "缺少或非法 proof_contract.mode"
        return contract
    if not contract["consumer_outcome"]:
        contract["validation_reason"] = "缺少 consumer_outcome"
        return contract
    if _COMPOUND_PRIMARY_RE.search(contract["consumer_outcome"]):
        contract["validation_reason"] = "consumer_outcome 必须只保留一个最终结果"
        return contract
    if signal_type not in _PROOF_SIGNAL_TYPES:
        contract["validation_reason"] = "signal_type 不在允许枚举中"
        return contract
    if signal_type not in _SIGNAL_TYPES_BY_MODE[mode]:
        contract["validation_reason"] = "proof_mode 与 signal_type 不匹配"
        return contract
    if not contract["observable_signal"]:
        contract["validation_reason"] = "缺少 observable_signal"
        return contract
    if (
        _CAPTURE_CONDITION_RE.search(contract["observable_signal"])
        and not _OBSERVABLE_SIGNAL_RE.search(contract["observable_signal"])
    ):
        contract["validation_reason"] = "observable_signal 只描述拍摄条件，不是可观察信号"
        return contract
    if _COMPOUND_PRIMARY_RE.search(contract["observable_signal"]):
        contract["validation_reason"] = "observable_signal 必须只保留一个可观察维度"
        return contract
    if not contract["proof_condition"]:
        contract["validation_reason"] = "缺少 proof_condition"
        return contract
    if mode in _DIRECT_VISUAL_PROOF_MODES:
        if not contract["before_state"] or not contract["after_state"]:
            contract["validation_reason"] = "直接视觉证明必须给出 before_state 与 after_state"
            return contract
        if contract["before_state"] == contract["after_state"]:
            contract["validation_reason"] = "before_state 与 after_state 不得相同"
            return contract
    contract["valid"] = True
    return contract


def normalize_short_video_proof_plan(value: Any) -> dict[str, Any] | None:
    """归一短视频证明计划。

    这个计划不替产品决定唯一商业卖点；它要求模型先列全候选，再按可视展示空间、功能中心性、理解成本
    选出一个 S4 测量锚点。代码仅验证计划内部排序、阶段归属和合同引用，不猜某个品该选什么。
    """
    if not isinstance(value, dict):
        return None
    candidates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    invalid_reason = ""
    for raw in value.get("candidates") or []:
        if not isinstance(raw, dict):
            invalid_reason = "candidates 含非法项"
            continue
        candidate_id = str(raw.get("id") or "").strip()
        selling_point = str(raw.get("selling_point") or "").strip()
        stage = str(raw.get("delivery_stage") or "").strip().upper()
        proof_mode = normalize_proof_mode(raw.get("proof_mode")) if stage == "S4" else ""
        candidate = {
            "id": candidate_id,
            "selling_point": selling_point,
            "visual_space": normalize_choice(raw.get("visual_space"), set(_PROOF_PLAN_RANKS), ""),
            "functional_centrality": normalize_choice(raw.get("functional_centrality"), set(_PROOF_PLAN_RANKS), ""),
            "comprehension_cost": normalize_choice(raw.get("comprehension_cost"), set(_PROOF_PLAN_RANKS), ""),
            "delivery_stage": stage,
            "proof_mode": proof_mode,
            "reason": str(raw.get("reason") or "").strip(),
        }
        if not candidate_id or candidate_id in seen_ids:
            invalid_reason = invalid_reason or "candidate id 缺失或重复"
        elif not selling_point:
            invalid_reason = invalid_reason or "candidate 缺少 selling_point"
        elif not all(candidate[key] in _PROOF_PLAN_RANKS for key in ("visual_space", "functional_centrality", "comprehension_cost")):
            invalid_reason = invalid_reason or "candidate 排序维度必须是 low|medium|high"
        elif stage not in _PROOF_PLAN_STAGES:
            invalid_reason = invalid_reason or "candidate delivery_stage 必须是 S2|S3|S4|S5"
        elif stage == "S4" and candidate["proof_mode"] not in _PROOF_MODES:
            invalid_reason = invalid_reason or "S4 candidate 缺少合法 proof_mode"
        else:
            candidates.append(candidate)
            seen_ids.add(candidate_id)
        if len(candidates) >= 6:
            break

    anchor_id = str(value.get("s4_anchor_candidate_id") or "").strip()
    source = normalize_choice(value.get("selection_source"), _PROOF_PLAN_SOURCES, "model_category_default")
    confidence = normalize_choice(value.get("anchor_confidence"), {"high", "low"}, "low")
    plan = {
        "candidates": candidates,
        "s4_anchor_candidate_id": anchor_id,
        "selection_source": source,
        "anchor_confidence": confidence,
        "valid": False,
        "validation_reason": invalid_reason,
    }
    if invalid_reason:
        return plan
    if not candidates:
        plan["validation_reason"] = "至少需要一个卖点 candidate"
        return plan
    s4_candidates = [candidate for candidate in candidates if candidate["delivery_stage"] == "S4"]
    if not s4_candidates:
        if anchor_id:
            plan["validation_reason"] = "无 S4 candidate 时不得指定 s4_anchor_candidate_id"
            return plan
        plan["valid"] = True
        return plan
    selected = next((candidate for candidate in s4_candidates if candidate["id"] == anchor_id), None)
    if selected is None:
        plan["validation_reason"] = "s4_anchor_candidate_id 必须指向一个 S4 candidate"
        return plan
    selected_rank = _proof_plan_rank(selected)
    if any(_proof_plan_rank(candidate) > selected_rank for candidate in s4_candidates):
        plan["validation_reason"] = "S4 anchor 未按可视展示空间、功能中心性、理解成本选择最高候选"
        return plan
    plan["valid"] = True
    return plan


def _proof_plan_rank(candidate: dict[str, Any]) -> tuple[int, int, int]:
    """S4 候选按视觉展示空间优先，其次产品主要功能，最后才看理解成本。"""
    return (
        _PROOF_PLAN_RANKS.get(str(candidate.get("visual_space") or ""), 0),
        _PROOF_PLAN_RANKS.get(str(candidate.get("functional_centrality") or ""), 0),
        -_PROOF_PLAN_RANKS.get(str(candidate.get("comprehension_cost") or ""), 0),
    )


def validate_proof_contract_against_plan(contract: dict[str, Any], plan: dict[str, Any]) -> str:
    """验证合同只消费短视频计划中已选定的 S4 锚点；旧结果没有计划时由调用方兼容。"""
    if plan.get("valid") is not True:
        return str(plan.get("validation_reason") or "short_video_proof_plan 无效")
    anchor_id = str(plan.get("s4_anchor_candidate_id") or "").strip()
    contract_anchor = str(contract.get("anchor_candidate_id") or "").strip()
    direct_visual = contract.get("mode") in _DIRECT_VISUAL_PROOF_MODES
    if not anchor_id:
        if direct_visual:
            return "直接视觉 proof_contract 必须对应 short_video_proof_plan 的 S4 anchor"
        return ""
    selected = next(
        (candidate for candidate in plan.get("candidates") or [] if candidate.get("id") == anchor_id),
        None,
    )
    if not selected:
        return "short_video_proof_plan 找不到 S4 anchor"
    if contract_anchor != anchor_id:
        return "proof_contract.anchor_candidate_id 必须等于 short_video_proof_plan.s4_anchor_candidate_id"
    if selected.get("proof_mode") != contract.get("mode"):
        return "proof_contract.mode 必须与 S4 anchor 的 proof_mode 一致"
    return ""


def _contract_primary_visual_proof(contract: dict[str, Any], points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """以通过校验的合同生成唯一 primary，避免模型自由文本把卖点或拍摄条件当证明标准。"""
    secondary = [dict(point) for point in points if point.get("priority") != "primary"]
    primary = {
        "priority": "primary",
        "proof_target": contract["consumer_outcome"],
        "visual_standard": f"{contract['before_state']} vs {contract['after_state']}",
        "visual_diff_dimensions": [contract["observable_signal"]],
        "related_selling_points": [],
    }
    return [primary, *secondary][:4]


def normalize_visual_proof_points(value: Any) -> list[dict[str, Any]]:
    """归一 S4 多视觉证明点；兼容旧 product_profile 无此字段。"""
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        proof_target = str(item.get("proof_target") or item.get("target") or "").strip()
        visual_standard = str(item.get("visual_standard") or item.get("standard") or "").strip()
        if not proof_target and not visual_standard:
            continue
        dims = [str(dim).strip() for dim in item.get("visual_diff_dimensions") or [] if str(dim).strip()][:3]
        related = [str(point).strip() for point in item.get("related_selling_points") or [] if str(point).strip()][:4]
        point = {
            "priority": normalize_choice(item.get("priority"), {"primary", "secondary"}, "secondary"),
            "proof_target": proof_target,
            "visual_standard": visual_standard,
            "visual_diff_dimensions": dims,
            "related_selling_points": related,
        }
        for normalized_point in _split_compound_primary_visual_proof(point):
            out.append(_repair_result_primary_visual_standard(normalized_point))
        if len(out) >= 4:
            break
    if out and not any(point["priority"] == "primary" for point in out):
        out[0]["priority"] = "primary"
    return out[:4]


def _split_compound_primary_visual_proof(point: dict[str, Any]) -> list[dict[str, Any]]:
    """把模型误写成 all-of 的 primary 拆回单一 primary + secondary。

    S4 primary 是消费者最终结果；机制、附加卖点、处置方式不能和最终结果焊成同一个
    all-of 条件，否则任一附加卖点没拍到都会误杀核心效果。
    """
    if point.get("priority") != "primary":
        return [point]
    target = str(point.get("proof_target") or "")
    standard = str(point.get("visual_standard") or "")
    dims = [str(d).strip() for d in point.get("visual_diff_dimensions") or [] if str(d).strip()]
    related = [str(r).strip() for r in point.get("related_selling_points") or [] if str(r).strip()]
    if not _looks_like_compound_primary(target, dims, related):
        return [point]

    head_target, tail_target = _split_first_compound_piece(target)
    head_standard, tail_standard = _split_first_compound_piece(standard)
    primary = dict(point)
    primary["proof_target"] = _clean_proof_phrase(head_target) or target
    primary["visual_standard"] = _clean_proof_phrase(head_standard) or standard
    primary["visual_diff_dimensions"] = dims[:1] or dims
    primary_related = _filter_related_points(
        related,
        " ".join([primary["proof_target"], primary["visual_standard"], " ".join(primary["visual_diff_dimensions"])]),
    )
    primary["related_selling_points"] = primary_related[:4]

    secondary_dims = dims[1:]
    secondary_related = [r for r in related if r not in primary_related]
    secondary_target = _clean_proof_phrase(tail_target)
    secondary_standard = _clean_proof_phrase(tail_standard)
    secondary: dict[str, Any] | None = None
    if secondary_target or secondary_standard or secondary_dims or secondary_related:
        secondary = {
            "priority": "secondary",
            "proof_target": secondary_target or "附加视觉证明",
            "visual_standard": secondary_standard or secondary_target or "补充证明点有效呈现",
            "visual_diff_dimensions": secondary_dims[:3],
            "related_selling_points": secondary_related[:4],
        }
    return [primary, secondary] if secondary else [primary]


def _looks_like_compound_primary(target: str, dims: list[str], related: list[str]) -> bool:
    text = target.strip()
    markers = ("与", "及", "和", "同时", "+", "/", "、", "双重", "多重")
    return any(marker in text for marker in markers) or (len(dims) > 1 and len(related) > 1 and ("双重" in text or "多重" in text))


def _split_first_compound_piece(text: str) -> tuple[str, str]:
    normalized = str(text or "").strip()
    if not normalized:
        return "", ""
    separators = ("；", ";", "，", ",", "并", "且", "同时", "与", "及", "和", "+", "/", "、")
    positions = [(normalized.find(sep), sep) for sep in separators if normalized.find(sep) > 0]
    if not positions:
        return normalized, ""
    index, sep = min(positions, key=lambda item: item[0])
    return normalized[:index], normalized[index + len(sep):]


def _clean_proof_phrase(text: str) -> str:
    cleaned = str(text or "").strip(" ，,；;。")
    for suffix in ("双重验证", "多重验证", "验证", "证明"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    return cleaned.strip(" ，,；;。")


def _filter_related_points(related: list[str], proof_text: str) -> list[str]:
    """只保留与 primary 文字直接相交的卖点，避免拆分后仍被附加卖点污染。"""
    compact = str(proof_text or "").replace(" ", "")
    if not compact:
        return []
    tokens = {compact[i:i + 2] for i in range(max(len(compact) - 1, 0)) if compact[i:i + 2].strip()}
    matched = []
    for item in related:
        item_text = str(item or "")
        if any(token and token in item_text for token in tokens):
            matched.append(item)
    return matched


def _repair_result_primary_visual_standard(point: dict[str, Any]) -> dict[str, Any]:
    """结果型 primary 的 visual_standard 不应被机制触发词替代。"""
    if point.get("priority") != "primary":
        return point
    target = str(point.get("proof_target") or "")
    standard = str(point.get("visual_standard") or "")
    dims = [str(dim).strip() for dim in point.get("visual_diff_dimensions") or [] if str(dim).strip()]
    if not target or not standard or not dims:
        return point
    result_hints = ("效果", "清洁", "洁净", "去污", "美白", "控油", "遮瑕", "修复", "提亮", "补水", "除皱", "除毛", "定妆", "防漏", "防水")
    process_hints = ("入水", "起泡", "释放", "接触", "按压", "打开", "启动", "喷出", "涂抹")
    dimension = dims[0]
    if (
        any(hint in target for hint in result_hints)
        and any(hint in standard for hint in process_hints)
        and ("vs" in dimension.lower() or "→" in dimension or "->" in dimension)
    ):
        repaired = dict(point)
        repaired["visual_standard"] = dimension
        return repaired
    return point


def normalize_bool_flag(value: Any) -> bool:
    """把模型可能输出的 true/"yes"/1/"是" 等统一成 bool。"""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"true", "yes", "1", "是", "有"}


def normalize_product_coverage(value: Any) -> str:
    return normalize_choice(value, {"none", "low", "medium", "high"}, "none")


def normalize_module_id(value: Any, index: int) -> str:
    normalized = str(value or "").strip().upper()
    if normalized == "UNKNOWN":
        return "unknown"
    if re.fullmatch(rf"S{index}-[A-Z]", normalized):
        return normalized
    return "unknown"


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


def normalize_video_understanding(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    normalized: dict[str, Any] = {}
    for role in ("benchmark", "creator"):
        item = source.get(role) if isinstance(source.get(role), dict) else {}
        units = item.get("evidence_units") if isinstance(item.get("evidence_units"), list) else []
        normalized[role] = {
            "content_summary": str(item.get("content_summary") or "").strip(),
            "communication_strategy": str(item.get("communication_strategy") or "").strip(),
            "evidence_units": [
                {
                    "id": str(unit.get("id") or f"{role[0].upper()}{index}").strip(),
                    "time_range": str(unit.get("time_range") or "").strip(),
                    "information": str(unit.get("information") or "").strip(),
                    "voiceover": str(unit.get("voiceover") or "").strip(),
                    "voiceover_zh": str(unit.get("voiceover_zh") or "").strip(),
                    "visual_fact": str(unit.get("visual_fact") or "").strip(),
                    "subtitle_fact": str(unit.get("subtitle_fact") or "").strip(),
                    "product_visible": normalize_bool_flag(unit.get("product_visible")),
                    "product_coverage": normalize_product_coverage(unit.get("product_coverage")),
                    # F 项背书劈成两个纯观察信道（替代焊死判断的 third_party_endorsement）：
                    "endorsement_verbal": normalize_bool_flag(unit.get("endorsement_verbal")),
                    "endorsement_visual": normalize_bool_flag(unit.get("endorsement_visual")),
                }
                for index, unit in enumerate(units, start=1)
                if isinstance(unit, dict)
            ][:20],
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
    if isinstance(value, dict):
        start = value.get("start", value.get("start_time"))
        end = value.get("end", value.get("end_time"))
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            return f"{format_seconds(start)} - {format_seconds(end)}"
    return str(value or "")


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
    stage_analysis = result.get("stage_analysis")
    improvements = result.get("improvements")
    executive_summary = str(result.get("one_line_summary") or result.get("executive_summary") or "").strip()

    if not isinstance(stage_analysis, list) or len(stage_analysis) != len(STAGES):
        raise SystemExit("analysis_result must contain stage_analysis with 6 items.")
    # improvements 数量上限 5，下限 1：LLM 主动判断只有 1 条值得改时不强迫凑数，
    # 避免编造内容污染报告。质量问题（应该有 3 条但只给 1 条）由 prompt 工程和后续 QA-RULES 兜底。
    if not isinstance(improvements, list) or not (1 <= len(improvements) <= 5):
        raise SystemExit("analysis_result must contain 1 to 5 improvements.")

    normalized_stages = []
    for index, item in enumerate(stage_analysis):
        if not isinstance(item, dict):
            raise SystemExit("Each stage_analysis item must be an object.")
        stage_name, default_range, core_question = STAGES[index]
        benchmark_time_range = normalize_time_range_value(item.get("benchmark_time_range") or item.get("time_range") or default_range)
        creator_time_range = normalize_time_range_value(item.get("creator_time_range") or item.get("time_range") or default_range)
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
                # S4 效果呈现布尔（结构库 S4-A~F 判定）：缺失为 None → derive 回退 _DEMO_RE 词法兜底
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
                # 4d：痛点命中事实（替代词法匹配定 C 系数），缺失为 None → derive 词法兜底
                "painpoint_relevance": normalize_painpoint_relevance(item.get("painpoint_relevance")),
                # 到位标准达成事实（全阶段统一，见 prompt 对照表；先收集，暂不卡分）
                "stage_standard_delivery": normalize_stage_standard_delivery(item.get("stage_standard_delivery")),
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
                "aigc_prompt": str(item.get("aigc_prompt") or "").strip(),
                "aigc_image_path": str(item.get("aigc_image_path") or "").strip(),
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


def normalize_video_fact_result(role: str, result: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    code = "B" if role == "benchmark" else "C"
    units = result.get("evidence_units")
    if not isinstance(units, list) or not units:
        raise SystemExit(f"{role} fact extraction returned no evidence_units.")
    normalized = {
        "content_summary": str(result.get("content_summary") or "").strip(),
        "communication_strategy": str(result.get("communication_strategy") or "").strip(),
        "evidence_units": [],
    }
    for index, unit in enumerate(units[:8], start=1):
        if not isinstance(unit, dict):
            continue
        normalized["evidence_units"].append(
            {
                "id": normalized_fact_id(unit.get("id"), code, index),
                "time_range": str(unit.get("time_range") or "").strip(),
                "information": str(unit.get("information") or "").strip(),
                "voiceover": str(unit.get("voiceover") or "").strip(),
                "voiceover_zh": str(unit.get("voiceover_zh") or "").strip(),
                "visual_fact": str(unit.get("visual_fact") or "").strip(),
                "subtitle_fact": str(unit.get("subtitle_fact") or "").strip(),
                "audio_fact": str(unit.get("audio_fact") or "").strip(),
                "product_visible": normalize_bool_flag(unit.get("product_visible")),
                "product_coverage": normalize_product_coverage(unit.get("product_coverage")),
                # F 项背书劈成两个纯观察信道（替代焊死判断的 third_party_endorsement）：
                "endorsement_verbal": normalize_bool_flag(unit.get("endorsement_verbal")),
                "endorsement_visual": normalize_bool_flag(unit.get("endorsement_visual")),
                # 这段支撑哪些带货功能（多选，描述性）；nullable，老 facts 缺失为 None
                "functions": normalize_functions(unit.get("functions")),
            }
        )
    validate_single_video_facts(role, normalized, analysis)
    return normalized


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
