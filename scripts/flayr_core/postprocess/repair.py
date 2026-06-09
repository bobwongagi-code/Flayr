"""flayr_core.postprocess.repair：对 result data 做修补的纯函数式变换。

本模块所有函数都是"修改 result data 后正常返回"，不抛 SystemExit，不触发流程终止。
按动词分组：
  - align_*       按规则把 evidence 归到正确阶段
  - bind_*        用真实数据回填 stage / improvement
  - reconcile_*   补占位 evidence_unit 让 schema 完整
  - ground_*      把字段对齐到所引用 evidence_unit 的事实
  - fill_*        修复阶段引用 evidence_unit 时间错位
  - materialize_* 阶段有口播但缺时段证据时造一个 stage 占位单元
  - deduplicate_* 去除跨阶段重复 quote 子句
  - downgrade_*   把未验证主张状态降为 voice_only
  - stabilize_*   对 LLM 容易漂移的阶段差距等级做确定性校准

另外含"品牌/型号清洗"和"时间归一"两小块，行为同样是修改 data 后返回，故归本模块。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..artifacts import (
    format_seconds,
    parse_time_range_seconds,
    parse_timestamp_seconds,
)
from ..llm.parse import STAGES, is_effective_voiceover
from .utils import (
    adjacent_review_range,
    assign_benchmark_unit,
    ensure_evidence_unit,
    evidence_mentions_product,
    evidence_overlaps_range,
    evidence_unit_at_time,
    find_evidence_unit,
    first_unmapped_overlapping_unit,
    nearest_evidence_unit,
    nearest_product_evidence_unit,
    read_srt_segments,
    referenced_spoken_unit,
)


# region align ---------------------------------------------------------------

def align_clear_commerce_evidence(result: dict[str, Any]) -> None:
    """按关键词把 benchmark 的高确定性事实归到对应阶段（KKM→S2, feedback→S4 等）。"""
    stages = result.get("stage_analysis", [])
    units = result.get("video_understanding", {}).get("benchmark", {}).get("evidence_units", [])
    if len(stages) != len(STAGES) or not isinstance(units, list):
        return
    assignments = {
        1: find_evidence_unit(units, r"KKM|KKMA|kelulusan|认证"),
        3: find_evidence_unit(units, r"feedback|testimoni|评论|反馈|testimonial"),
        4: find_evidence_unit(
            [unit for unit in units if not re.search(r"KKM|KKMA|kelulusan|认证", json.dumps(unit, ensure_ascii=False), flags=re.IGNORECASE)],
            r"vitamin|collagen|成分",
        ),
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
        duration = float(info.get("duration_seconds") or 0.0)
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
    """校准容易跨阶段漂移的 severity。

    只处理高确定性的规则：
    - S3 只回答"能不能看懂怎么用"，闻香/口味等感官体验差距归 S4；
    - 达人某阶段持平或优于标杆时，severity 不应超过 small；
    - 标杆没有 CTA 而达人有购买指令时，S6 不应被"不够强促销"惩罚。
    """
    creator_global_has_cta = role_has_positive_cta(result, "creator")
    benchmark_global_has_cta = role_has_positive_cta(result, "benchmark")

    for stage in result.get("stage_analysis", []):
        stage_id = stage_code(stage)
        text = stage_text(stage)
        creator_text = role_stage_text(stage, "creator")
        benchmark_text = role_stage_text(stage, "benchmark")

        if creator_not_worse(text):
            set_stage_small(stage)

        if stage_id == "S3" and creator_has_usage_demo(creator_text) and (
            stage.get("severity") == "small" or mentions_sensory_gap(text + benchmark_text)
        ):
            set_stage_small(
                stage,
                "达人和标杆都让用户看懂用法；闻香、口味等感官体验差距归 S4，不构成 S3 降低购买意愿的硬伤。",
                "达人已完成按压/用量说明，S3 仅保留细节差距。",
            )

        if stage_id == "S4" and sensory_effect_gap(text, creator_text, benchmark_text):
            if creator_has_functional_effect(creator_text):
                set_stage_small(
                    stage,
                    "达人已用按压用量/减少浪费等功能结果完成效果验证；标杆的香味/口味展示只能作为辅助体验，不覆盖达人核心卖点优势。",
                    "功能效果已完成，感官体验缺口不判为中大差距。",
                )
            else:
                stage["severity"] = "medium"
                stage["gap"] = "标杆用闻香/口味等感官体验增强孩子愿意使用的想象，达人缺少对应体验验证，会削弱但不直接决定购买意愿。"
                stage["gap_summary"] = ["感官体验展示不足，但不是压过功能卖点的硬伤。"]

        if stage_id == "S4" and creator_has_functional_effect(creator_text) and creator_effect_not_worse(text):
            set_stage_small(
                stage,
                "达人已用功能结果完成效果验证，且该阶段不弱于标杆；香味/口味只能作为辅助体验，不判中大差距。",
                "达人 S4 不弱于标杆，差距等级按 small 处理。",
            )

        if stage_id == "S4" and creator_has_functional_effect(creator_text) and mentions_sensory_gap(text):
            set_stage_small(
                stage,
                "达人已用按压用量/减少浪费等功能结果完成效果验证；儿童牙膏的香味/孩子喜欢只能作为辅助体验，不压过核心功能卖点。",
                "功能效果已完成，感官体验不判中大差距。",
            )

        # S2 不再无条件兜底 small：双方都完成产品引出不代表卖点质量相当，
        # 需由 LLM 按品类消费者决策优先级判断卖点选择是否偏离。

        if stage_id == "S5" and not has_real_endorsement(creator_text) and not has_real_endorsement(benchmark_text):
            set_stage_small(
                stage,
                "双方均未提供第三方背书（认证/检测/测评/口碑/权威），S5 信任放大环节均未涉及；自述功效属卖点，不计为背书。",
                "双方均未涉及 S5，差距按 small 处理。",
            )

        if stage_id == "S6" and creator_global_has_cta and not benchmark_global_has_cta:
            set_stage_small(
                stage,
                "达人有明确购买指令，标杆未设计独立 CTA；不因缺少限时/限量话术判为中大差距。",
                "达人 CTA 不弱于标杆，差距等级按 small 处理。",
            )


def stabilize_improvement_priorities(result: dict[str, Any]) -> None:
    """让 Top 改进跟随最终 stage 判断，避免把达人优势阶段列为高优先级。"""
    stages = {stage_code(stage): stage for stage in result.get("stage_analysis", []) if isinstance(stage, dict)}
    cta_not_gap = str(stages.get("S6", {}).get("severity") or "") == "small"
    filtered: list[dict[str, Any]] = []
    for item in result.get("improvements", []):
        if not isinstance(item, dict):
            continue
        target = improvement_stage_code(item)
        cta_label = " ".join(str(item.get(key) or "") for key in ("target_stage", "title"))
        if cta_not_gap and (target == "S6" or re.search(r"CTA|促单", cta_label, flags=re.IGNORECASE)):
            continue
        filtered.append(item)
    if not filtered:
        filtered = [item for item in result.get("improvements", []) if isinstance(item, dict)]
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


def role_units_text(result: dict[str, Any], role: str) -> str:
    units = result.get("video_understanding", {}).get(role, {}).get("evidence_units", [])
    return json.dumps(units, ensure_ascii=False)


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


def creator_effect_not_worse(text: str) -> bool:
    positive_patterns = (
        r"达人[^。；;]{0,40}(说服力强|更能说服|更强|更有效|优于)",
        r"核心价值[^。；;]{0,24}(有效|清晰)",
        r"标杆[^。；;]{0,40}(帮助不大|商业价值较低|仅展示|仅停留)",
        r"直接命中[^。；;]{0,24}痛点",
    )
    return bool(re.search("|".join(positive_patterns), text, flags=re.IGNORECASE))



def creator_has_usage_demo(text: str) -> bool:
    return bool(re.search(r"按压|pump|pam|泵|刷牙|brush|一次|用量|挤|操作|演示|不浪费|membazir|guna", text, flags=re.IGNORECASE))


def creator_has_functional_effect(text: str) -> bool:
    return bool(re.search(r"浪费|不浪费|membazir|bazir|按压|pump|pam|泵|一次|用量|省|方便|cukup|senang", text, flags=re.IGNORECASE))


def mentions_sensory_gap(text: str) -> bool:
    return bool(re.search(r"闻|香|气味|口味|水果味|wangi|bau|感官|体验|嗅", text, flags=re.IGNORECASE))


def sensory_effect_gap(text: str, creator_text: str, benchmark_text: str) -> bool:
    combined = text + benchmark_text
    creator_has_sensory = mentions_sensory_gap(creator_text)
    benchmark_has_sensory = mentions_sensory_gap(benchmark_text)
    gap_mentions_missing = bool(re.search(r"缺失|没有|未展示|仅停留|削弱|大打折扣|无法", text, flags=re.IGNORECASE))
    return benchmark_has_sensory and not creator_has_sensory and gap_mentions_missing and mentions_sensory_gap(combined)


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
    """
    cleaned = _NEG_ENDORSEMENT_RE.sub("", str(text or ""))
    return bool(_ENDORSEMENT_RE.search(cleaned))


