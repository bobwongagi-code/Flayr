"""Artifact and frame-selection helpers for Flayr."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


def get_stage_frame_entries(info: dict[str, Any]) -> list[dict[str, Any]]:
    entries = info.get("stage_frames")
    if isinstance(entries, list) and entries:
        return [entry for entry in entries if isinstance(entry, dict)]

    manifest_path = info.get("stage_frame_manifest_path")
    if manifest_path:
        manifest = Path(manifest_path)
        if manifest.exists():
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [entry for entry in data if isinstance(entry, dict)]

    frame_entries = get_frame_entries(info)
    return build_stage_frame_manifest(frame_entries, info.get("duration_seconds"))


def get_frame_entries(info: dict[str, Any]) -> list[dict[str, Any]]:
    entries = info.get("frames")
    if isinstance(entries, list) and entries:
        return [entry for entry in entries if isinstance(entry, dict)]

    manifest_path = info.get("frame_manifest_path")
    if manifest_path:
        manifest = Path(manifest_path)
        if manifest.exists():
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [entry for entry in data if isinstance(entry, dict)]

    frames_dir = info.get("frames_dir")
    directory = Path(frames_dir) if frames_dir else None
    if not directory or not directory.exists():
        return []
    return build_frame_manifest(sorted(directory.glob("frame_*.jpg"), key=numbered_frame_sort_key))


def get_focus_frame_entries(info: dict[str, Any]) -> list[dict[str, Any]]:
    entries = info.get("focus_frames")
    if isinstance(entries, list) and entries:
        return [entry for entry in entries if isinstance(entry, dict)]

    manifest_path = info.get("focus_frame_manifest_path")
    if manifest_path:
        manifest = Path(manifest_path)
        if manifest.exists():
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [entry for entry in data if isinstance(entry, dict)]

    focus_dir = info.get("focus_frames_dir")
    duration = info.get("duration_seconds")
    frames_dir = Path(focus_dir) if focus_dir else None
    if not frames_dir or not frames_dir.exists():
        return []

    entries = []
    for frame in sorted(frames_dir.glob("*.jpg"), key=focus_frame_sort_key):
        label = frame.name.split("_", 1)[0]
        index_text = frame.stem.rsplit("_", 1)[-1]
        try:
            index = max(0, int(index_text) - 1)
        except ValueError:
            index = 0
        start = 0.0
        if label == "cta" and isinstance(duration, (int, float)):
            start = max(0.0, duration - 5.0)
        entries.append(
            {
                "label": label,
                "timestamp_seconds": round(start + index * 0.5, 2),
                "path": str(frame),
                "filename": frame.name,
            }
        )
    return entries


def build_frame_manifest(frames: list[Path]) -> list[dict[str, Any]]:
    manifest = []
    for index, frame in enumerate(frames):
        manifest.append(
            {
                "timestamp_seconds": float(index),
                "path": str(frame),
                "filename": frame.name,
            }
        )
    return manifest


def build_stage_frame_manifest(
    frame_manifest: list[dict[str, Any]],
    duration_seconds: Any,
) -> list[dict[str, Any]]:
    if not frame_manifest:
        return []

    duration = duration_seconds if isinstance(duration_seconds, (int, float)) else max(
        item.get("timestamp_seconds", 0.0) for item in frame_manifest
    )
    ranges = stage_time_ranges(float(duration))
    stage_frames = []
    for stage_name, label, start, end in ranges:
        candidates = [
            item for item in frame_manifest
            if start <= float(item.get("timestamp_seconds", 0.0)) <= end
        ]
        if not candidates:
            target = (start + end) / 2
            candidates = sorted(
                frame_manifest,
                key=lambda item: abs(float(item.get("timestamp_seconds", 0.0)) - target),
            )[:1]
        for item in sample_evenly(candidates, 2):
            stage_frames.append(
                {
                    "stage": stage_name,
                    "label": label,
                    "timestamp_seconds": item.get("timestamp_seconds"),
                    "path": item.get("path"),
                    "filename": item.get("filename"),
                }
            )
    return stage_frames


def stage_time_ranges(duration: float) -> list[tuple[str, str, float, float]]:
    end = max(0.0, duration)
    cta_start = max(0.0, end - 5.0)
    return [
        ("S1 Hook", "Hook", 0.0, min(3.0, end)),
        ("S2 产品引出", "Product intro", min(3.0, end), min(6.0, end)),
        ("S3 使用过程", "Usage", min(6.0, end), min(15.0, end)),
        ("S4 效果呈现", "Result", min(15.0, end), min(23.0, end)),
        ("S5 信任放大", "Trust", min(23.0, end), min(27.0, end)),
        ("S6 CTA", "CTA", cta_start, end),
    ]


def select_frame_for_time_range(info: dict[str, Any], time_range: str) -> dict[str, Any] | None:
    frames = select_frames_for_time_range(info, time_range, limit=1)
    return frames[0] if frames else None


def select_frame_near_timestamp(info: dict[str, Any], timestamp: Any) -> dict[str, Any] | None:
    entries = get_frame_entries(info)
    if not entries:
        entries = get_stage_frame_entries(info) + get_focus_frame_entries(info)
    if not entries:
        return None
    target = parse_timestamp_seconds(timestamp)
    if target is None:
        return None
    return min(entries, key=lambda item: abs(float(item.get("timestamp_seconds", 0.0)) - target))


def select_frames_for_time_range(
    info: dict[str, Any],
    time_range: str,
    limit: int = 3,
) -> list[dict[str, Any]]:
    entries = get_frame_entries(info)
    if not entries:
        entries = get_stage_frame_entries(info) + get_focus_frame_entries(info)
    if not entries:
        return []

    start, end = parse_time_range_seconds(time_range, info.get("duration_seconds"))
    in_range = [
        item for item in entries
        if start <= float(item.get("timestamp_seconds", 0.0)) <= end
    ]
    candidates = in_range
    if not candidates:
        target = (start + end) / 2
        candidates = sorted(
            entries,
            key=lambda item: abs(float(item.get("timestamp_seconds", 0.0)) - target),
        )[:max(1, limit)]
    return sample_evenly(candidates, limit)


def parse_time_range_seconds(text: Any, duration: Any) -> tuple[float, float]:
    duration_value = float(duration) if isinstance(duration, (int, float)) else 0.0
    raw = str(text or "")
    minute_values = [
        float(minutes) * 60 + float(seconds)
        for minutes, seconds in re.findall(r"(\d+):(\d+(?:\.\d+)?)", raw)
    ]
    numbers = minute_values or [float(value) for value in re.findall(r"\d+(?:\.\d+)?", raw)]

    if "最后" in raw or "末尾" in raw or "结尾" in raw or "CTA" in raw.upper():
        if len(numbers) >= 2:
            start = max(0.0, duration_value - max(numbers))
            end = max(0.0, duration_value - min(numbers))
            return normalize_time_range(start, end, duration_value)
        if len(numbers) == 1:
            return normalize_time_range(duration_value - numbers[0], duration_value, duration_value)
        return normalize_time_range(duration_value - 5.0, duration_value, duration_value)

    if len(numbers) >= 2:
        return normalize_time_range(numbers[0], numbers[1], duration_value)
    if len(numbers) == 1:
        return normalize_time_range(numbers[0], numbers[0], duration_value)
    return normalize_time_range(0.0, min(5.0, duration_value), duration_value)


def parse_timestamp_seconds(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value or "").strip()
    match = re.search(r"(\d+):(\d+(?:\.\d+)?)", raw)
    if match:
        return float(match.group(1)) * 60 + float(match.group(2))
    match = re.search(r"\d+(?:\.\d+)?", raw)
    return float(match.group(0)) if match else None


def normalize_time_range(start: float, end: float, duration: float) -> tuple[float, float]:
    if duration > 0:
        start = max(0.0, min(start, duration))
        end = max(0.0, min(end, duration))
    else:
        start = max(0.0, start)
        end = max(0.0, end)
    if end < start:
        start, end = end, start
    if start == end:
        if duration > 0 and start >= duration:
            start = max(0.0, duration - 0.5)
            end = duration
        else:
            end = min(duration, start + 0.5) if duration > 0 else start + 0.5
    return start, end


def sample_evenly(items: list[Any], limit: int) -> list[Any]:
    if limit <= 0 or not items:
        return []
    if len(items) <= limit:
        return items
    if limit == 1:
        return [items[0]]

    step = (len(items) - 1) / (limit - 1)
    indexes = [round(index * step) for index in range(limit)]
    return [items[index] for index in indexes]


def focus_frame_sort_key(path: Path) -> tuple[int, str]:
    if path.name.startswith("hook_"):
        return (0, path.name)
    if path.name.startswith("cta_"):
        return (1, path.name)
    return (2, path.name)


def numbered_frame_sort_key(path: Path) -> tuple[int, str]:
    match = re.search(r"_(\d+)$", path.stem)
    if not match:
        return (sys.maxsize, path.name)
    return (int(match.group(1)), path.name)


def format_seconds(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{value:.1f}s"
    return "未知"
