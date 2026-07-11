"""Video and audio artifact extraction for Flayr."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .artifacts import (
    build_frame_manifest,
    build_stage_frame_manifest,
    focus_frame_sort_key,
    numbered_frame_sort_key,
)
from .utils import run_command, write_json


def probe_duration_seconds(video_path: Path) -> float | None:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    completed = run_command(command)
    if completed.returncode != 0:
        return None
    try:
        return float(completed.stdout.strip())
    except ValueError:
        return None


def extract_frames(
    video_path: Path,
    frames_dir: Path,
    focus_frames_dir: Path,
    result: dict[str, Any],
) -> None:
    for stale_frame in frames_dir.glob("frame_*.jpg"):
        stale_frame.unlink(missing_ok=True)
    for stale_frame in focus_frames_dir.glob("*.jpg"):
        stale_frame.unlink(missing_ok=True)
    pattern = frames_dir / "frame_%04d.jpg"
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-vf",
        "fps=1",
        str(pattern),
    ]
    completed = run_command(command)
    if completed.returncode != 0:
        result["errors"].append(f"frame extraction failed: {completed.stderr.strip()}")
        return
    frames = sorted(frames_dir.glob("frame_*.jpg"), key=numbered_frame_sort_key)
    result["frame_count"] = len(frames)
    frame_manifest = build_frame_manifest(frames)
    result["frame_manifest_path"] = str(frames_dir / "manifest.json")
    result["frames"] = frame_manifest
    write_json(frames_dir / "manifest.json", frame_manifest)
    stage_frames = build_stage_frame_manifest(frame_manifest, result.get("duration_seconds"))
    result["stage_frame_manifest_path"] = str(frames_dir / "stage_frames.json")
    result["stage_frames"] = stage_frames
    write_json(frames_dir / "stage_frames.json", stage_frames)
    extract_focus_frames(video_path, focus_frames_dir, result)


def extract_focus_frames(video_path: Path, focus_frames_dir: Path, result: dict[str, Any]) -> None:
    duration = result.get("duration_seconds")
    if not duration:
        result["errors"].append("duration unavailable: skipped focus frame extraction")
        return

    segments: list[tuple[str, float, float]] = [("hook", 0.0, min(5.0, duration))]
    if duration > 5:
        cta_start = max(0.0, duration - 5.0)
        if cta_start > 5.0:
            segments.append(("cta", cta_start, min(5.0, duration - cta_start)))

    manifest: list[dict[str, Any]] = []
    for label, start, length in segments:
        pattern = focus_frames_dir / f"{label}_%04d.jpg"
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{length:.3f}",
            "-i",
            str(video_path),
            "-vf",
            "fps=2",
            str(pattern),
        ]
        completed = run_command(command)
        if completed.returncode != 0:
            result["errors"].append(f"{label} focus frame extraction failed: {completed.stderr.strip()}")
            continue

        frames = sorted(focus_frames_dir.glob(f"{label}_*.jpg"), key=focus_frame_sort_key)
        for index, frame in enumerate(frames):
            timestamp = start + index * 0.5
            manifest.append(
                {
                    "label": label,
                    "timestamp_seconds": round(timestamp, 2),
                    "path": str(frame),
                    "filename": frame.name,
                }
            )

    result["focus_frame_count"] = len(list(focus_frames_dir.glob("*.jpg")))
    result["focus_frame_manifest_path"] = str(focus_frames_dir / "manifest.json")
    result["focus_frames"] = manifest
    write_json(focus_frames_dir / "manifest.json", manifest)


def extract_audio(video_path: Path, audio_path: Path, result: dict[str, Any]) -> None:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(audio_path),
    ]
    completed = run_command(command)
    if completed.returncode != 0:
        result["errors"].append(f"audio extraction failed: {completed.stderr.strip()}")
        return
    result["audio_path"] = str(audio_path)