def creator_has_cta(text: str) -> bool:
    return bool(re.search(r"买|购买|下单|小黄车|黄色|购物车|beg|kuning|grab|beli|direct|link|cart", text, flags=re.IGNORECASE))


def set_stage_small(stage: dict[str, Any], gap: str | None = None, summary: str | None = None) -> None:
    stage["severity"] = "small"
    if gap:
        stage["gap"] = gap
    if summary:
        stage["gap_summary"] = [summary]

# endregion


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
                stage[f"{role}_support_status"] = "visual_only"
                continue
            reliable_unit = referenced_spoken_unit(result, stage, role)
            if reliable_unit:
                stage[f"{role}_quote"] = str(reliable_unit.get("voiceover") or "")
                stage[f"{role}_quote_zh"] = str(reliable_unit.get("voiceover_zh") or "")
                continue
            segments = read_srt_segments(videos.get(role, {}))
            if not segments:
                continue
            start, end = parse_time_range_seconds(stage.get(f"{role}_time_range"), None)
            text = " ".join(
                segment["text"]
                for segment in segments
                if min(end, segment["end"]) > max(start, segment["start"])
            ).strip()
            old_text = str(stage.get(f"{role}_quote") or "").strip()
            stage[f"{role}_quote"] = text
            if text != old_text:
                stage[f"{role}_quote_zh"] = ""


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
    prompt = " ".join(str(item.get(key) or "") for key in ("suggestion", "aigc_prompt"))
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
        start, _ = parse_time_range_seconds(chosen.get("time_range"), None)
        item["best_base_frame_time"] = format_seconds(start)
    visible_fact = str(chosen.get("visual_fact") or chosen.get("information") or "").strip()
    item["base_frame_reason"] = f"来自达人 {item['base_frame_evidence_id']} 的真实素材：{visible_fact}"

