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
from ..llm.parse import is_effective_voiceover
from .utils import (
    ensure_evidence_unit,
    evidence_mentions_product,
    evidence_overlaps_range,
    evidence_unit_at_time,
    nearest_evidence_unit,
    nearest_product_evidence_unit,
    read_srt_segments,
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
    """S6 没有可识别的购买指令时写占位；字幕/画面驱动 CTA 不得被空 quote 覆盖。"""
    stages = result.get("stage_analysis", [])
    if len(stages) < 6:
        return
    cta = stages[5]
    for role, code in (("benchmark", "B"), ("creator", "C")):
        quote = str(cta.get(f"{role}_quote") or "")
        flag = cta.get(f"{role}_s6") if isinstance(cta.get(f"{role}_s6"), dict) else {}
        units = result.get("video_understanding", {}).get(role, {}).get("evidence_units", [])
        flag_ids = [str(value) for value in flag.get("evidence_ids", []) if str(value).strip()]
        referenced = [
            unit for unit in units
            if isinstance(unit, dict) and str(unit.get("id")) in flag_ids and "_NO_CTA" not in str(unit.get("id"))
        ]
        referenced_text = json.dumps(referenced, ensure_ascii=False)
        has_positive_flag = flag.get("exists") is True and flag.get("direct_order_met") is True and bool(referenced)
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
