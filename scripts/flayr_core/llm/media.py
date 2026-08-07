"""flayr_core.llm.media：多模态 LLM 输入素材选择。

只负责把本地帧、timeline view 和 evidence 感官切片转成 chat payload 可用的
image_url / input_audio / video_url 块；不写 prompt，不碰业务判断规则。
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from ..artifacts import (
    format_seconds,
    get_analysis_frame_entries,
    get_focus_frame_entries,
    get_stage_frame_entries,
    parse_time_range_seconds,
    parse_timestamp_seconds,
    resolve_artifact_path,
    sample_evenly,
    select_frames_for_time_range,
)
from .api import (
    audio_to_mp3_data_url,
    can_send_standalone_audio,
    image_to_data_url,
)
from ..resources import ResourceBudget
from ..stage_evidence_contracts import stage_analysis_evidence_view


def select_role_visual_inputs(info: dict[str, Any], role: str, image_limit: int) -> list[dict[str, Any]]:
    """为单视频事实抽取选关键帧，最多 image_limit 张。"""
    selected: list[dict[str, Any]] = []
    for entry in get_llm_visual_candidates(info, image_limit):
        frame = resolve_artifact_path(info, entry.get("path"), require_file=True, require_root=True)
        if frame is None:
            continue
        timestamp = format_seconds(entry.get("timestamp_seconds")) if entry.get("timestamp_seconds") is not None else ""
        marker = f" @ {timestamp}" if timestamp else ""
        selected.append(
            {
                "role": role,
                "path": str(frame),
                "label": f"{role} {entry.get('stage') or entry.get('label', 'frame')}{marker} {frame.name}",
                "data_url": image_to_data_url(frame),
                "timestamp_seconds": entry.get("timestamp_seconds"),
                "source_frame_timestamps": list(entry.get("source_frame_timestamps") or []),
            }
        )
    return selected[:image_limit]


def select_stage_recovery_visual_inputs(
    info: dict[str, Any],
    role: str,
    target_stages: list[str],
    image_limit: int,
) -> list[dict[str, Any]]:
    """Select a bounded, stage-focused view for the one Stage1-C pass.

    The initial extractor gets the canonical whole-video selection. Recovery
    must use the stage-frame manifest instead of silently sending that same
    selection and hoping a different instruction repairs the blind spot.
    """
    if image_limit <= 0:
        return []
    targets = {
        stage
        for value in target_stages
        if (stage := _stage_token(value)) is not None
    }
    entries = [
        entry
        for entry in get_stage_frame_entries(info)
        if (stage := _stage_token(entry.get("stage"))) in targets
    ]
    if not entries:
        return select_role_visual_inputs(info, role, image_limit)

    # Preserve temporal coverage across requested stages, with at most one
    # extra frame for a remainder. Dedupe by path because stage boundaries can
    # intentionally share a frame.
    selected_entries: list[dict[str, Any]] = []
    per_stage = max(1, image_limit // max(1, len(targets)))
    for stage in sorted(targets):
        stage_entries = [
            entry
            for entry in entries
            if _stage_token(entry.get("stage")) == stage
        ]
        selected_entries.extend(sample_evenly(stage_entries, per_stage))
    if len(selected_entries) < image_limit:
        chosen = {str(entry.get("path") or "") for entry in selected_entries}
        selected_entries.extend(
            entry for entry in entries if str(entry.get("path") or "") not in chosen
        )
    selected_entries = selected_entries[:image_limit]
    selected: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for entry in selected_entries:
        frame = resolve_artifact_path(info, entry.get("path"), require_file=True, require_root=True)
        if frame is None or str(frame) in seen_paths:
            continue
        seen_paths.add(str(frame))
        timestamp = format_seconds(entry.get("timestamp_seconds")) if entry.get("timestamp_seconds") is not None else ""
        marker = f" @ {timestamp}" if timestamp else ""
        selected.append(
            {
                "role": role,
                "path": str(frame),
                "label": f"{role} Stage1-C {entry.get('stage') or 'stage'}{marker} {frame.name}",
                "data_url": image_to_data_url(frame),
                "timestamp_seconds": entry.get("timestamp_seconds"),
                "source_frame_timestamps": list(entry.get("source_frame_timestamps") or []),
            }
        )
    return selected[:image_limit]


def _stage_token(value: Any) -> str | None:
    """Extract one canonical stage token without trusting arbitrary labels."""
    match = re.search(r"\bS([1-6])\b", str(value or "").upper())
    return f"S{match.group(1)}" if match else None


def get_llm_visual_candidates(info: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """候选视觉输入：先给 Hook/CTA timeline view，再补原始帧。"""
    if limit <= 0:
        return []
    timeline_limit = min(2, max(0, limit // 3))
    timeline_entries = get_timeline_view_entries(info)[:timeline_limit]
    remaining = max(0, limit - len(timeline_entries))
    frame_entries = _uncovered_frame_candidates(info, remaining, timeline_entries)
    return timeline_entries + frame_entries


def _uncovered_frame_candidates(
    info: dict[str, Any],
    limit: int,
    timeline_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Use the raw-frame budget for timeline regions not already visible.

    Hook/CTA filmstrips already expose several timestamped frames. Selecting
    first/last/focus anchors again would spend the small request budget on the
    same endpoints and leave S2-S5 unseen. This selector keeps the overview
    images and distributes the remaining raw frames across uncovered time.
    """
    if limit <= 0:
        return []
    covered: list[tuple[float, float]] = []
    for item in timeline_entries:
        start = parse_timestamp_seconds(item.get("start_seconds"))
        end = parse_timestamp_seconds(item.get("end_seconds"))
        if start is not None and end is not None and end >= start:
            covered.append((start, end))
    if not covered:
        return get_llm_frame_candidates(info, limit)

    unique: list[dict[str, Any]] = []
    seen_timestamps: set[float] = set()
    for entry in get_analysis_frame_entries(info):
        timestamp = parse_timestamp_seconds(entry.get("timestamp_seconds"))
        if timestamp is None or any(start <= timestamp <= end for start, end in covered):
            continue
        key = round(timestamp, 3)
        if key in seen_timestamps:
            continue
        seen_timestamps.add(key)
        unique.append(entry)
    selected = sample_evenly(unique, limit)
    if len(selected) >= limit:
        return selected[:limit]

    used = {str(entry.get("path") or "") for entry in selected}
    for entry in get_llm_frame_candidates(info, limit):
        path = str(entry.get("path") or "")
        if path and path not in used:
            selected.append(entry)
            used.add(path)
        if len(selected) >= limit:
            break
    return selected[:limit]


