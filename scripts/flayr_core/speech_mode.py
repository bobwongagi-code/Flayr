"""Speech-mode classification for evidence priority routing."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


NON_SPEECH_LABELS = {
    "*outro music*",
    "[music]",
    "(music)",
    "music",
    "[bgm]",
    "(bgm)",
    "（音乐）",
    "（音乐渐弱）",
    "音乐",
}
PLACEHOLDER_TEXTS = {
    "（缺失）",
    "（空）",
    "whisper skipped by --skip-whisper.",
    "whisper unavailable or audio extraction failed.",
    "whisper failed. fill transcript manually.",
}


def classify_speech_mode(role_dir: Path, info: dict[str, Any]) -> dict[str, Any]:
    """Classify the dominant evidence spine for this video."""
    transcript = read_text(role_dir / "transcript.txt").strip()
    transcript_lower = transcript.lower()
    srt_segments = count_srt_segments(role_dir / "transcript.srt")
    subtitle_segments = count_subtitle_segments(role_dir / "subtitle_track.json")
    audio_exists = bool(info.get("audio_path") and Path(str(info.get("audio_path"))).is_file())
    transcription_status = str(info.get("transcription_status") or "").strip()

    effective_speech = has_effective_speech(transcript, srt_segments, transcription_status)
    if effective_speech:
        return {
            "mode": "spoken",
            "primary_evidence": ["transcript_packed", "transcript_srt", "timeline_views", "visual_frames"],
            "reason": f"有效口播存在；SRT segments={srt_segments}。",
            "has_effective_speech": True,
            "srt_segment_count": srt_segments,
            "subtitle_segment_count": subtitle_segments,
            "audio_exists": audio_exists,
        }

    if subtitle_segments > 0:
        return {
            "mode": "subtitle_driven",
            "primary_evidence": ["subtitle_track", "timeline_views", "selection_report", "visual_frames"],
            "reason": f"未检测到有效口播，但 OCR 字幕轨有 {subtitle_segments} 段。",
            "has_effective_speech": False,
            "srt_segment_count": srt_segments,
            "subtitle_segment_count": subtitle_segments,
            "audio_exists": audio_exists,
        }

    if audio_exists and transcript_lower in NON_SPEECH_LABELS:
        return {
            "mode": "music_driven",
            "primary_evidence": ["timeline_views", "audio_waveform", "selection_report", "visual_frames"],
            "reason": "转写结果为音乐/环境声标签；用画面和音频节奏组织证据。",
            "has_effective_speech": False,
            "srt_segment_count": srt_segments,
            "subtitle_segment_count": subtitle_segments,
            "audio_exists": audio_exists,
        }

    return {
        "mode": "visual_driven",
        "primary_evidence": ["selection_report", "timeline_views", "visual_frames", "shot_track"],
        "reason": "未检测到有效口播和 OCR 字幕轨；用画面变化、镜头轨和时间线视图组织证据。",
        "has_effective_speech": False,
        "srt_segment_count": srt_segments,
        "subtitle_segment_count": subtitle_segments,
        "audio_exists": audio_exists,
    }


def has_effective_speech(transcript: str, srt_segments: int, transcription_status: str) -> bool:
    text = re.sub(r"\s+", " ", transcript or "").strip()
    lowered = text.lower()
    if not text or text in PLACEHOLDER_TEXTS or lowered in PLACEHOLDER_TEXTS:
        return False
    if lowered in NON_SPEECH_LABELS or text in NON_SPEECH_LABELS:
        return False
    if srt_segments > 0:
        return True
    return transcription_status == "completed" and len(text) >= 8


def count_srt_segments(path: Path) -> int:
    if not path.is_file():
        return 0
    text = path.read_text(encoding="utf-8", errors="ignore")
    return len(re.findall(r"-->", text))


def count_subtitle_segments(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0
    segments = data.get("segments") if isinstance(data, dict) else None
    return len(segments) if isinstance(segments, list) else 0


def read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def speech_mode_prompt(mode_info: dict[str, Any]) -> str:
    mode = str(mode_info.get("mode") or "visual_driven")
    common = (
        f"speech_mode={mode}；reason={mode_info.get('reason') or ''}\n"
        "证据优先级必须随 speech_mode 切换："
    )
    if mode == "spoken":
        return (
            common +
            "有有效口播时，以 transcript_packed/transcript.srt 为口播骨架，画面、OCR、波形用于校验声画是否对齐。"
        )
    if mode == "subtitle_driven":
        return (
            common +
            "无有效口播但有字幕时，以 subtitle_track/OCR 作为文案轨，画面变化和 timeline view 校验字幕承诺是否被画面支撑；"
            "voiceover 必须留空，不得把屏幕字幕写成口播。"
        )
    if mode == "music_driven":
        return (
            common +
            "无有效口播且主要靠音乐/节奏时，以 selection_report/timeline_views/visual_frames 为主，audio_fact 只记录 BGM、节奏、音效；"
            "voiceover 必须留空，不得臆造口播。"
        )
    return (
        common +
        "无有效口播、无字幕时，以 selection_report/timeline_views/visual_frames/shot_track 为主；"
        "各阶段必须靠可见画面动作、产品出镜、效果变化、屏幕 UI 或镜头节奏归因；voiceover 必须留空。"
    )