# endregion


# region reconcile -----------------------------------------------------------

def reconcile_unsupported_cta(result: dict[str, Any]) -> None:
    """S6 没有可识别的购买指令时，写入占位 evidence_unit，避免后续校验把缺失误判为通过。"""
    stages = result.get("stage_analysis", [])
    if len(stages) < 6:
        return
    cta = stages[5]
    for role, code in (("benchmark", "B"), ("creator", "C")):
        quote = str(cta.get(f"{role}_quote") or "")
        if re.search(r"\b(beli|troli|klik|cart|checkout|order|link|shop)\b|购买|下单|购物车|点击", quote, flags=re.IGNORECASE):
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
        units = result.get("video_understanding", {}).get(role, {}).get("evidence_units", [])
        ensure_evidence_unit(units, placeholder)
        cta[f"{role}_evidence_ids"] = [unit_id]
        cta[f"{role}_key_message"] = placeholder["information"]
        cta[f"{role}_summary"] = placeholder["information"]
        cta[f"{role}_quote"] = ""
        cta[f"{role}_quote_zh"] = ""
        cta[f"{role}_visual_evidence"] = [placeholder["visual_fact"]]
        cta[f"{role}_support_status"] = "visual_only"

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

def fill_missing_evidence_references(result: dict[str, Any]) -> None:
    """阶段引用的 evidence_unit 不在该阶段时间内时，补占位单元或就近匹配。

    TODO 原归属"证据校验"章节，但实际是 fill 行为不抛 SystemExit，按用户拆分约束 #7 归 repair。
    """
    understanding = result.get("video_understanding", {})
    for index, stage in enumerate(result.get("stage_analysis", []), start=1):
        for role, code in (("benchmark", "B"), ("creator", "C")):
            key = f"{role}_evidence_ids"
            units = understanding.get(role, {}).get("evidence_units", [])
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


