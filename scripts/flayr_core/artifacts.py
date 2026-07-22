"""Artifact and frame-selection helpers for Flayr."""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path
from typing import Any

from .stage_catalog import fallback_artifact_ranges

MAX_TIME_SECONDS = 24 * 60 * 60.0


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
        entries.append(
            {
                "label": label,
                # Without the extraction manifest there is no trustworthy source PTS.
                "timestamp_seconds": None,
                "path": str(frame),
                "filename": frame.name,
            }
        )
    return entries


def build_frame_manifest(frames: list[Path], timestamps: list[Any] | None = None) -> list[dict[str, Any]]:
    manifest = []
    for index, frame in enumerate(frames):
        timestamp = (
            parse_timestamp_seconds(timestamps[index])
            if timestamps is not None and index < len(timestamps)
            else None
        )
        manifest.append(
            {
                "timestamp_seconds": timestamp,
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

    raw_duration_present = duration_seconds is not None and str(duration_seconds).strip() != ""
    duration = parse_timestamp_seconds(duration_seconds)
    if raw_duration_present and duration is None:
        return []
    if duration is None:
        valid_timestamps = [
            timestamp
            for item in frame_manifest
            if (timestamp := parse_timestamp_seconds(item.get("timestamp_seconds"))) is not None
        ]
        duration = max(valid_timestamps, default=None)
    if duration is None:
        return []
    timed_manifest = [
        item
        for item in frame_manifest
        if parse_timestamp_seconds(item.get("timestamp_seconds")) is not None
    ]
    if not timed_manifest:
        return []
    ranges = stage_time_ranges(duration)
    stage_frames = []
    for stage_name, label, start, end in ranges:
        candidates = [
            item for item in timed_manifest
            if start <= parse_timestamp_seconds(item.get("timestamp_seconds")) <= end
        ]
        if not candidates:
            target = (start + end) / 2
            candidates = sorted(
                timed_manifest,
                key=lambda item: abs(parse_timestamp_seconds(item.get("timestamp_seconds")) - target),
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
    return fallback_artifact_ranges(duration)


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
    timed_entries = []
    for item in entries:
        item_timestamp = parse_timestamp_seconds(item.get("timestamp_seconds"))
        if item_timestamp is not None:
            timed_entries.append((item, item_timestamp))
    if not timed_entries:
        return None
    return min(timed_entries, key=lambda pair: abs(pair[1] - target))[0]


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

    parsed = parse_time_range_seconds(time_range, info.get("duration_seconds"))
    if parsed is None:
        return []
    start, end = parsed
    timed_entries = [
        (item, timestamp)
        for item in entries
        if (timestamp := parse_timestamp_seconds(item.get("timestamp_seconds"))) is not None
    ]
    in_range = [item for item, timestamp in timed_entries if start <= timestamp <= end]
    candidates = in_range
    if not candidates:
        target = (start + end) / 2
        candidates = [
            item
            for item, _timestamp in sorted(
                timed_entries,
                key=lambda pair: abs(pair[1] - target),
            )[:max(1, limit)]
        ]
    return sample_evenly(candidates, limit)


_TIME_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_.])(-?(?:(?:\d+(?::\d+){1,2})(?:\.\d+)?|\d+(?:\.\d+)?))(?![0-9.])"
)
_NONFINITE_TEXT_RE = re.compile(r"(?:^|[^A-Za-z])(nan|inf(?:inity)?)(?:$|[^A-Za-z])", re.IGNORECASE)
_TIME_VALUE_PATTERN = r"(?:\d+(?::\d{1,2}){1,2}(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?:s|sec(?:onds?)?|秒)?"
_TIME_RANGE_RE = re.compile(
    rf"^\s*{_TIME_VALUE_PATTERN}\s*(?:[-~～至到]\s*{_TIME_VALUE_PATTERN})?\s*$",
    re.IGNORECASE,
)
_RELATIVE_TIME_RANGE_RE = re.compile(
    rf"^\s*(?:(?:最后|末尾|结尾)\s*|CTA\s*){_TIME_VALUE_PATTERN}"
    rf"(?:\s*(?:[-~～至到])\s*{_TIME_VALUE_PATTERN})?\s*$",
    re.IGNORECASE,
)
_TIMESTAMP_RE = re.compile(rf"^\s*{_TIME_VALUE_PATTERN}\s*$", re.IGNORECASE)


