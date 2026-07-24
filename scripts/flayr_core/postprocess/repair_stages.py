"""flayr_core.postprocess.repair_stages：阶段归属与证据边界的确定性校准。

从 repair.py 按 region 簇拆出（2026-06-15，零跨模块依赖）：
  - align_*       按关键词/时间戳把 evidence 归到正确阶段
  - stabilize_*   保留旧调用入口；severity 已由 derive resolver 统一收口
另含这两簇共享的小工具（stage_code/stage_text/has_real_endorsement 等）。
所有函数都是"修改 result data 后正常返回"，不抛 SystemExit。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..artifacts import format_seconds, parse_time_range_seconds, parse_timestamp_seconds
from ..stage_catalog import stage_tuples

STAGES = stage_tuples()

S1_POSTPROCESS_STATE_KEY = "_postprocess_state"
S1_HOOK_BOUNDARIES_STATE_KEY = "s1_hook_boundaries"
S1_HOOK_BOUNDARIES_REPAIRED = "repaired"
from .utils import (
    adjacent_review_range,
    assign_benchmark_unit,
    ensure_evidence_unit,
    find_evidence_unit,
    first_unmapped_overlapping_unit,
    read_srt_segments,
)


S2_START_CUES = [
    "能解决",
    "解决",
    "能拯救",
    "拯救",
    "救",
    "直到我发现",
    "我发现",
    "我用的是",
    "我用的就是",
    "答案",
    "秘密",
    "就是它",
    "这个产品",
    "这款产品",
    "这个是",
    "这款是",
    "认证",
    "成分",
    "价格",
    "优惠",
    "推荐",
    "朋友推荐",
    "医生",
]

S1_HOOK_CUES = [
    "痛",
    "糟糕",
    "坏了",
    "不来月经",
    "经期",
    "疲劳",
    "脸色暗",
    "油光",
    "出油",
    "没期待",
    "超预期",
    "结果",
    "为什么",
    "是不是",
    "有没有",
    "适合",
    "家里有",
    "2岁",
    "女性",
    "perempuan",
    "rosak",
    "gigi",
    "period",
    "penat",
]

GENERIC_SUPPLEMENT_ANCHOR_CUES = [
    "女性",
    "woman",
    "women",
    "perempuan",
    "补充剂",
    "supplement",
    "suplemen",
    "维生素",
    "vitamin",
    "multivitamin",
]

PERIOD_ANCHOR_CUES = [
    "生理",
    "经期",
    "月经",
    "痛经",
    "气血",
    "含铁",
    "铁",
    "情绪",
    "腹痛",
    "疲劳",
    "乏力",
    "面色",
    "暗沉",
    "手脚",
    "荷尔蒙",
]

ANCHOR_ALIASES = {
    "生理期专用配方": ["生理", "经期", "月经", "period", "haid"],
    "补气血(含铁)": ["气血", "含铁", "铁", "iron", "darah"],
    "经期情绪舒缓": ["经期", "情绪", "mood", "hormon", "hormone"],
    "经期腹痛": ["经期", "痛经", "腹痛", "senggugut", "period"],
    "情绪波动": ["情绪", "mood", "hormon", "hormone"],
    "疲劳乏力": ["疲劳", "乏力", "累", "penat", "tired"],
    "面色暗沉": ["面色", "脸色", "暗沉", "kusam", "tua"],
    "手脚冰冷": ["手脚", "冰冷", "冷"],
}


# region align ---------------------------------------------------------------

def repair_s1_hook_boundaries(result: dict[str, Any], analysis: dict[str, Any]) -> None:
    """用 SRT/facts 候选边界收敛 S1 Hook，避免模型等到产品亮相才把 S2 切出来。"""
    s1 = next((stage for stage in result.get("stage_analysis", []) if str(stage.get("stage", "")).startswith("S1")), None)
    if not isinstance(s1, dict):
        return
    # derive 只消费经过这一轮 repair 的 canonical S1 facts。即使本轮没有
    # 可应用候选也要写入，表示前置检查已完成，而不是表示一定发生了改写。
    postprocess_state = s1.setdefault(S1_POSTPROCESS_STATE_KEY, {})
    if isinstance(postprocess_state, dict):
        postprocess_state[S1_HOOK_BOUNDARIES_STATE_KEY] = S1_HOOK_BOUNDARIES_REPAIRED
    for role, side in (("creator", "creator"), ("benchmark", "benchmark")):
        hook = s1.get(f"{side}_hook")
        if not isinstance(hook, dict):
            continue
        repair_s1_hook_observable_floor(role, hook, result)
        candidate = infer_s1_boundary_candidate(role, result, analysis)
        if candidate:
            current = hook.get("hook_boundary_seconds")
            if isinstance(current, bool) or not isinstance(current, (int, float)):
                current = None
            should_apply_candidate = (
                current is None
                or float(current) > candidate["seconds"] + 0.5
                or (candidate.get("source") == "evidence" and float(current) < candidate["seconds"] - 0.5)
            )
            if should_apply_candidate:
                hook["hook_boundary_seconds"] = candidate["seconds"]
                hook["hook_boundary_reason"] = f"{hook.get('hook_boundary_reason') or ''}（系统按 SRT/facts 边界候选收回：{candidate['reason']}）"
                if not hook.get("s2_start_signal"):
                    hook["s2_start_signal"] = candidate.get("cue") or "S2 产品/解决方案承接开始"
        reason = str(hook.get("landing_reason") or "")
        leaks_by_time = hook_reason_window_leaks(reason, hook.get("hook_boundary_seconds"))
        if leaks_by_time:
            hook["landing_window_leak"] = True
            if hook.get("landing_met") is True:
                hook["landing_met"] = False
                append_system_note(
                    hook,
                    "landing_reason",
                    "系统按 S1 边界复核：该理由借用了边界后的 S2/S3 材料，landing 改为 false。",
                )
        repair_s1_anchor_proposition(role, hook, result, analysis)


def append_system_note(target: dict[str, Any], key: str, note: str) -> None:
    """给模型原理由追加确定性后处理说明，避免审计字段和修正结果互相矛盾。"""
    current = str(target.get(key) or "").strip()
    if note in current:
        return
    target[key] = f"{current}（{note}）" if current else f"（{note}）"


def repair_s1_anchor_proposition(role: str, hook: dict[str, Any], result: dict[str, Any], analysis: dict[str, Any]) -> None:
    """按冻结 S1 命题尺子压掉宽泛人群/品类词造成的 anchors 误判。

    典型 case：女性保健品开头只说"女性/很多补充剂"，没有触及经期、气血、疲劳、面色等
    冻结命题时，不能算命题锚定。
    """
    if hook.get("anchors_proposition") is not True:
        return
    bp = analysis.get("brand_proposition") if isinstance(analysis.get("brand_proposition"), dict) else {}
    terms = [str(item).strip() for item in (bp.get("propositions") or []) + (bp.get("painpoints") or []) if str(item).strip()]
    if not terms or not anchor_set_is_period_specific(terms):
        return
    evidence = find_early_evidence_for_role(role, result)
    text = " ".join(
        part
        for part in [
            evidence_text(evidence or {}),
            str(hook.get("window_evidence") or ""),
            str(hook.get("landing_reason") or ""),
        ]
        if part
    )
    if direct_anchor_hit(text, terms):
        return
    if any(cue.lower() in text.lower() for cue in GENERIC_SUPPLEMENT_ANCHOR_CUES):
        hook["anchors_proposition"] = False
        append_system_note(
            hook,
            "landing_reason",
            "系统按冻结 S1 命题尺子复核：仅有人群/补充剂泛词，未命中经期、气血、疲劳、面色等核心锚点，anchors 改为 false。",
        )


def anchor_set_is_period_specific(terms: list[str]) -> bool:
    text = " ".join(terms)
    return any(cue in text for cue in PERIOD_ANCHOR_CUES)


def direct_anchor_hit(text: str, terms: list[str]) -> bool:
    compact = re.sub(r"\s+", "", text).lower()
    needles: list[str] = []
    for term in terms:
        needles.append(term)
        needles.extend(ANCHOR_ALIASES.get(term, []))
    for needle in needles:
        value = re.sub(r"\s+", "", str(needle)).lower()
        if value and value in compact:
            return True
    return False


def repair_s1_hook_observable_floor(role: str, hook: dict[str, Any], result: dict[str, Any]) -> None:
    """用早段 facts 给 hook_exists 和四维可观察项做下限，防模型偶发把有证据的 Hook 判成全无。"""
    evidence = find_early_evidence_for_role(role, result)
    if not evidence or not early_evidence_has_hook_signal(evidence):
        return
    if hook.get("exists") is not True:
        hook["exists"] = True
        hook["hook_boundary_reason"] = f"{hook.get('hook_boundary_reason') or ''}（系统按早段 facts 识别到 Hook 信号，修正 exists=true）"
    dims = hook.get("dims") if isinstance(hook.get("dims"), dict) else {}
    hook["dims"] = dims
    if has_visual_signal(evidence):
        dims["camera"] = True
    if has_copy_signal(evidence):
        dims["copy"] = True
    if has_sound_signal(evidence):
        dims["sound"] = True


def early_evidence_has_hook_signal(unit: dict[str, Any]) -> bool:
    funcs = {str(item) for item in unit.get("functions") or []}
    text = evidence_text(unit)
    return "S1_hook" in funcs or any(cue.lower() in text.lower() for cue in S1_HOOK_CUES)


def has_visual_signal(unit: dict[str, Any]) -> bool:
    visual = str(unit.get("visual_fact") or "").strip()
    return bool(visual and "未发现" not in visual and "无" != visual)


def has_copy_signal(unit: dict[str, Any]) -> bool:
    text = " ".join(str(unit.get(key) or "") for key in ("information", "voiceover_zh", "subtitle_fact", "voiceover"))
    return any(cue.lower() in text.lower() for cue in S1_HOOK_CUES) or bool(str(unit.get("voiceover_zh") or "").strip())


def has_sound_signal(unit: dict[str, Any]) -> bool:
    return bool(str(unit.get("voiceover") or "").strip() or str(unit.get("audio_fact") or "").strip())


def evidence_text(unit: dict[str, Any]) -> str:
    return " ".join(
        str(unit.get(key) or "")
        for key in ("information", "voiceover", "voiceover_zh", "visual_fact", "subtitle_fact")
    )


def infer_s1_boundary_candidate(role: str, result: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any] | None:
    segments = read_srt_segments((analysis.get("videos") or {}).get(role, {}) if isinstance(analysis.get("videos"), dict) else {})
    if len(segments) >= 2:
        first = segments[0]
        second = segments[1]
        start = parse_timestamp_seconds(second.get("start"))
        if start is None:
            return None
        if 0 < start <= 12:
            # 只能用相邻 SRT 句判这个候选边界。早段 fact 常把多句口播合并为
            # 一个单元；把它混进来会把后段的“推荐/产品名”错误投射到第二句起点。
            text = " ".join(
                [
                    str(first.get("text") or ""),
                    str(second.get("text") or ""),
                ]
            )
            cue = find_s2_start_cue(text)
            if cue:
                return {
                    "seconds": round(start, 2),
                    "cue": cue,
                    "source": "srt",
                    "reason": (
                        f"SRT 第一句 {first.get('start', 0):.2f}-{first.get('end', 0):.2f}s 更像 S1 留人；"
                        f"第二句从 {start:.2f}s 出现“{cue}”类 S2 承接信号。"
                    ),
                }
    return infer_boundary_from_evidence(role, result)


def infer_boundary_from_evidence(role: str, result: dict[str, Any]) -> dict[str, Any] | None:
    units = get_role_evidence_units(role, result)
    if len(units) < 2:
        return None
    previous = units[0]
    for current in units[1:4]:
        parsed = parse_time_range_seconds(current.get("time_range"), None)
        if parsed is None:
            continue
        current_start, _ = parsed
        if current_start <= 0 or current_start > 12:
            continue
        prev_functions = {str(item) for item in previous.get("functions") or []}
        current_functions = {str(item) for item in current.get("functions") or []}
        current_text = " ".join(
            str(current.get(key) or "")
            for key in ("information", "voiceover_zh", "visual_fact", "subtitle_fact")
        )
        cue = find_s2_start_cue(current_text)
        if "S1_hook" in prev_functions and ("S2_intro" in current_functions or cue):
            return {
                "seconds": round(current_start, 2),
                "cue": cue,
                "source": "evidence",
                "reason": (
                    f"facts 中 {previous.get('id')} 主功能为 S1_hook，"
                    f"{current.get('id')} 从 {current_start:.2f}s 进入 S2_intro/产品承接。"
                ),
            }
        previous = current
    return None


def find_early_evidence_for_role(role: str, result: dict[str, Any]) -> dict[str, Any]:
    units = get_role_evidence_units(role, result)
    if not units:
        return {}
    timed_units = []
    for unit in units:
        parsed = parse_time_range_seconds(unit.get("time_range"), None)
        if parsed is not None:
            timed_units.append((parsed[0], unit))
    return min(timed_units, key=lambda item: item[0])[1] if timed_units else {}


def get_role_evidence_units(role: str, result: dict[str, Any]) -> list[dict[str, Any]]:
    facts = result.get("video_understanding") if isinstance(result.get("video_understanding"), dict) else {}
    direct = facts.get(role) if isinstance(facts.get(role), dict) else {}
    units = direct.get("evidence_units") if isinstance(direct.get("evidence_units"), list) else []
    return [unit for unit in units if isinstance(unit, dict)]


def find_s2_start_cue(text: str) -> str:
    compact = re.sub(r"\s+", "", text)
    for cue in S2_START_CUES:
        if cue in compact:
            return cue
    return ""


def hook_reason_window_leaks(reason: str, boundary_seconds: Any, tolerance: float = 0.3) -> bool:
    if isinstance(boundary_seconds, bool) or not isinstance(boundary_seconds, (int, float)):
        return False
    vals = [float(m) for m in re.findall(r"(\d+(?:\.\d+)?)\s*s", reason or "")]
    return bool(vals and max(vals) > float(boundary_seconds) + tolerance)

def align_clear_commerce_evidence(result: dict[str, Any]) -> None:
    """按关键词把 benchmark 的高确定性事实归到对应阶段（成分→S2, feedback→S4, KKM 认证→S5 等）。"""
    stages = result.get("stage_analysis", [])
    units = result.get("video_understanding", {}).get("benchmark", {}).get("evidence_units", [])
    if len(stages) != len(STAGES) or not isinstance(units, list):
        return
    assignments = {
        # 成分/卖点信息归产品引出 S2；第三方认证按功能归信任放大 S5（不归 S2）。
        1: find_evidence_unit(
            [unit for unit in units if not re.search(r"KKM|KKMA|kelulusan|认证", json.dumps(unit, ensure_ascii=False), flags=re.IGNORECASE)],
            r"vitamin|collagen|成分",
        ),
        3: find_evidence_unit(units, r"feedback|testimoni|评论|反馈|testimonial"),
        4: find_evidence_unit(units, r"KKM|KKMA|kelulusan|认证"),
        5: find_evidence_unit(units, r"beli|bagun|troli|cart|dekat sini|下单|购买"),
    }
    for index, unit in assignments.items():
        if unit:
            assign_benchmark_unit(stages[index], unit)

    mapped_ids = {str(unit.get("id")) for unit in assignments.values() if unit}
    usage_unit = first_unmapped_overlapping_unit(units, mapped_ids, stages[2].get("benchmark_time_range"))
    if usage_unit:
        assign_benchmark_unit(stages[2], usage_unit)
        return
    placeholder = {
        "id": "B_NO_USAGE",
        "time_range": adjacent_review_range(assignments.get(1), assignments.get(3), stages[2].get("benchmark_time_range")),
        "information": "该时间段未识别到可独立归因的使用步骤演示。",
        "voiceover": "",
        "voiceover_zh": "",
        "visual_fact": "未发现可独立验证的使用步骤画面，需人工复核原视频。",
        "subtitle_fact": "",
    }
    ensure_evidence_unit(units, placeholder)
    assign_benchmark_unit(stages[2], placeholder)


def align_timed_cta_from_transcript(result: dict[str, Any], analysis: dict[str, Any]) -> None:
    """以 SRT 时间戳识别尾段购买指令，覆盖模型可能错位的 CTA 时间。"""
    stages = result.get("stage_analysis", [])
    if len(stages) < 6:
        return
    cta = stages[5]
    for role, code in (("benchmark", "B"), ("creator", "C")):
        info = analysis.get("videos", {}).get(role, {})
        duration = parse_timestamp_seconds(info.get("duration_seconds"))
        if duration is None or duration <= 0:
            continue
        segments = read_srt_segments(info)
        candidates = [
            segment
            for segment in segments
            if segment["start"] >= duration * 0.55
            and re.search(r"\b(beli|troli|klik|cart|checkout|order|link|direct)\b|购买|下单|购物车|点击", segment["text"], flags=re.IGNORECASE)
        ]
        if not candidates:
            continue
        last = candidates[-1]
        selected = [last]
        for segment in reversed(candidates[:-1]):
            if selected[0]["start"] - segment["end"] <= 0.5:
                selected.insert(0, segment)
            else:
                break
        time_range = f"{format_seconds(selected[0]['start'])} - {format_seconds(selected[-1]['end'])}"
        quote = " ".join(segment["text"] for segment in selected).strip()
        unit_id = f"{code}_CTA_SRT"
        unit = {
            "id": unit_id,
            "time_range": time_range,
            "information": "结尾口播出现明确购买或点击指令。",
            "voiceover": quote,
            "voiceover_zh": "",
            "visual_fact": "该结论由口播时间戳支持；画面是否呈现购物车提示需结合关键帧复核。",
            "subtitle_fact": "",
        }
        units = result.get("video_understanding", {}).get(role, {}).get("evidence_units", [])
        ensure_evidence_unit(units, unit)
        cta[f"{role}_time_range"] = time_range
        cta[f"{role}_evidence_ids"] = [unit_id]
        cta[f"{role}_key_message"] = unit["information"]
        cta[f"{role}_summary"] = unit["information"]
        cta[f"{role}_quote"] = quote
        cta[f"{role}_quote_zh"] = ""
        cta[f"{role}_visual_evidence"] = [unit["visual_fact"]]
        cta[f"{role}_support_status"] = "voice_only"

# endregion


# region stabilize -----------------------------------------------------------

def stabilize_stage_severity(result: dict[str, Any]) -> None:
    """兼容性占位钩子；最终 severity 只能由 derive resolver 写入。

    过去这里的文本兜底会先写 severity，再由 derive 覆盖，造成规则顺序和
    gap 文本不同步。保留调用点但不再修改任何 severity 或阶段业务字段。
    """
    del result


def apply_comparison_eligibility(result: dict[str, Any]) -> None:
    """按阶段比较合同限制差距结论，不用整体商品关系替阶段作答。"""
    contract = result.get("comparison_contract") or result.get("comparison_eligibility")
    if not isinstance(contract, dict):
        return
    from ..llm.parse import normalize_comparison_contract

    contract = normalize_comparison_contract(contract)
    result["comparison_contract"] = contract
    result["comparison_eligibility"] = contract
    stage_contracts = contract.get("stage_eligibility")
    if not isinstance(stage_contracts, dict):
        return
    default_reason = str(contract.get("reason") or "两条视频在该阶段缺少共同比较合同。").strip()
    for stage in result.get("stage_analysis", []):
        if not isinstance(stage, dict):
            continue
        code = stage_code(stage)
        stage_contract = stage_contracts.get(code)
        if not isinstance(stage_contract, dict):
            continue
        status = str(stage_contract.get("status") or "not_comparable")
        reason = str(stage_contract.get("basis") or default_reason).strip()
        stage["comparison_contract"] = dict(stage_contract)
        if status == "direct":
            stage["comparison_basis"] = "product_direct"
            stage.pop("comparison_status", None)
            stage.pop("comparison_reason", None)
            continue
        if status == "structural":
            stage["comparison_basis"] = "structure_execution"
            stage["comparison_status"] = "structural_comparison"
            stage["comparison_reason"] = reason
            continue
        stage["comparison_status"] = "not_applicable" if status == "not_applicable" else "not_directly_comparable"
        stage["comparison_reason"] = reason
        trace = stage.get("severity_derivation")
        if isinstance(trace, dict):
            trace["direct_product_comparison_eligible"] = False

    if contract.get("overall_status") != "full_direct":
        result["comparison_scope_note"] = comparison_scope_summary(contract)


def comparison_scope_summary(eligibility: dict[str, Any]) -> str:
    """按阶段资格生成范围说明，不把结构可比误写成产品优劣可比。"""
    from ..llm.parse import normalize_comparison_contract

    eligibility = normalize_comparison_contract(eligibility)
    identity = str(eligibility.get("identity_relation") or "uncertain").strip()
    substitution = str(eligibility.get("substitution_relation") or "uncertain").strip()
    overall = str(eligibility.get("overall_status") or "uncertain").strip()
    reason = str(eligibility.get("reason") or "两条视频的产品级比较资格不足。").strip()
    stage_contracts = eligibility.get("stage_eligibility") if isinstance(eligibility.get("stage_eligibility"), dict) else {}
    stage_labels = {
        "S1": "S1 Hook",
        "S2": "S2 产品引出",
        "S3": "S3 使用过程",
        "S4": "S4 效果呈现",
        "S5": "S5 信任放大",
        "S6": "S6 CTA",
    }
    structural = [
        stage_labels[stage] for stage in stage_labels
        if isinstance(stage_contracts.get(stage), dict) and stage_contracts[stage].get("status") == "structural"
    ]
    skipped = [
        stage_labels[stage] for stage in stage_labels
        if isinstance(stage_contracts.get(stage), dict)
        and stage_contracts[stage].get("status") in {"not_applicable", "not_comparable"}
    ]
    if overall == "full_direct":
        return "两条视频属于同品或同产品家族，S1-S6 可直接比较。"
    if overall == "selective_structural":
        structural_text = "、".join(structural) or "无"
        skipped_text = "、".join(skipped) or "无"
        return (
            f"两条视频为不同产品但存在{('强' if substitution == 'strong_substitute' else '部分')}替代关系；"
            f"仅在共同消费者任务下比较内容执行：{structural_text}；不比较或未涉及：{skipped_text}。{reason}"
        )
    if overall == "not_comparable":
        return f"两条视频属于不可替代的不同产品，不输出 S1-S6 差距结论。{reason}"
    return f"两条视频的商品关系或替代关系无法确认，暂不输出 S1-S6 差距结论。{reason}"


def stabilize_improvement_priorities(result: dict[str, Any]) -> None:
    """让 Top 改进跟随最终 stage 判断，避免把达人优势阶段列为高优先级。"""
    stages = {stage_code(stage): stage for stage in result.get("stage_analysis", []) if isinstance(stage, dict)}
    cta_not_gap = str(stages.get("S6", {}).get("severity") or "") == "small"
    filtered: list[dict[str, Any]] = []
    excluded_by_scope = False
    for item in result.get("improvements", []):
        if not isinstance(item, dict):
            continue
        target = improvement_stage_code(item)
        if not improvement_is_actionable(item, target):
            continue
        if str(stages.get(target, {}).get("comparison_status") or "") in {"not_directly_comparable", "not_applicable"}:
            excluded_by_scope = True
            continue
        cta_label = " ".join(str(item.get(key) or "") for key in ("target_stage", "title"))
        if cta_not_gap and (target == "S6" or re.search(r"CTA|促单", cta_label, flags=re.IGNORECASE)):
            continue
        filtered.append(item)
    severity_rank = {"large": 0, "medium": 1, "small": 2}
    filtered.sort(
        key=lambda item: (
            severity_rank.get(str(stages.get(improvement_stage_code(item), {}).get("severity") or "medium"), 1),
            int(item.get("priority") or 99),
        )
    )
    for index, item in enumerate(filtered, start=1):
        item["priority"] = index
    result["improvements"] = filtered[:5]


def improvement_is_actionable(item: dict[str, Any], target: str) -> bool:
    """报告只保留可执行的提升点，不能把 repair 模板占位符展示给用户。"""
    title = str(item.get("title") or "").strip()
    return bool(
        target
        and title
        and "LLM 未填写" not in title
    )


def improvement_stage_code(item: dict[str, Any]) -> str:
    text = " ".join(str(item.get(key) or "") for key in ("target_stage", "title", "problem", "suggestion"))
    match = re.search(r"\b(S[1-6])\b", text, flags=re.IGNORECASE)
    if match:
        return match.group(1).upper()
    keywords = (
        ("S6", r"CTA|促单|下单|购买|购物车"),
        ("S4", r"效果|验证|闻香|口味|香味|感官"),
        ("S3", r"使用|演示|步骤|how-to|按压"),
        ("S2", r"引出|卖点|产品"),
        ("S1", r"Hook|钩子|开头|停留"),
        ("S5", r"信任|背书|认证|测评"),
    )
    for code, pattern in keywords:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return code
    return ""


def stage_code(stage: dict[str, Any]) -> str:
    match = re.match(r"(S[1-6])", str(stage.get("stage") or ""))
    return match.group(1) if match else ""


def stage_text(stage: dict[str, Any]) -> str:
    return json.dumps(
        {
            "gap": stage.get("gap"),
            "gap_summary": stage.get("gap_summary"),
            "module_fit_reason": stage.get("module_fit_reason"),
            "evidence": stage.get("evidence"),
        },
        ensure_ascii=False,
    )


def role_stage_text(stage: dict[str, Any], role: str) -> str:
    return json.dumps(
        {
            "summary": stage.get(f"{role}_summary"),
            "key_message": stage.get(f"{role}_key_message"),
            "quote": stage.get(f"{role}_quote"),
            "visual_evidence": stage.get(f"{role}_visual_evidence"),
            "support_status": stage.get(f"{role}_support_status"),
        },
        ensure_ascii=False,
    )


def role_has_positive_cta(result: dict[str, Any], role: str) -> bool:
    units = result.get("video_understanding", {}).get(role, {}).get("evidence_units", [])
    for unit in units if isinstance(units, list) else []:
        if not isinstance(unit, dict):
            continue
        if "_NO_CTA" in str(unit.get("id") or ""):
            continue
        text = json.dumps(unit, ensure_ascii=False)
        if re.search(r"未识别|未见|未发现|没有明确|无购买|无下单", text):
            continue
        if creator_has_cta(text):
            return True
    return False


def creator_not_worse(text: str) -> bool:
    positive_patterns = (
        r"无明显差距",
        r"持平",
        r"不输",
        r"优于标杆",
        r"达人[^。；;，,]{0,18}(优于|更好|更强|更清晰|更有效)",
        r"反而[^。；;，,]{0,18}(更好|更强|更有效|增强)",
        r"都有效",
        r"均有效",
        r"不同但均",
    )
    return bool(re.search("|".join(positive_patterns), text, flags=re.IGNORECASE))


# creator_effect_not_worse / creator_has_usage_demo / creator_has_functional_effect /
# mentions_sensory_gap / sensory_effect_gap 已随牙膏特例一并删除（2026-06-11，TODO #1）。


# 背书类型词（跨品类稳定）：监管认证 / 检测临床 / 测评口碑 / 权威推荐。
# 刻意不含任何具体功效卖点——自述功效（如 12hrs 防蛀）是卖点不是背书，
# 误把它当背书正是 stabilize 过拟合单一品类的根源。
_ENDORSEMENT_PATTERN = (
    r"认证|认可|检测报告|检验|临床|clinical|lab[\s-]?tested|certified|"
    r"KKM|kelulusan|sijil|BPOM|halal|FDA|SNI|GMP|"
    r"测评|评测|review|ulasan|口碑|testimoni|好评|回购|销量|畅销|"
    r"医生|牙医|皮肤科|药剂师|专家|expert|dermatologist|doktor|权威|官方推荐|机构"
)
_ENDORSEMENT_RE = re.compile(_ENDORSEMENT_PATTERN, re.IGNORECASE)
# 否定语境：把"无/没有/缺乏/未/tanpa/tiada/no + (第三方/独立/权威等) + 背书词"整段抹掉，
# 避免"无第三方认证"被读成"有认证"。这是纯正则能力的边界，详见函数注释。
_NEG_ENDORSEMENT_RE = re.compile(
    r"(?:无|沒有|没有|没|缺乏|缺少|未[见有]?|不具备|非|tanpa|tiada|tidak\s*ada|bukan|without|lack[a-z ]*|\bno\b)"
    # 否定后连续抹掉一串背书词（含限定词与连接符），处理"无第三方认证""tiada kelulusan KKM"这类多词连用
    r"(?:\s*(?:第三方|独立|官方|权威|真正的?|实质性?|任何)?\s*(?:" + _ENDORSEMENT_PATTERN + r")[\s,，、和及]*)+",
    re.IGNORECASE,
)


def has_real_endorsement(text: str) -> bool:
    """是否含真背书（外部支撑），跨品类通用：监管认证 / 检测临床 / 测评口碑 / 权威推荐。

    先抹掉否定语境再匹配，处理"无第三方认证"这类假阳性。
    注意：这是纯正则能稳定到的上限——彻底泛化（语义级"卖点 vs 背书"判断）应改为
    让模型输出结构化标记（同 product_visible 的做法），由代码消费。
    含软背书（测评/口碑/好评/销量）；S5 severity 闸需要"硬背书"口径时用 has_hard_endorsement。
    """
    cleaned = _NEG_ENDORSEMENT_RE.sub("", str(text or ""))
    return bool(_ENDORSEMENT_RE.search(cleaned))


# 硬背书子集（机构/监管/检测临床/医生专家），排除软背书（测评/口碑/好评/销量）。
# 用户判例：软背书 ≤ 硬背书，双方均无硬背书（哪怕一方有软背书）→ S5 small。
_HARD_ENDORSEMENT_PATTERN = (
    r"认证|认可|检测报告|检验|临床|clinical|lab[\s-]?tested|certified|"
    r"KKM|kelulusan|sijil|BPOM|halal|FDA|SNI|GMP|"
    r"医生|牙医|皮肤科|药剂师|专家|expert|dermatologist|doktor|权威|官方推荐|机构"
)
_HARD_ENDORSEMENT_RE = re.compile(_HARD_ENDORSEMENT_PATTERN, re.IGNORECASE)


def has_hard_endorsement(text: str) -> bool:
    """是否含【硬】背书（机构/监管/检测/医生专家）——软背书(测评/口碑/好评/销量)不算。
    与 Stage1 结构化 flag endorsement_verbal/visual 的硬来源口径一致；derive S5 闸缺 flag 时用它兜底。"""
    cleaned = _NEG_ENDORSEMENT_RE.sub("", str(text or ""))
    return bool(_HARD_ENDORSEMENT_RE.search(cleaned))


def creator_has_cta(text: str) -> bool:
    return bool(re.search(r"买|购买|下单|小黄车|黄色|购物车|beg|kuning|grab|beli|direct|link|cart", text, flags=re.IGNORECASE))


# endregion