# region downgrade -----------------------------------------------------------

def downgrade_unverified_sensitive_claims(result: dict[str, Any]) -> None:
    """口播提及年龄或口腔护理但画面未验证时，把状态降为 voice_only 并打提示。"""
    patterns = r"\b\d+\s*(?:hingga|-)\s*\d+\s*tahun\b|anti.?car|防蛀|适合.{0,5}\d+\s*(?:到|-)\s*\d+\s*岁"
    for stage in result.get("stage_analysis", []):
        for role in ("benchmark", "creator"):
            quote = str(stage.get(f"{role}_quote") or "")
            quote_zh = str(stage.get(f"{role}_quote_zh") or "")
            if not re.search(patterns, f"{quote} {quote_zh}", flags=re.IGNORECASE):
                continue
            stage[f"{role}_support_status"] = "voice_only"
            note = "口播提及年龄或口腔护理相关主张；当前关键帧未核验包装信息。"
            facts = [str(value) for value in stage.get(f"{role}_visual_evidence", []) if str(value).strip()]
            stage[f"{role}_visual_evidence"] = list(dict.fromkeys([note, *facts]))

# endregion


# region derive --------------------------------------------------------------

def derive_product_visibility(result: dict[str, Any], analysis: dict[str, Any]) -> None:
    """从达人 evidence_units 的产品出镜标记确定性累加 product_visibility，覆盖模型估值。

    口径：
    - 只统计达人(creator)视频——product_visibility 描述的就是被改进的达人片段。
    - product_visible=True 或 product_coverage∈{low,medium,high} 视为该时段产品在画面内。
    - total_screen = 各出镜时段时长之和（evidence_units 沿时间线非重叠，直接累加并截到时长内）。
    - ratio = total_screen / 视频时长，与 validate_product_visibility 校验口径一致。
    优雅降级：缺时长或一个出镜标记都没有时不覆盖，沿用模型估算值，避免误判为产品全程缺席。
    """
    creator = result.get("video_understanding", {}).get("creator", {})
    units = creator.get("evidence_units", []) if isinstance(creator, dict) else []
    raw_duration = analysis.get("videos", {}).get("creator", {}).get("duration_seconds")
    duration = float(raw_duration) if isinstance(raw_duration, (int, float)) and raw_duration else 0.0
    if duration <= 0:
        return

    visible_spans: list[tuple[float, float]] = []
    for unit in units if isinstance(units, list) else []:
        if not isinstance(unit, dict) or not unit_product_visible(unit):
            continue
        start, end = parse_time_range_seconds(unit.get("time_range"), duration)
        if end > start:
            visible_spans.append((start, end))
    if not visible_spans:
        return

    first_appearance = min(max(0.0, start) for start, _ in visible_spans)
    total_screen = min(sum(end - start for start, end in visible_spans), duration)
    result["product_visibility"] = {
        "first_appearance_sec": round(min(first_appearance, duration), 2),
        "total_screen_time_sec": round(total_screen, 2),
        "video_duration_sec": round(duration, 2),
        "ratio": round(total_screen / duration, 3),
        "estimation_note": (
            f"由达人 {len(visible_spans)} 个标记产品出镜的 evidence_unit 时段累加确定性算得，非模型估算。"
        ),
    }


def unit_product_visible(unit: dict[str, Any]) -> bool:
    if bool(unit.get("product_visible")):
        return True
    return str(unit.get("product_coverage") or "").strip().lower() in {"low", "medium", "high"}

# endregion


# region brand / timing 归一 -------------------------------------------------

