"""flayr_core.llm.media：多模态 LLM 输入素材选择。

只负责把本地帧、timeline view 和 evidence 感官切片转成 chat payload 可用的
image_url / input_audio / video_url 块；不写 prompt，不碰业务判断规则。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..artifacts import (
    format_seconds,
    get_focus_frame_entries,
    get_frame_entries,
    parse_time_range_seconds,
    sample_evenly,
    select_frames_for_time_range,
)
from .api import (
    audio_to_mp3_data_url,
    image_to_data_url,
    is_agent_plan_api_url,
    video_to_data_url,
)


AGENT_PLAN_MAX_VIDEO_BLOCKS_PER_ROLE = 5


def select_role_visual_inputs(info: dict[str, Any], role: str, image_limit: int) -> list[dict[str, str]]:
    """为单视频事实抽取选关键帧，最多 image_limit 张。"""
    selected: list[dict[str, str]] = []
    for entry in get_llm_visual_candidates(info, image_limit):
        frame = Path(str(entry.get("path", "")))
        if not frame.exists():
            continue
        timestamp = format_seconds(entry.get("timestamp_seconds")) if entry.get("timestamp_seconds") is not None else ""
        marker = f" @ {timestamp}" if timestamp else ""
        selected.append(
            {
                "role": role,
                "path": str(frame),
                "label": f"{role} {entry.get('stage') or entry.get('label', 'frame')}{marker} {frame.name}",
                "data_url": image_to_data_url(frame),
            }
        )
    return selected[:image_limit]


def get_llm_visual_candidates(info: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """候选视觉输入：先给 Hook/CTA timeline view，再补原始帧。"""
    if limit <= 0:
        return []
    timeline_limit = min(2, max(0, limit // 3))
    timeline_entries = get_timeline_view_entries(info)[:timeline_limit]
    remaining = max(0, limit - len(timeline_entries))
    used = {str(entry.get("path") or "") for entry in timeline_entries}
    frame_entries = [
        entry for entry in get_llm_frame_candidates(info, remaining)
        if str(entry.get("path") or "") not in used
    ]
    return timeline_entries + frame_entries


def get_timeline_view_entries(info: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = info.get("video_evidence")
    views = evidence.get("timeline_views") if isinstance(evidence, dict) else None
    entries: list[dict[str, Any]] = []
    if isinstance(views, list):
        for item in views:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "")
            if path:
                entries.append(
                    {
                        "label": f"{item.get('label') or 'timeline'} timeline",
                        "path": path,
                        "timestamp_seconds": None,
                    }
                )
    if entries:
        return entries

    work_dir = Path(str(info.get("work_dir") or ""))
    timeline_dir = work_dir / "timeline_views"
    if not timeline_dir.is_dir():
        return []
    for label in ("hook", "cta"):
        path = timeline_dir / f"{label}.jpg"
        if path.is_file():
            entries.append({"label": f"{label} timeline", "path": str(path), "timestamp_seconds": None})
    return entries


def get_llm_frame_candidates(info: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """从一个视频的全片帧 + 加密 focus 帧中选候选帧。"""
    if limit <= 0:
        return []
    focus_limit = 2 if limit >= 6 else 0
    timeline_limit = max(1, limit - focus_limit)
    timeline_entries = sample_evenly(get_frame_entries(info), timeline_limit)
    focus_entries = sample_evenly(get_focus_frame_entries(info), focus_limit)
    by_second: dict[float, dict[str, Any]] = {}
    for entry in timeline_entries:
        if not str(entry.get("path") or ""):
            continue
        by_second.setdefault(round(float(entry.get("timestamp_seconds") or 0.0), 1), entry)
    for entry in focus_entries:
        if not str(entry.get("path") or ""):
            continue
        by_second[round(float(entry.get("timestamp_seconds") or 0.0), 1)] = entry
    return sorted(by_second.values(), key=lambda item: float(item.get("timestamp_seconds") or 0.0))


def build_evidence_sensory_inputs(
    analysis: dict[str, Any],
    facts: dict[str, Any],
    frames_per_unit: int = 1,
    window_end_seconds: float | None = None,
    api_url: str = "",
) -> list[dict[str, Any]]:
    """为阶段二对比判断准备每条 evidence_unit 的感官证据。"""
    content: list[dict[str, Any]] = []
    use_native_clip = is_agent_plan_api_url(api_url)
    videos = analysis.get("videos", {})
    for role in ("benchmark", "creator"):
        role_facts = facts.get(role) or {}
        units = role_facts.get("evidence_units") or []
        info = videos.get(role) or {}
        video_path = Path(str(info.get("path") or ""))
        audio_path = Path(str(info.get("work_dir") or "")) / "audio.wav"
        duration = info.get("duration_seconds")
        prepared_units = _prepare_evidence_windows(units, duration, window_end_seconds)
        if use_native_clip:
            prepared_units = _merge_evidence_windows(
                prepared_units,
                AGENT_PLAN_MAX_VIDEO_BLOCKS_PER_ROLE,
            )
        for unit in prepared_units:
            uid = str(unit["label"])
            start = float(unit["start"])
            end = float(unit["end"])
            clipped_range = f"{start:.2f}s - {end:.2f}s"
            label = f"{role} {uid} @ {clipped_range}"
            if use_native_clip:
                clip = video_to_data_url(
                    video_path,
                    fps=3.0,
                    max_width=480,
                    start=start,
                    duration=max(0.1, end - start),
                    max_data_bytes=8 * 1024 * 1024,
                )
                if clip is not None:
                    content.append({"type": "text", "text": f"【{label}｜连续画面与该时段原声】"})
                    content.append({"type": "video_url", "video_url": {"url": clip}})
                    continue
            frames = select_frames_for_time_range(info, clipped_range, limit=frames_per_unit)
            for fr in frames:
                frame_path = Path(str(fr.get("path") or ""))
                if not frame_path.is_file():
                    continue
                content.append({"type": "text", "text": f"【{label}｜画面帧】"})
                content.append(
                    {"type": "image_url", "image_url": {"url": image_to_data_url(frame_path), "detail": "low"}}
                )
            seg = None if use_native_clip else audio_to_mp3_data_url(
                audio_path, start=start, duration=max(0.1, end - start)
            )
            if seg is not None:
                content.append({"type": "text", "text": f"【{label}｜该时段音频】"})
                content.append({"type": "input_audio", "input_audio": {"data": seg, "format": "mp3"}})
    return content


def _prepare_evidence_windows(
    units: list[dict[str, Any]],
    duration: Any,
    window_end_seconds: float | None,
) -> list[dict[str, Any]]:
    """把 evidence unit 归一为有序时间窗，保留 ID 与原始时间段。"""
    prepared: list[dict[str, Any]] = []
    for unit in units:
        if not isinstance(unit, dict):
            continue
        uid = str(unit.get("id") or "")
        time_range = str(unit.get("time_range") or "")
        if not uid or not time_range:
            continue
        start, end = parse_time_range_seconds(time_range, duration)
        if window_end_seconds is not None:
            if start >= window_end_seconds:
                continue
            end = min(end, window_end_seconds)
        prepared.append(
            {
                "label": f"{uid}({start:.2f}-{end:.2f}s)",
                "start": start,
                "end": end,
            }
        )
    return sorted(prepared, key=lambda item: (float(item["start"]), float(item["end"])))


def _merge_evidence_windows(
    windows: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    """均衡合并相邻窗口，在不丢 evidence 的前提下满足供应商视频块上限。"""
    if limit <= 0 or len(windows) <= limit:
        return windows
    merged: list[dict[str, Any]] = []
    total = len(windows)
    for index in range(limit):
        start_index = index * total // limit
        end_index = (index + 1) * total // limit
        group = windows[start_index:end_index]
        merged.append(
            {
                "label": "+".join(str(item["label"]) for item in group),
                "start": min(float(item["start"]) for item in group),
                "end": max(float(item["end"]) for item in group),
            }
        )
    return merged


def _merge_short_evidence_windows(
    windows: list[dict[str, Any]],
    minimum_seconds: float,
) -> list[dict[str, Any]]:
    """Merge sub-minimum adjacent units so providers never receive invalid tiny clips."""
    merged: list[dict[str, Any]] = []
    index = 0
    while index < len(windows):
        current = dict(windows[index])
        if float(current["end"]) - float(current["start"]) >= minimum_seconds:
            merged.append(current)
            index += 1
            continue
        if index + 1 < len(windows):
            following = windows[index + 1]
            current["label"] = f"{current['label']}+{following['label']}"
            current["end"] = max(float(current["end"]), float(following["end"]))
            merged.append(current)
            index += 2
            continue
        if merged:
            merged[-1]["label"] = f"{merged[-1]['label']}+{current['label']}"
            merged[-1]["end"] = max(float(merged[-1]["end"]), float(current["end"]))
        else:
            merged.append(current)
        index += 1
    return merged

