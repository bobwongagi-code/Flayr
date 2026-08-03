"""Canonical frame selection for multimodal Flayr analysis.

The base extraction remains a deterministic, bounded frame corpus.  This
module turns that corpus plus structural anchors into the manifest consumed by
the LLM and by downstream evidence views.  It deliberately keeps the original
frames and records why each selected frame was retained; selection is a
routing decision, not a destructive cleanup step.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .artifacts import get_focus_frame_entries, get_frame_entries, parse_timestamp_seconds

try:
    from PIL import Image
except ImportError:  # pragma: no cover - optional artifact dependency
    Image = None  # type: ignore[assignment]


GLOBAL_SIGNATURE_SIZE = 16
LOCAL_SIGNATURE_SIZE = 8
LOCAL_GRID_SIZE = 3
GLOBAL_CHANGE_THRESHOLD_PERCENT = 8.0
LOCAL_CHANGE_THRESHOLD_PERCENT = 5.0
ACTION_CHANGE_THRESHOLD_PERCENT = 8.0
MAX_DENSITY_GAP_SECONDS = 2.0


def build_analysis_frame_manifest(info: dict[str, Any]) -> dict[str, Any]:
    """Return the canonical selected-frame manifest and audit decisions.

    Candidate sources are the existing base frames and the existing focused
    hook/CTA frames.  Scene boundaries and subtitle boundaries are promoted to
    anchors by choosing the nearest available candidate.  Consecutive frames
    are compared through global, local-grid, and edge/motion-like signatures;
    any one of those signals can preserve a frame.
    """
    base_frames = sorted(get_frame_entries(info), key=_entry_sort_key)
    focus_frames = sorted(get_focus_frame_entries(info), key=_entry_sort_key)
    if not base_frames and not focus_frames:
        return {"frames": [], "decisions": [], "anchor_count": 0, "strategy_version": "v2"}

    duration = parse_timestamp_seconds(info.get("duration_seconds"))
    anchors = _load_anchor_times(info, duration)
    anchor_reasons: dict[str, set[str]] = {}
    all_candidates = [*base_frames, *focus_frames]
    for timestamp, reason in anchors:
        nearest = _nearest_candidate(all_candidates, timestamp)
        if nearest is None:
            continue
        path = str(nearest.get("path") or "")
        if path:
            anchor_reasons.setdefault(path, set()).add(reason)

    selected_by_path: dict[str, dict[str, Any]] = {}
    decisions: list[dict[str, Any]] = []
    previous_features: dict[str, Any] | None = None
    last_selected_timestamp: float | None = None

    for index, entry in enumerate(base_frames):
        path = str(entry.get("path") or "")
        timestamp = parse_timestamp_seconds(entry.get("timestamp_seconds"))
        features = _image_features(Path(path)) if path else None
        global_diff = _diff_percent(features["global"], previous_features["global"]) if features and previous_features else None
        local_diff = _max_local_diff(features, previous_features) if features and previous_features else None
        action_diff = _diff_percent(features["action"], previous_features["action"]) if features and previous_features else None

        reasons = set(anchor_reasons.get(path, set()))
        if str(entry.get("source") or "") == "structural_anchor":
            reasons.add("structural_anchor")
            reasons.update(
                token
                for token in str(entry.get("anchor_type") or "").split("+")
                if token in {"scene_boundary", "subtitle_boundary", "stage_boundary"}
            )
        if index == 0:
            reasons.add("first_frame")
        if index == len(base_frames) - 1:
            reasons.add("last_frame")
        if global_diff is not None and global_diff >= GLOBAL_CHANGE_THRESHOLD_PERCENT:
            reasons.add("global_change")
        if local_diff is not None and local_diff >= LOCAL_CHANGE_THRESHOLD_PERCENT:
            reasons.add("local_change")
        if action_diff is not None and action_diff >= ACTION_CHANGE_THRESHOLD_PERCENT:
            reasons.add("action_change")
        if timestamp is not None and (
            last_selected_timestamp is None or timestamp - last_selected_timestamp >= MAX_DENSITY_GAP_SECONDS
        ):
            reasons.add("density_floor")

        keep = bool(reasons)
        if features is None and not reasons:
            # Keep an unreadable candidate out of the model payload; its
            # failure remains visible in the audit decision below.
            keep = False
        if keep and path:
            selected_by_path[path] = _selected_entry(
                entry,
                reasons,
                global_diff,
                local_diff,
                action_diff,
                str(entry.get("source") or "base"),
            )
            if timestamp is not None:
                last_selected_timestamp = timestamp

        decisions.append(
            {
                **entry,
                "kept": keep,
                "selection_reasons": sorted(reasons),
                "reason": _primary_reason(reasons, "unreadable" if features is None else "near_duplicate"),
                "global_diff_percent": _round_metric(global_diff),
                "local_diff_percent": _round_metric(local_diff),
                "action_diff_percent": _round_metric(action_diff),
            }
        )
        if features is not None:
            previous_features = features

    for entry in focus_frames:
        path = str(entry.get("path") or "")
        if not path:
            continue
        reasons = set(anchor_reasons.get(path, set()))
        label = str(entry.get("label") or "").strip().lower()
        if label == "hook":
            reasons.add("focus_hook")
        elif label == "cta":
            reasons.add("focus_cta")
        else:
            reasons.add("focus_anchor")
        selected_by_path[path] = _selected_entry(entry, reasons, None, None, None, "focus")
        decisions.append(
            {
                **entry,
                "kept": True,
                "selection_reasons": sorted(reasons),
                "reason": _primary_reason(reasons, "focus_anchor"),
                "global_diff_percent": None,
                "local_diff_percent": None,
                "action_diff_percent": None,
            }
        )

    # A malformed or very sparse frame manifest must not silently produce no
    # visual input.  Keep the first available readable base frame as a final
    # deterministic fallback.
    if not selected_by_path:
        for entry in all_candidates:
            path = str(entry.get("path") or "")
            if path and Path(path).is_file():
                selected_by_path[path] = _selected_entry(entry, {"fallback"}, None, None, None, "fallback")
                break

    selected = sorted(selected_by_path.values(), key=_entry_sort_key)
    return {
        "strategy_version": "v3",
        "frames": selected,
        "decisions": decisions,
        "anchor_count": sum(1 for item in selected if _anchor_reasons(item)),
        "base_frame_count": len(base_frames),
        "focus_frame_count": len(focus_frames),
    }


def _selected_entry(
    entry: dict[str, Any],
    reasons: set[str],
    global_diff: float | None,
    local_diff: float | None,
    action_diff: float | None,
    source: str,
) -> dict[str, Any]:
    return {
        **entry,
        "source": source,
        "selection_reasons": sorted(reasons),
        "selection_reason": _primary_reason(reasons, "selected"),
        "global_diff_percent": _round_metric(global_diff),
        "local_diff_percent": _round_metric(local_diff),
        "action_diff_percent": _round_metric(action_diff),
    }


def _primary_reason(reasons: set[str], fallback: str) -> str:
    priority = (
        "scene_boundary",
        "subtitle_boundary",
        "structural_anchor",
        "stage_boundary",
        "focus_hook",
        "focus_cta",
        "last_frame",
        "first_frame",
        "local_change",
        "action_change",
        "global_change",
        "density_floor",
        "fallback",
    )
    for reason in priority:
        if reason in reasons:
            return reason
    return fallback


def _anchor_reasons(entry: dict[str, Any]) -> set[str]:
    return {
        str(reason)
        for reason in entry.get("selection_reasons", [])
        if str(reason) in {
            "scene_boundary",
            "subtitle_boundary",
            "structural_anchor",
            "stage_boundary",
            "focus_hook",
            "focus_cta",
            "first_frame",
            "last_frame",
        }
    }


def _entry_sort_key(entry: dict[str, Any]) -> tuple[float, str]:
    timestamp = parse_timestamp_seconds(entry.get("timestamp_seconds"))
    return (timestamp if timestamp is not None else float("inf"), str(entry.get("path") or ""))


def _nearest_candidate(candidates: list[dict[str, Any]], target: float) -> dict[str, Any] | None:
    timed = [
        item
        for item in candidates
        if parse_timestamp_seconds(item.get("timestamp_seconds")) is not None and str(item.get("path") or "")
    ]
    if not timed:
        return None
    return min(timed, key=lambda item: abs(float(parse_timestamp_seconds(item["timestamp_seconds"]) or 0.0) - target))


def _load_anchor_times(info: dict[str, Any], duration: float | None) -> list[tuple[float, str]]:
    anchors: list[tuple[float, str]] = []
    shot_track = _load_json_path(info, "shot_track_path")
    if isinstance(shot_track, dict):
        for shot in shot_track.get("shots", []):
            if not isinstance(shot, dict):
                continue
            start = _bounded_time(shot.get("start_sec"), duration)
            if start is not None:
                anchors.append((start, "scene_boundary"))

    subtitle_track = _load_json_path(info, "subtitle_track_path")
    if isinstance(subtitle_track, dict):
        for segment in subtitle_track.get("segments", []):
            if not isinstance(segment, dict):
                continue
            for key in ("start_sec", "end_sec"):
                timestamp = _bounded_time(segment.get(key), duration)
                if timestamp is not None:
                    anchors.append((timestamp, "subtitle_boundary"))
    return anchors


def _bounded_time(value: Any, duration: float | None) -> float | None:
    timestamp = parse_timestamp_seconds(value)
    if timestamp is None or not math.isfinite(timestamp) or timestamp < 0:
        return None
    if duration is not None and timestamp > duration:
        return None
    return timestamp


def _load_json_path(info: dict[str, Any], key: str) -> Any:
    raw = str(info.get(key) or "").strip()
    if not raw and isinstance(info.get("video_evidence"), dict):
        raw = str(info["video_evidence"].get(key) or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _image_features(path: Path) -> dict[str, Any] | None:
    if Image is None or not path.is_file():
        return None
    try:
        with Image.open(path) as source:
            image = source.convert("RGB")
            global_signature = list(image.resize((GLOBAL_SIGNATURE_SIZE, GLOBAL_SIGNATURE_SIZE)).getdata())
            gray = image.convert("L").resize((GLOBAL_SIGNATURE_SIZE, GLOBAL_SIGNATURE_SIZE))
            action_signature = _edge_signature(gray)
            local_signatures: list[list[tuple[int, int, int]]] = []
            width, height = image.size
            for row in range(LOCAL_GRID_SIZE):
                for column in range(LOCAL_GRID_SIZE):
                    left = int(width * column / LOCAL_GRID_SIZE)
                    upper = int(height * row / LOCAL_GRID_SIZE)
                    right = max(left + 1, int(width * (column + 1) / LOCAL_GRID_SIZE))
                    lower = max(upper + 1, int(height * (row + 1) / LOCAL_GRID_SIZE))
                    local = image.crop((left, upper, right, lower)).resize((LOCAL_SIGNATURE_SIZE, LOCAL_SIGNATURE_SIZE))
                    local_signatures.append(list(local.getdata()))
            return {"global": global_signature, "local": local_signatures, "action": action_signature}
    except (OSError, ValueError):
        return None


def _edge_signature(gray: Any) -> list[int]:
    width, height = gray.size
    pixels = gray.load()
    result: list[int] = []
    for y in range(height):
        for x in range(width):
            right = pixels[min(width - 1, x + 1), y]
            down = pixels[x, min(height - 1, y + 1)]
            result.append(min(255, abs(int(pixels[x, y]) - int(right)) + abs(int(pixels[x, y]) - int(down))))
    return result


def _max_local_diff(current: dict[str, Any], previous: dict[str, Any]) -> float:
    current_regions = current.get("local", [])
    previous_regions = previous.get("local", [])
    return max(
        (_diff_percent(left, right) for left, right in zip(current_regions, previous_regions)),
        default=0.0,
    )


def _diff_percent(current: list[Any] | None, previous: list[Any] | None) -> float | None:
    if current is None or previous is None:
        return None
    total = min(len(current), len(previous))
    if total <= 0:
        return 100.0
    changed = 0
    for left, right in zip(current[:total], previous[:total]):
        if isinstance(left, tuple) and isinstance(right, tuple):
            different = any(abs(int(left[index]) - int(right[index])) > 25 for index in range(min(len(left), len(right))))
        else:
            different = abs(int(left) - int(right)) > 18
        if different:
            changed += 1
    return changed / total * 100.0


def _round_metric(value: float | None) -> float | None:
    return round(value, 2) if value is not None and math.isfinite(value) else None