def get_timeline_view_entries(info: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = info.get("video_evidence")
    views = evidence.get("timeline_views") if isinstance(evidence, dict) else None
    entries: list[dict[str, Any]] = []
    canonical_timestamps: dict[str, float] = {}
    for frame in get_analysis_frame_entries(info):
        frame_path = resolve_artifact_path(info, frame.get("path"), require_file=True, require_root=True)
        timestamp = parse_timestamp_seconds(frame.get("timestamp_seconds"))
        if frame_path is not None and timestamp is not None:
            canonical_timestamps[str(frame_path)] = timestamp
    if isinstance(views, list):
        for item in views:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "")
            safe_path = resolve_artifact_path(info, path, require_file=True, require_root=True)
            if safe_path is not None:
                start = parse_timestamp_seconds(item.get("start_seconds"))
                end = parse_timestamp_seconds(item.get("end_seconds"))
                source_timestamps: list[float] = []
                frame_paths = item.get("frame_paths") if isinstance(item.get("frame_paths"), list) else []
                for raw_frame_path in frame_paths:
                    frame_path = resolve_artifact_path(
                        info,
                        raw_frame_path,
                        require_file=True,
                        require_root=True,
                    )
                    timestamp = canonical_timestamps.get(str(frame_path)) if frame_path is not None else None
                    if timestamp is None:
                        continue
                    if start is not None and end is not None and not (start <= timestamp <= end):
                        continue
                    if timestamp not in source_timestamps:
                        source_timestamps.append(timestamp)
                entries.append(
                    {
                        "label": f"{item.get('label') or 'timeline'} timeline",
                        "path": str(safe_path),
                        "timestamp_seconds": None,
                        "start_seconds": start,
                        "end_seconds": end,
                        "source_frame_timestamps": sorted(source_timestamps),
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
        safe_path = resolve_artifact_path(info, path, require_file=True, require_root=True)
        if safe_path is not None:
            entries.append({"label": f"{label} timeline", "path": str(safe_path), "timestamp_seconds": None})
    return entries


def get_llm_frame_candidates(info: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """从 canonical analysis manifest 中选候选帧。

    Anchor frames get first refusal so a small image budget cannot silently
    replace scene/subtitle/CTA evidence with only evenly spaced frames.
    """
    if limit <= 0:
        return []
    entries = get_analysis_frame_entries(info)
    if not entries:
        return []
    has_canonical_metadata = any(isinstance(entry.get("selection_reasons"), list) for entry in entries)
    if not has_canonical_metadata:
        # Compatibility for pre-v2 cached manifests.  New preprocessing writes
        # selection provenance and therefore always follows the canonical path.
        focus_limit = 2 if limit >= 6 else 0
        timeline_entries = sample_evenly(entries, max(1, limit - focus_limit))
        focus_entries = sample_evenly(get_focus_frame_entries(info), focus_limit)
        return sample_evenly([*timeline_entries, *focus_entries], limit)

    def reasons(entry: dict[str, Any]) -> list[str]:
        value = entry.get("selection_reasons")
        return [str(item) for item in value] if isinstance(value, list) else []

    selected: list[dict[str, Any]] = []
    used_paths: set[str] = set()

    def add_group(group: list[dict[str, Any]], count: int) -> None:
        if count <= 0:
            return
        available = [item for item in group if str(item.get("path") or "") not in used_paths]
        for entry in sample_evenly(available, count):
            path = str(entry.get("path") or "")
            if path and path not in used_paths and len(selected) < limit:
                selected.append(entry)
                used_paths.add(path)

    # Keep one explicit boundary/attention anchor from each high-value group,
    # then use temporal coverage to fill the remaining budget.  Pure priority
    # sorting can otherwise spend the entire image budget on early subtitles or
    # several adjacent scene cuts and never show the middle/end of the video.
    add_group([entry for entry in entries if "first_frame" in reasons(entry)], 1)
    add_group([entry for entry in entries if "last_frame" in reasons(entry)], 1)
    add_group([entry for entry in entries if "focus_hook" in reasons(entry)], 1)
    add_group([entry for entry in entries if "focus_cta" in reasons(entry)], 1)
    boundary_entries = [
        entry
        for entry in entries
        if {"scene_boundary", "subtitle_boundary", "speech_boundary"}.intersection(reasons(entry))
    ]
    add_group(boundary_entries, min(2, max(0, limit - len(selected))))
    change_entries = [
        entry
        for entry in entries
        if {"local_change", "action_change", "global_change"}.intersection(reasons(entry))
    ]
    add_group(change_entries, min(2, max(0, limit - len(selected))))

    if len(selected) < limit:
        remaining = [entry for entry in entries if str(entry.get("path") or "") not in used_paths]
        for entry in sample_evenly(remaining, limit - len(selected)):
            path = str(entry.get("path") or "")
            if path and path not in used_paths:
                selected.append(entry)
                used_paths.add(path)
    return sorted(
        selected[:limit],
        key=lambda item: parse_timestamp_seconds(item.get("timestamp_seconds"))
        if parse_timestamp_seconds(item.get("timestamp_seconds")) is not None
        else float("inf"),
    )


def build_evidence_sensory_inputs(
    analysis: dict[str, Any],
    facts: dict[str, Any],
    frames_per_unit: int = 1,
    window_end_seconds: float | None = None,
    api_url: str = "",
    model: str = "",
    budget: ResourceBudget | None = None,
) -> list[dict[str, Any]]:
    """为阶段二对比判断准备每条 evidence_unit 的感官证据。"""
    content: list[dict[str, Any]] = []
    standalone_audio = can_send_standalone_audio(api_url, model)
    videos = analysis.get("videos", {})
    facts = stage_analysis_evidence_view(facts)
    for role in ("benchmark", "creator"):
        role_facts = facts.get(role) or {}
        stage_units = role_facts.get("stage_evidence_units")
        if isinstance(stage_units, dict):
            units_by_id: dict[str, dict[str, Any]] = {}
            qualified_stages: dict[str, set[str]] = {}
            for stage, stage_items in stage_units.items():
                for unit in stage_items or []:
                    if not isinstance(unit, dict):
                        continue
                    unit_id = str(unit.get("id") or "").strip()
                    if not unit_id:
                        continue
                    units_by_id.setdefault(unit_id, unit)
                    qualified_stages.setdefault(unit_id, set()).add(str(stage))
            units = []
            for unit_id, unit in units_by_id.items():
                prepared = dict(unit)
                prepared["qualified_stages"] = sorted(qualified_stages.get(unit_id, set()))
                units.append(prepared)
        else:
            units = role_facts.get("evidence_units") or []
        info = videos.get(role) or {}
        audio_path = Path(str(info.get("work_dir") or "")) / "audio.wav"
        duration = info.get("duration_seconds")
        prepared_units = _prepare_evidence_windows(units, duration, window_end_seconds)
        for unit in prepared_units:
            uid = str(unit["label"])
            start = float(unit["start"])
            end = float(unit["end"])
            clipped_range = f"{start:.2f}s - {end:.2f}s"
            stage_label = ",".join(unit.get("qualified_stages") or [])
            label = f"{role} {uid} [{stage_label or 'legacy'}] @ {clipped_range}"
            frames = select_frames_for_time_range(info, clipped_range, limit=frames_per_unit)
            for fr in frames:
                frame_path = resolve_artifact_path(info, fr.get("path"), require_file=True, require_root=True)
                if frame_path is None:
                    continue
                content.append({"type": "text", "text": f"【{label}｜画面帧】"})
                content.append(
                    {"type": "image_url", "image_url": {"url": image_to_data_url(frame_path), "detail": "low"}}
                )
            seg = (
                audio_to_mp3_data_url(
                    audio_path,
                    start=start,
                    duration=max(0.1, end - start),
                    max_duration_seconds=600.0,
                    max_data_bytes=8 * 1024 * 1024,
                    budget=budget,
                )
                if standalone_audio
                else None
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
        parsed = parse_time_range_seconds(time_range, duration)
        if parsed is None:
            continue
        start, end = parsed
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
