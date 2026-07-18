"""flayr_core.llm.media：多模态 LLM 输入素材选择。

只负责把本地帧、timeline view 和 evidence 切片音频转成 chat payload 可用的
image_url / input_audio 块；不写 prompt，不碰业务判断规则。
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
from .api import audio_to_mp3_data_url, image_to_data_url


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


def select_llm_visual_inputs(analysis: dict[str, Any], image_limit: int) -> list[dict[str, str]]:
    """跨视频选关键帧（同时含 benchmark 和 creator）。"""
    if image_limit <= 0:
        return []

    videos = analysis.get("videos", {})
    roles = [role for role in ("benchmark", "creator") if role in videos]
    if not roles:
        return []

    per_role_limit = max(1, image_limit // len(roles))
    selected: list[dict[str, str]] = []
    for role in roles:
        entries = get_llm_visual_candidates(videos[role], per_role_limit)
        for entry in entries[:per_role_limit]:
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

    if len(selected) < image_limit:
        used_paths = {item["path"] for item in selected}
        for role in roles:
            entries = get_llm_visual_candidates(videos[role], image_limit)
            for entry in entries:
                if len(selected) >= image_limit:
                    break
                frame = Path(str(entry.get("path", "")))
                if not frame.exists():
                    continue
                if str(frame) in used_paths:
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
                used_paths.add(str(frame))

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
) -> list[dict[str, Any]]:
    """为阶段二对比判断准备每条 evidence_unit 的关键帧 + 切片音频。"""
    content: list[dict[str, Any]] = []
    videos = analysis.get("videos", {})
    for role in ("benchmark", "creator"):
        role_facts = facts.get(role) or {}
        units = role_facts.get("evidence_units") or []
        info = videos.get(role) or {}
        audio_path = Path(str(info.get("work_dir") or "")) / "audio.wav"
        duration = info.get("duration_seconds")
        for unit in units:
            uid = str(unit.get("id") or "")
            time_range = str(unit.get("time_range") or "")
            if not uid or not time_range:
                continue
            start, end = parse_time_range_seconds(time_range, duration)
            if window_end_seconds is not None:
                if start >= window_end_seconds:
                    continue
                end = min(end, window_end_seconds)
            clipped_range = f"{start:.2f}s - {end:.2f}s"
            label = f"{role} {uid} @ {clipped_range}"
            frames = select_frames_for_time_range(info, clipped_range, limit=frames_per_unit)
            for fr in frames:
                frame_path = Path(str(fr.get("path") or ""))
                if not frame_path.is_file():
                    continue
                content.append({"type": "text", "text": f"【{label}｜画面帧】"})
                content.append(
                    {"type": "image_url", "image_url": {"url": image_to_data_url(frame_path), "detail": "low"}}
                )
            seg = audio_to_mp3_data_url(audio_path, start=start, duration=max(0.1, end - start))
            if seg is not None:
                content.append({"type": "text", "text": f"【{label}｜该时段音频】"})
                content.append({"type": "input_audio", "input_audio": {"data": seg, "format": "mp3"}})
    return content
