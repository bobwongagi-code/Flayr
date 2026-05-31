"""flayr_core.postprocess.utils：postprocess 包内最底层的工具集。

所有"对 evidence_units / SRT / time_range 做读取或定位"的纯工具函数。
不包含任何业务规则、不抛 SystemExit、不修改业务语义。

依赖方向：本模块只依赖外部 (artifacts + llm.parse)，不依赖 postprocess 包内任何上层模块。
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
from ..llm.parse import is_effective_voiceover


# ---------------------------------------------------------------------------
# SRT 转写读取
# ---------------------------------------------------------------------------

def read_srt_segments(info: dict[str, Any]) -> list[dict[str, Any]]:
    configured = str(info.get("transcript_segments_path") or "").strip()
    path = Path(configured) if configured else Path("__missing_transcript_segments__")
    if not path.is_file():
        work_dir = Path(str(info.get("work_dir") or ""))
        path = work_dir / "transcript.srt"
    if not path.is_file():
        return []
    blocks = re.split(r"\n\s*\n", path.read_text(encoding="utf-8", errors="ignore").strip())
    segments: list[dict[str, Any]] = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        time_line = next((line for line in lines if "-->" in line), "")
        if not time_line:
            continue
        start_text, end_text = [item.strip() for item in time_line.split("-->", 1)]
        text_lines = lines[lines.index(time_line) + 1 :]
        if not text_lines:
            continue
        segments.append(
            {
                "start": parse_srt_timestamp(start_text),
                "end": parse_srt_timestamp(end_text),
                "text": " ".join(text_lines),
            }
        )
    return segments


def parse_srt_timestamp(value: str) -> float:
    normalized = value.replace(",", ".")
    parts = normalized.split(":")
    if len(parts) != 3:
        return 0.0
    return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])


# ---------------------------------------------------------------------------
# evidence_unit 查找与赋值
# ---------------------------------------------------------------------------

def find_evidence_unit(units: list[Any], pattern: str) -> dict[str, Any] | None:
    for unit in units:
        if not isinstance(unit, dict):
            continue
        text = " ".join(str(unit.get(key) or "") for key in ("information", "voiceover", "voiceover_zh"))
        if re.search(pattern, text, flags=re.IGNORECASE):
            return unit
    return None


def referenced_spoken_unit(result: dict[str, Any], stage: dict[str, Any], role: str) -> dict[str, Any] | None:
    references = {str(item) for item in stage.get(f"{role}_evidence_ids", [])}
    units = result.get("video_understanding", {}).get(role, {}).get("evidence_units", [])
    for unit in units:
        if (
            isinstance(unit, dict)
            and str(unit.get("id")) in references
            and not str(unit.get("id")).endswith("_NO_USAGE")
            and "_STAGE_" not in str(unit.get("id"))
            and is_effective_voiceover(unit.get("voiceover"))
            and evidence_overlaps_range(unit, stage.get(f"{role}_time_range"))
        ):
            return unit
    return None


def assign_benchmark_unit(stage: dict[str, Any], unit: dict[str, Any]) -> None:
    stage["benchmark_time_range"] = str(unit.get("time_range") or stage.get("benchmark_time_range") or "")
    stage["benchmark_key_message"] = str(unit.get("information") or stage.get("benchmark_key_message") or "")
    stage["benchmark_summary"] = str(unit.get("information") or stage.get("benchmark_summary") or "")
    stage["benchmark_evidence_ids"] = [str(unit.get("id"))]
    stage["benchmark_quote"] = str(unit.get("voiceover") or "")
    stage["benchmark_quote_zh"] = str(unit.get("voiceover_zh") or "")
    stage["benchmark_visual_evidence"] = [
        value
        for value in (str(unit.get("visual_fact") or "").strip(), str(unit.get("subtitle_fact") or "").strip())
        if value
    ]
    stage["benchmark_support_status"] = "voice_only" if is_effective_voiceover(unit.get("voiceover")) else "visual_only"


def first_unmapped_overlapping_unit(
    units: list[Any],
    mapped_ids: set[str],
    time_range: Any,
) -> dict[str, Any] | None:
    for unit in units:
        if isinstance(unit, dict) and str(unit.get("id")) not in mapped_ids and evidence_overlaps_range(unit, time_range):
            return unit
    return None


def adjacent_review_range(before: dict[str, Any] | None, after: dict[str, Any] | None, fallback: Any) -> str:
    if before and after:
        _, before_end = parse_time_range_seconds(before.get("time_range"), None)
        after_start, _ = parse_time_range_seconds(after.get("time_range"), None)
        if after_start >= before_end:
            return f"{format_seconds(before_end)} - {format_seconds(max(before_end + 0.5, after_start))}"
    return str(fallback or "")


def ensure_evidence_unit(units: list[Any], new_unit: dict[str, Any]) -> None:
    units[:] = [unit for unit in units if not isinstance(unit, dict) or str(unit.get("id")) != str(new_unit.get("id"))]
    units.append(new_unit)


# ---------------------------------------------------------------------------
# 时间点 / 时间区间 与 evidence_unit 的位置关系
# ---------------------------------------------------------------------------

def evidence_unit_at_time(units: list[Any], timestamp: Any) -> dict[str, Any] | None:
    seconds = parse_timestamp_seconds(timestamp)
    if seconds is None:
        return None
    for unit in units:
        if not isinstance(unit, dict):
            continue
        start, end = parse_time_range_seconds(unit.get("time_range"), None)
        if start <= seconds <= end:
            return unit
    return None


def evidence_mentions_product(unit: dict[str, Any] | None) -> bool:
    if not isinstance(unit, dict):
        return False
    text = " ".join(str(unit.get(key) or "") for key in ("visual_fact", "subtitle_fact"))
    return bool(re.search(r"产品|包装|瓶|牙膏|泵|product|botol|pump|ubat gigi|supplement|vitamin", text, flags=re.IGNORECASE))


def nearest_product_evidence_unit(units: list[Any], timestamp: Any) -> dict[str, Any] | None:
    seconds = parse_timestamp_seconds(timestamp)
    candidates = [unit for unit in units if isinstance(unit, dict) and evidence_mentions_product(unit)]
    if not candidates:
        return None
    if seconds is None:
        return candidates[0]
    return min(
        candidates,
        key=lambda unit: distance_to_time_range(seconds, unit.get("time_range")),
    )


def distance_to_time_range(seconds: float, time_range: Any) -> float:
    start, end = parse_time_range_seconds(time_range, None)
    if start <= seconds <= end:
        return 0.0
    return min(abs(seconds - start), abs(seconds - end))


def nearest_evidence_unit(units: Any, time_range: Any) -> dict[str, Any] | None:
    if not isinstance(units, list):
        return None
    target_start, target_end = parse_time_range_seconds(time_range, None)
    target_midpoint = (target_start + target_end) / 2
    candidates: list[tuple[float, dict[str, Any]]] = []
    for unit in units:
        if not isinstance(unit, dict) or not unit.get("id"):
            continue
        start, end = parse_time_range_seconds(unit.get("time_range"), None)
        overlap = max(0.0, min(target_end, end) - max(target_start, start))
        midpoint_distance = abs(((start + end) / 2) - target_midpoint)
        candidates.append((-overlap if overlap > 0 else midpoint_distance, unit))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


def evidence_overlaps_range(unit: dict[str, Any], time_range: Any) -> bool:
    target_start, target_end = parse_time_range_seconds(time_range, None)
    start, end = parse_time_range_seconds(unit.get("time_range"), None)
    return min(target_end, end) > max(target_start, start)
