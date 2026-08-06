"""flayr_core.postprocess.repair_claims：主张降级、产品出镜累加、品牌清洗与时间归一。

从 repair.py 按 region 簇拆出（2026-06-15，零跨模块依赖）：
  - downgrade_*   未验证敏感主张降级 voice_only
  - derive_product_visibility  达人产品出镜标记确定性累加
  - 品牌/型号清洗 + 时间归一（行为同样是修改 data 后返回）
普通报告字段修补会正常返回；active Stage1 事实如果出现非法时间范围则 fail closed，抛出
`SystemExit`，防止后处理把坏事实修成看似合法的数据。
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

from ..artifacts import (
    format_seconds,
    parse_time_range_seconds,
    parse_timestamp_seconds,
)
from ..stage_evidence_contracts import (
    STAGE_EVIDENCE_CONTRACT_VERSION,
    STAGE1_IMMUTABLE_FIELDS,
    qualified_stage_evidence_units,
)


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
    units = qualified_stage_evidence_units(creator) if isinstance(creator, dict) else []
    raw_duration = analysis.get("videos", {}).get("creator", {}).get("duration_seconds")
    duration = parse_timestamp_seconds(raw_duration)
    if duration is None or duration <= 0:
        return

    visible_spans: list[tuple[float, float]] = []
    for unit in units if isinstance(units, list) else []:
        if not isinstance(unit, dict) or not unit_product_visible(unit):
            continue
        parsed = parse_time_range_seconds(unit.get("time_range"), duration)
        if parsed is None:
            continue
        start, end = parsed
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

    understanding = result.get("video_understanding", {})
    cleaned_understanding = clean_object(understanding)
    # Active Stage1 evidence is locked before postprocess. Brand/model cleanup
    # may rewrite report prose, but it must never rewrite evidence content,
    # timestamps, or attribution after the lock.
    if isinstance(understanding, dict) and isinstance(cleaned_understanding, dict):
        for role in ("benchmark", "creator"):
            original_side = understanding.get(role)
            cleaned_side = cleaned_understanding.get(role)
            if (
                isinstance(original_side, dict)
                and original_side.get("stage_evidence_contract_version") == STAGE_EVIDENCE_CONTRACT_VERSION
                and isinstance(cleaned_side, dict)
            ):
                cleaned_side["evidence_units"] = copy.deepcopy(original_side.get("evidence_units") or [])
                cleaned_side["stage_evidence_checks"] = copy.deepcopy(original_side.get("stage_evidence_checks") or [])
                for key in (*STAGE1_IMMUTABLE_FIELDS,
                    "evidence_set_version",
                    "evidence_set_sha256",
                    "evidence_set_status",
                    "evidence_set_source",
                    "stage_evidence_contract_version",
                ):
                    if key in original_side:
                        cleaned_side[key] = copy.deepcopy(original_side[key])
    result["video_understanding"] = cleaned_understanding
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
    """Canonicalize valid time ranges; invalid evidence is cleared, never repaired."""
    videos = analysis.get("videos", {})
    benchmark_duration = videos.get("benchmark", {}).get("duration_seconds")
    creator_duration = videos.get("creator", {}).get("duration_seconds")
    understanding = result.get("video_understanding", {})
    if isinstance(understanding, dict):
        for role, duration in (("benchmark", benchmark_duration), ("creator", creator_duration)):
            role_result = understanding.get(role, {})
            if isinstance(role_result, dict):
                active_contract = (
                    isinstance(role_result, dict)
                    and role_result.get("stage_evidence_contract_version") == STAGE_EVIDENCE_CONTRACT_VERSION
                )
                for unit in role_result.get("evidence_units", []):
                    if not isinstance(unit, dict):
                        continue
                    if active_contract:
                        # A locked Stage1 unit is an observation, not a report
                        # field. Invalid/out-of-bounds input fails closed; a
                        # valid range is left byte-for-byte untouched.
                        if parse_time_range_seconds(unit.get("time_range"), duration) is None:
                            raise SystemExit(
                                f"{role} Stage1 evidence {unit.get('id')} has invalid time_range after lock."
                            )
                        continue
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
        duration_value = parse_timestamp_seconds(creator_duration)
        if best_time is None or duration_value is None or best_time > duration_value:
            item["best_base_frame_time"] = ""
        else:
            item["best_base_frame_time"] = format_seconds(best_time)


def bounded_time_range(value: Any, duration: Any) -> str:
    parsed = parse_time_range_seconds(value, duration)
    if parsed is None:
        return ""
    start, end = parsed
    return f"{format_seconds(start)} - {format_seconds(end)}"

# endregion