def remove_unverified_brand_models(result: dict[str, Any], analysis: dict[str, Any]) -> None:
    """删除未被产品上下文或转写支持的英文品牌型号，避免模型幻觉品牌。"""
    allowed_text = "\n".join(allowed_claim_sources(analysis)).lower()
    product_name = str(analysis.get("product", {}).get("name") or "该产品").strip() or "该产品"
    local_product_name = local_product_reference(analysis, product_name)

    def clean_text(value: Any, key: str = "") -> Any:
        if not isinstance(value, str):
            return value
        replacement = local_product_name if key == "creator_script" else product_name
        return re.sub(
            r"\b[A-Z][A-Za-z]{2,}(?:\s+[A-Z]?\d+[A-Za-z0-9-]*)+\b",
            lambda match: match.group(0) if match.group(0).lower() in allowed_text else replacement,
            value,
        )

    def clean_object(value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {child_key: clean_object(item, child_key) for child_key, item in value.items()}
        if isinstance(value, list):
            return [clean_object(item, key) for item in value]
        return clean_text(value, key)

    result["video_understanding"] = clean_object(result.get("video_understanding", {}))
    result["stage_analysis"] = clean_object(result.get("stage_analysis", []))
    result["improvements"] = clean_object(result.get("improvements", []))
    result["executive_summary"] = clean_text(result.get("executive_summary", ""))


def local_product_reference(analysis: dict[str, Any], fallback: str) -> str:
    """根据达人语言返回中性产品指代，避免 creator_script 出现中文品牌。"""
    creator = analysis.get("videos", {}).get("creator", {})
    language = str(creator.get("detected_language") or creator.get("transcription_language") or "").lower()
    if language == "ms":
        return "produk ini"
    if language == "id":
        return "produk ini"
    if language == "th":
        return "ผลิตภัณฑ์นี้"
    return fallback


def allowed_claim_sources(analysis: dict[str, Any]) -> list[str]:
    """收集所有可信文本来源（产品输入 + 转写 + 中文翻译），用于品牌型号白名单。"""
    sources = [
        str(analysis.get("product", {}).get("name") or ""),
        str(analysis.get("product", {}).get("notes") or ""),
    ]
    for info in analysis.get("videos", {}).values():
        transcript_path = info.get("transcript_path")
        if transcript_path and Path(str(transcript_path)).exists():
            sources.append(Path(str(transcript_path)).read_text(encoding="utf-8", errors="ignore"))
        zh_path = Path(str(info.get("work_dir", ""))) / "transcript.zh.txt"
        if zh_path.exists():
            sources.append(zh_path.read_text(encoding="utf-8", errors="ignore"))
    return sources


def clamp_result_time_ranges(result: dict[str, Any], analysis: dict[str, Any]) -> None:
    """把所有 time_range 截到对应视频时长内，防止越界帧选取。"""
    videos = analysis.get("videos", {})
    benchmark_duration = videos.get("benchmark", {}).get("duration_seconds")
    creator_duration = videos.get("creator", {}).get("duration_seconds")
    understanding = result.get("video_understanding", {})
    if isinstance(understanding, dict):
        for role, duration in (("benchmark", benchmark_duration), ("creator", creator_duration)):
            role_result = understanding.get(role, {})
            if isinstance(role_result, dict):
                for unit in role_result.get("evidence_units", []):
                    if isinstance(unit, dict):
                        unit["time_range"] = bounded_time_range(unit.get("time_range"), duration)
    for stage in result.get("stage_analysis", []):
        stage["benchmark_time_range"] = bounded_time_range(stage.get("benchmark_time_range"), benchmark_duration)
        stage["creator_time_range"] = bounded_time_range(stage.get("creator_time_range"), creator_duration)
        stage["time_range"] = f"标杆 {stage['benchmark_time_range']} / 达人 {stage['creator_time_range']}"
    for item in result.get("improvements", []):
        item["benchmark_time_range"] = bounded_time_range(item.get("benchmark_time_range"), benchmark_duration)
        item["creator_time_range"] = bounded_time_range(item.get("creator_time_range"), creator_duration)
        item["time_range"] = item["creator_time_range"]
        best_time = parse_timestamp_seconds(item.get("best_base_frame_time"))
        if best_time is not None and isinstance(creator_duration, (int, float)):
            item["best_base_frame_time"] = format_seconds(min(max(0.0, best_time), float(creator_duration)))


def bounded_time_range(value: Any, duration: Any) -> str:
    start, end = parse_time_range_seconds(value, duration)
    return f"{format_seconds(start)} - {format_seconds(end)}"

# endregion
