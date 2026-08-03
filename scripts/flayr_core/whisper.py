"""Whisper transcription helpers for Flayr."""

from __future__ import annotations

import re
import json
import math
from pathlib import Path
from typing import Any

from .utils import run_command, write_json, write_text


def run_whisper(
    deps: dict[str, Any],
    audio_path: Path,
    role_dir: Path,
    transcript_path: Path,
    result: dict[str, Any],
) -> None:
    segments_path = role_dir / "transcript.srt"
    words_path = role_dir / "transcript.words.json"
    json_path = role_dir / "transcript.json"
    result["transcript_segments_path"] = None
    result["transcript_segments_available"] = False
    result["transcript_words_path"] = None
    result["transcript_words_available"] = False
    transcript_path.unlink(missing_ok=True)
    segments_path.unlink(missing_ok=True)
    words_path.unlink(missing_ok=True)
    json_path.unlink(missing_ok=True)
    whisper_command = deps["whisper"]
    language = deps["whisper_language"]
    if language == "auto" and whisper_command in {"whisper-cli", "whisper-cpp"}:
        detected = detect_whisper_language(deps, audio_path)
        if detected:
            language = detected["language"]
            result["detected_language"] = detected["language"]
            result["detected_language_confidence"] = detected["confidence"]
        else:
            result["errors"].append("language detection failed: falling back to -l auto")
    result["transcription_language"] = language

    # 泰语走 VidLingo 专用泰语模型；语言检测仍用通用模型，仅转写阶段切换。
    # 泰语模型缺失（whisper_model_th 为 None）时回退通用模型，保证主流程不断。
    transcription_model = deps["whisper_model"]
    if language == "th" and deps.get("whisper_model_th"):
        transcription_model = deps["whisper_model_th"]
    result["transcription_model_path"] = transcription_model

    if whisper_command == "whisper":
        command = [
            "whisper",
            str(audio_path),
            "--output_format",
            "txt",
            "--output_dir",
            str(role_dir),
        ]
        if language != "auto":
            command[2:2] = ["--language", language]
        generated = audio_path.with_suffix(".txt")
    elif whisper_command in {"whisper-cli", "whisper-cpp"}:
        output_prefix = role_dir / "transcript"
        command = [
            whisper_command,
            "-l",
            language,
            "-otxt",
            "-osrt",
            "-ojf",
            "-ml",
            "60",
            "-sow",
            "-of",
            str(output_prefix),
            "-f",
            str(audio_path),
        ]
        if transcription_model:
            command[1:1] = ["-m", transcription_model]
        generated = output_prefix.with_suffix(".txt")
    else:
        command = [whisper_command, str(audio_path)]
        generated = audio_path.with_suffix(".txt")

    generated.unlink(missing_ok=True)
    completed = run_command(command)
    if completed.returncode != 0:
        write_text(transcript_path, "Whisper failed. Fill transcript manually.\n")
        result["transcription_status"] = "failed"
        result["errors"].append(f"whisper failed: {completed.stderr.strip()}")
        return

    if generated.exists():
        write_text(transcript_path, generated.read_text(encoding="utf-8"))
    else:
        write_text(transcript_path, completed.stdout.strip() + "\n")
    result["transcription_status"] = "completed"
    if segments_path.is_file() and segments_path.read_text(encoding="utf-8", errors="ignore").strip():
        result["transcript_segments_path"] = str(segments_path)
        result["transcript_segments_available"] = True
    if json_path.is_file():
        words = extract_word_timestamps(json_path)
        if words:
            write_json(
                words_path,
                {
                    "schema_version": "flayr.transcript_words.v1",
                    "source": str(json_path),
                    "words": words,
                },
            )
            result["transcript_words_path"] = str(words_path)
            result["transcript_words_available"] = True


def extract_word_timestamps(path: Path) -> list[dict[str, Any]]:
    """Normalize whisper.cpp full JSON token offsets into word-level seconds."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    segments = data.get("transcription") or data.get("segments") or []
    if not isinstance(segments, list):
        return []
    words: list[dict[str, Any]] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        tokens = segment.get("tokens") or segment.get("words") or []
        if not isinstance(tokens, list):
            continue
        for token in tokens:
            if not isinstance(token, dict):
                continue
            text = str(token.get("text") or token.get("word") or "").strip()
            offsets = token.get("offsets") if isinstance(token.get("offsets"), dict) else token
            start = _whisper_offset_seconds(offsets.get("from") if isinstance(offsets, dict) else None)
            end = _whisper_offset_seconds(offsets.get("to") if isinstance(offsets, dict) else None)
            if not text or start is None or end is None or end < start:
                continue
            item: dict[str, Any] = {
                "start_seconds": round(start, 3),
                "end_seconds": round(end, 3),
                "text": text,
            }
            probability = token.get("p")
            if isinstance(probability, (int, float)) and math.isfinite(float(probability)):
                item["probability"] = round(float(probability), 4)
            words.append(item)
    return words


def _whisper_offset_seconds(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        numeric = float(value) / 1000.0
    else:
        text = str(value or "").strip()
        if not text:
            return None
        match = re.fullmatch(r"(\d+):(\d{2}):(\d{2})(?:[.,](\d+))?", text)
        if match:
            fraction = float(f"0.{match.group(4)}") if match.group(4) else 0.0
            numeric = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3)) + fraction
        else:
            try:
                numeric = float(text) / 1000.0
            except ValueError:
                return None
    return numeric if math.isfinite(numeric) and numeric >= 0 else None


def detect_whisper_language(deps: dict[str, Any], audio_path: Path) -> dict[str, Any] | None:
    command = [
        deps["whisper"],
        "-l",
        "auto",
        "--detect-language",
        "-f",
        str(audio_path),
    ]
    if deps["whisper_model"]:
        command[1:1] = ["-m", deps["whisper_model"]]

    completed = run_command(command)
    if completed.returncode != 0:
        return None

    text = f"{completed.stdout}\n{completed.stderr}"
    match = re.search(r"auto-detected language:\s*([a-z-]+)\s*\(p\s*=\s*([0-9.]+)\)", text)
    if not match:
        return None

    return {
        "language": match.group(1),
        "confidence": float(match.group(2)),
    }
