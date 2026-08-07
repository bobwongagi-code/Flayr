"""Explicit current-generation transcript artifact resolution and timing views."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .artifacts import parse_timestamp_seconds, resolve_artifact_path


def parse_srt_segments(path: Path) -> list[dict[str, Any]]:
    """Read raw SRT segments without pretending they are window-safe units."""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="ignore")
    blocks = re.split(r"\n\s*\n", text.strip())
    segments: list[dict[str, Any]] = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        time_line_index = next((idx for idx, line in enumerate(lines) if "-->" in line), None)
        if time_line_index is None:
            continue
        start, end = parse_srt_time_range(lines[time_line_index])
        if start is None or end is None:
            continue
        spoken = " ".join(lines[time_line_index + 1 :]).strip()
        if spoken:
            segments.append(
                {
                    "start_seconds": round(start, 2),
                    "end_seconds": round(end, 2),
                    "text": spoken,
                }
            )
    return segments


def parse_srt_time_range(line: str) -> tuple[float | None, float | None]:
    parts = line.split("-->")
    if len(parts) != 2:
        return None, None
    start = parse_srt_timestamp(parts[0])
    end = parse_srt_timestamp(parts[1])
    if start is None or end is None or end < start:
        return None, None
    return start, end


def parse_srt_timestamp(value: str) -> float | None:
    normalized = str(value or "").strip()
    if not re.fullmatch(r"\d+:\d{2}:\d{2}(?:[.,]\d+)?", normalized):
        return None
    return parse_timestamp_seconds(normalized.replace(",", "."))


def current_transcript_segments_path(info: dict[str, Any]) -> Path | None:
    """Return only the transcript segment artifact explicitly recorded for this video."""
    raw = str(info.get("transcript_segments_path") or "").strip()
    if not raw:
        return None
    work_dir = str(info.get("work_dir") or "").strip()
    return resolve_artifact_path(
        info,
        raw,
        require_file=True,
        require_root=bool(work_dir),
    )


def current_transcript_words_path(info: dict[str, Any]) -> Path | None:
    """Return the current-generation word-timestamp artifact, if available."""
    raw = str(info.get("transcript_words_path") or "").strip()
    if not raw and isinstance(info.get("video_evidence"), dict):
        raw = str(info["video_evidence"].get("transcript_words_path") or "").strip()
    if not raw:
        return None
    work_dir = str(info.get("work_dir") or "").strip()
    return resolve_artifact_path(
        info,
        raw,
        require_file=True,
        require_root=bool(work_dir),
    )


def parse_transcript_words(path: Path) -> list[dict[str, Any]]:
    """Read normalized word timestamps produced by the current ASR backend."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict) or not isinstance(payload.get("words"), list):
        return []

    words: list[dict[str, Any]] = []
    for item in payload["words"]:
        if not isinstance(item, dict):
            continue
        start = parse_timestamp_seconds(item.get("start_seconds"))
        end = parse_timestamp_seconds(item.get("end_seconds"))
        text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
        if start is None or end is None or end < start or not text:
            continue
        words.append(
            {
                "start_seconds": round(start, 3),
                "end_seconds": round(end, 3),
                "text": text,
            }
        )
    return sorted(words, key=lambda item: (item["start_seconds"], item["end_seconds"]))


def load_transcript_words(info: dict[str, Any]) -> list[dict[str, Any]]:
    path = current_transcript_words_path(info)
    return parse_transcript_words(path) if path is not None else []


def transcript_words_for_range(
    words: list[dict[str, Any]],
    start_seconds: float,
    end_seconds: float,
) -> list[dict[str, Any]]:
    """Return only word-timed speech that intersects one evidence window."""
    if end_seconds <= start_seconds:
        return []
    return [
        word
        for word in words
        if float(word.get("end_seconds", 0.0)) > start_seconds
        and float(word.get("start_seconds", 0.0)) < end_seconds
    ]


def transcript_text_for_range(
    words: list[dict[str, Any]],
    start_seconds: float,
    end_seconds: float,
) -> str:
    """Build the authoritative ASR text for one evidence time window."""
    return " ".join(
        str(word.get("text") or "").strip()
        for word in transcript_words_for_range(words, start_seconds, end_seconds)
        if str(word.get("text") or "").strip()
    ).strip()


def group_transcript_words(
    words: list[dict[str, Any]],
    *,
    max_gap_seconds: float = 0.55,
    max_window_seconds: float = 4.0,
    max_characters: int = 120,
) -> list[dict[str, Any]]:
    """Create readable, time-safe transcript windows from word timestamps.

    These windows are a consumption view, not a replacement for the raw SRT
    or word artifact. A split is introduced on a pause, a duration limit, or
    a text-size limit so a long continuous ASR segment cannot leak into a
    narrow stage window.
    """
    grouped: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []

    def flush() -> None:
        if not current:
            return
        grouped.append(
            {
                "start_seconds": round(float(current[0]["start_seconds"]), 3),
                "end_seconds": round(float(current[-1]["end_seconds"]), 3),
                "text": " ".join(str(item["text"]) for item in current).strip(),
                "precision": "word_window",
            }
        )
        current.clear()

    for word in words:
        if not current:
            current.append(word)
            continue
        gap = float(word["start_seconds"]) - float(current[-1]["end_seconds"])
        candidate_text = " ".join([*(str(item["text"]) for item in current), str(word["text"])])
        candidate_duration = float(word["end_seconds"]) - float(current[0]["start_seconds"])
        if (
            gap > max_gap_seconds
            or candidate_duration > max_window_seconds
            or len(candidate_text) > max_characters
        ):
            flush()
        current.append(word)
    flush()
    return grouped


def read_timed_transcript_segments(info: dict[str, Any]) -> list[dict[str, Any]]:
    """Return word-derived windows when possible, otherwise raw SRT segments.

    Callers that assign evidence to a stage should use this function. The
    ``precision`` field makes the fallback explicit for audit and validation.
    """
    words = load_transcript_words(info)
    if words:
        return group_transcript_words(words)
    path = current_transcript_segments_path(info)
    segments = parse_srt_segments(path) if path is not None else []
    for segment in segments:
        segment["precision"] = "segment"
    return segments


def transcript_timing_contract(info: dict[str, Any]) -> dict[str, Any]:
    """Describe which transcript precision downstream consumers can trust."""
    words = load_transcript_words(info)
    segments = parse_srt_segments(current_transcript_segments_path(info)) if current_transcript_segments_path(info) else []
    return {
        "precision": "word" if words else "segment" if segments else "none",
        "word_count": len(words),
        "segment_count": len(segments),
        "window_attribution": "word_timestamps" if words else "not_safe_for_partial_windows",
    }