def _finite_nonnegative_time(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0 or number > MAX_TIME_SECONDS:
        return None
    return number


def _parse_time_tokens(raw: str) -> list[float] | None:
    """Return parsed tokens, or None when an explicit malformed token exists."""
    if _NONFINITE_TEXT_RE.search(raw):
        return None
    values: list[float] = []
    for token in _TIME_TOKEN_RE.findall(raw):
        if token.startswith("-"):
            return None
        parts = token.split(":")
        try:
            if len(parts) == 1:
                value = float(parts[0])
            elif len(parts) == 2:
                minutes = int(parts[0])
                seconds = float(parts[1])
                if seconds >= 60:
                    return None
                value = minutes * 60.0 + seconds
            elif len(parts) == 3:
                hours = int(parts[0])
                minutes = int(parts[1])
                seconds = float(parts[2])
                if minutes >= 60 or seconds >= 60:
                    return None
                value = hours * 3600.0 + minutes * 60.0 + seconds
            else:
                return None
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value) or value < 0:
            return None
        values.append(value)
    return values


def parse_time_range_seconds(text: Any, duration: Any) -> tuple[float, float] | None:
    """Parse a time range without changing invalid evidence semantics.

    Missing, reversed, out-of-bounds, negative, and non-finite values return
    ``None``. No default window, swapping, clamping, or zero-width expansion is
    performed here.
    """
    duration_value = None if duration is None or str(duration).strip() == "" else _finite_nonnegative_time(duration)
    if duration is not None and str(duration).strip() != "" and duration_value is None:
        return None

    if isinstance(text, (list, tuple)):
        if len(text) != 2:
            return None
        start = _finite_nonnegative_time(text[0])
        end = _finite_nonnegative_time(text[1])
        if start is None or end is None:
            return None
        return normalize_time_range(start, end, duration_value)

    if isinstance(text, dict):
        start = _finite_nonnegative_time(text.get("start", text.get("start_time")))
        end = _finite_nonnegative_time(text.get("end", text.get("end_time")))
        if start is None or end is None:
            return None
        return normalize_time_range(start, end, duration_value)

    if isinstance(text, bool):
        return None
    raw = str(text or "").strip()
    if not raw:
        return None
    numbers = _parse_time_tokens(raw)
    if numbers is None or not numbers:
        return None

    is_relative = "最后" in raw or "末尾" in raw or "结尾" in raw or "CTA" in raw.upper()
    if is_relative:
        if not _RELATIVE_TIME_RANGE_RE.fullmatch(raw):
            return None
        if duration_value is None:
            return None
        if len(numbers) == 2:
            start = duration_value - max(numbers[:2])
            end = duration_value - min(numbers[:2])
        elif len(numbers) == 1:
            start, end = duration_value - numbers[0], duration_value
        else:
            return None
    elif not _TIME_RANGE_RE.fullmatch(raw):
        return None
    elif len(numbers) >= 2:
        if len(numbers) != 2:
            return None
        start, end = numbers[0], numbers[1]
    else:
        start = end = numbers[0]
    return normalize_time_range(start, end, duration_value)


def parse_timestamp_seconds(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return _finite_nonnegative_time(value)
    if isinstance(value, bool):
        return None
    if isinstance(value, (list, tuple, dict)):
        return None
    raw = str(value or "").strip()
    if not raw:
        return None
    if not _TIMESTAMP_RE.fullmatch(raw):
        return None
    numbers = _parse_time_tokens(raw)
    return numbers[0] if numbers is not None and len(numbers) == 1 else None


def normalize_time_range(start: Any, end: Any, duration: float | None) -> tuple[float, float] | None:
    """Validate a range; retained as a compatibility name for callers."""
    start_value = _finite_nonnegative_time(start)
    end_value = _finite_nonnegative_time(end)
    if start_value is None or end_value is None or end_value < start_value:
        return None
    if duration is not None and end_value > duration:
        return None
    return start_value, end_value


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
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and value >= 0:
        return f"{float(value):.1f}s"
    return "未知"
