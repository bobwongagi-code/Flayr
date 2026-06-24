"""flayr_core.postprocess.repair_stages：阶段归属与差距等级的确定性校准。

从 repair.py 按 region 簇拆出（2026-06-15，零跨模块依赖）：
  - align_*       按关键词/时间戳把 evidence 归到正确阶段
  - stabilize_*   对 LLM 容易漂移的阶段差距等级做兜底校准
另含这两簇共享的小工具（stage_code/stage_text/has_real_endorsement 等）。
所有函数都是"修改 result data 后正常返回"，不抛 SystemExit。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..artifacts import format_seconds
from ..llm.parse import STAGES
from .utils import (
    adjacent_review_range,
    assign_benchmark_unit,
    ensure_evidence_unit,
    find_evidence_unit,
    first_unmapped_overlapping_unit,
    read_srt_segments,
)


# region align ---------------------------------------------------------------

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
    """校准容易跨阶段漂移的 severity（4d 后为兜底层）。

    2026-06-11 按 TODO #1 处置清单执行去过拟合：S3/S4 牙膏三件套（按压/闻香/功能效果正则）
    已删——kakwan S3 误触发实锤 + round4 验证 derive 用执行分正确处理同品类（tasha S4 large 3/5）。
    保留的规则只在 derive 跳过时（执行分缺失的旧数据/降级路径）作为最终值生效：
    - 达人持平或优于标杆时，severity 不应超过 small（极性兜底，文本正则版）；
    - S5 双方均无真背书 → 均未涉及（与 derive S5 门槛同源）；
    - 标杆没有 CTA 而达人有购买指令时，S6 不应被"不够强促销"惩罚。
    """
    creator_global_has_cta = role_has_positive_cta(result, "creator")
    benchmark_global_has_cta = role_has_positive_cta(result, "benchmark")

    for stage in result.get("stage_analysis", []):
        stage_id = stage_code(stage)
        text = stage_text(stage)

        if creator_not_worse(text):
            set_stage_small(stage)

        # S3/S4 牙膏三件套已删（2026-06-11，TODO #1 处置清单）：按压/闻香/功能效果正则
        # 是对单一牙膏样本长出的过拟合，kakwan"按压按钮"误触发实锤；该判断现由
        # derive.py 的执行分 + S4 演示差分承担（round4 tasha S4 实证）。
        # S2 不再无条件兜底 small：双方都完成产品引出不代表卖点质量相当，
        # 需由 LLM 按品类消费者决策优先级判断卖点选择是否偏离。

        # S5「双方均无背书 → small」闸已移交 derive（2026-06-23）：derive 用结构化 flag
        # （endorsement_verbal/visual，缺失退 has_real_endorsement 正则兜底）做唯一判定。
        # 此处旧正则闸删除——它在 derive 之前跑、设 small + gap_summary，derive 后跑覆盖 severity
        # 却不改 gap_summary，两者一旦不一致（正则 vs flag）会产出「severity≠解释」的自相矛盾。

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
