"""Whisper transcription helpers for Flayr."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .utils import run_command, write_text


def run_whisper(
    deps: dict[str, Any],
    audio_path: Path,
    role_dir: Path,
    transcript_path: Path,
    result: dict[str, Any],
) -> None:
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
    segments_path = role_dir / "transcript.srt"
    if segments_path.exists():
        result["transcript_segments_path"] = str(segments_path)


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
