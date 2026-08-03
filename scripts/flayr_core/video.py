"""Video and audio artifact extraction for Flayr."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from .artifacts import (
    build_frame_manifest,
    build_stage_frame_manifest,
    focus_frame_sort_key,
    numbered_frame_sort_key,
    parse_timestamp_seconds,
    stage_time_ranges,
)


MAX_SUPPLEMENTAL_ANCHOR_FRAMES = 48
MIN_ANCHOR_GAP_SECONDS = 0.35
MAX_BASE_FPS = 2.0
MAX_FOCUS_FRAMES_PER_ROLE = 20
from .utils import run_command, write_json
from .resources import (
    ResourceBudget,
    ResourceBudgetExceeded,
    ResourceLimits,
    current_budget,
    finite_nonnegative,
)


def _artifact_bytes(paths: list[Path]) -> int:
    total = 0
    for path in paths:
        if not path.is_file():
            continue
        size = path.stat().st_size
        if size < 0:
            raise OSError(f"negative artifact size: {path}")
        total += size
    return total


def _reserve_artifacts(
    paths: list[Path],
    budget: ResourceBudget,
    result: dict[str, Any],
    label: str,
) -> bool:
    try:
        budget.reserve_local_artifact(_artifact_bytes(paths))
    except (OSError, ResourceBudgetExceeded) as exc:
        for path in paths:
            path.unlink(missing_ok=True)
        result["errors"].append(f"{label} exceeded the local media artifact budget: {exc}")
        return False
    return True


def reserve_existing_media_artifacts(role_dir: Path, budget: ResourceBudget) -> int:
    """Account for cached media so --reuse-preprocessing cannot bypass disk limits."""
    paths = [
        *role_dir.glob("frames/frame_*.jpg"),
        *role_dir.glob("frames/anchors/*.jpg"),
        *role_dir.glob("focus_frames/*.jpg"),
    ]
    audio_path = role_dir / "audio.wav"
    if audio_path.is_file():
        paths.append(audio_path)
    size = _artifact_bytes(paths)
    budget.reserve_local_artifact(size)
    return size


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
    completed = run_command(command, timeout_seconds=30, max_output_bytes=4096)
    if completed.returncode != 0:
        return None
    try:
        value = finite_nonnegative(completed.stdout.strip(), "video duration")
        return value
    except ValueError:
        return None


_SHOWINFO_PTS_RE = re.compile(r"pts_time:(-?(?:\d+(?:\.\d*)?|\.\d+))")


def _showinfo_timestamps(stderr: str, expected_count: int, offset: float = 0.0) -> list[float | None]:
    """Read output-frame PTS from ffmpeg showinfo; incomplete PTS stays unknown."""
    timestamps: list[float] = []
    for raw in _SHOWINFO_PTS_RE.findall(stderr or ""):
        timestamp = parse_timestamp_seconds(raw)
        if timestamp is None:
            continue
        value = timestamp + offset
        if not math.isfinite(value) or value < 0:
            continue
        timestamps.append(round(value, 6))
    if len(timestamps) != expected_count:
        return [None] * expected_count
    return timestamps


def extract_frames(
    video_path: Path,
    frames_dir: Path,
    focus_frames_dir: Path,
    result: dict[str, Any],
) -> None:
    budget = current_budget() or ResourceBudget()
    for stale_frame in frames_dir.glob("frame_*.jpg"):
        stale_frame.unlink(missing_ok=True)
    for stale_frame in focus_frames_dir.glob("*.jpg"):
        stale_frame.unlink(missing_ok=True)
    pattern = frames_dir / "frame_%04d.jpg"
    duration = result.get("duration_seconds")
    try:
        duration = finite_nonnegative(
            duration,
            "video duration",
            maximum=budget.limits.max_source_duration,
        )
    except ValueError:
        result["errors"].append("duration unavailable or invalid: skipped frame extraction")
        return
    # Reserve roughly half of the run-level frame budget for this role and
    # leave room for its first/last focus frames. Short videos therefore get
    # up to 2fps; long videos degrade deterministically instead of exceeding
    # the shared budget.
    role_frame_ceiling = max(
        1,
        (
            budget.limits.max_extracted_frames
            - MAX_FOCUS_FRAMES_PER_ROLE * 2
            - MAX_SUPPLEMENTAL_ANCHOR_FRAMES * 2
        )
        // 2,
    )
    base_fps = min(MAX_BASE_FPS, role_frame_ceiling / max(duration, 1.0))
    base_fps = max(1.0 / max(duration, 1.0), base_fps)
    expected_frames = max(1, int(math.ceil(duration * base_fps)))
    reserved_frames = 0
    if budget is not None:
        try:
            budget.reserve_frames(expected_frames)
            reserved_frames = expected_frames
        except ResourceBudgetExceeded as exc:
            result["errors"].append(str(exc))
            return
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "info",
        "-y",
        "-i",
        str(video_path),
        "-vf",
        f"fps={base_fps:.3f},showinfo",
        "-t",
        f"{duration:.3f}",
        "-frames:v",
        str(expected_frames),
        "-fs",
        str(max(1, budget.limits.max_local_artifact_bytes - budget.local_artifact_bytes)),
        str(pattern),
    ]
    completed = run_command(command, budget=budget)
    if completed.returncode != 0:
        for frame in frames_dir.glob("frame_*.jpg"):
            frame.unlink(missing_ok=True)
        if budget is not None and reserved_frames:
            budget.release_frames(reserved_frames)
        result["errors"].append(f"frame extraction failed: {completed.stderr.strip()}")
        return
    frames = sorted(frames_dir.glob("frame_*.jpg"), key=numbered_frame_sort_key)
    if not frames:
        budget.release_frames(reserved_frames)
        result["errors"].append("frame extraction produced no frames")
        return
    if not _reserve_artifacts(frames, budget, result, "base frames"):
        budget.release_frames(reserved_frames)
        return
    if budget is not None and reserved_frames > len(frames):
        budget.release_frames(reserved_frames - len(frames))
    result["frame_count"] = len(frames)
    result["base_frame_fps"] = round(base_fps, 3)
    frame_timestamps = _showinfo_timestamps(completed.stderr, len(frames))
    if any(timestamp is None for timestamp in frame_timestamps):
        result["errors"].append("frame timestamps unavailable or incomplete; frame evidence timestamps omitted")
    frame_manifest = build_frame_manifest(frames, frame_timestamps)
    result["frame_manifest_path"] = str(frames_dir / "manifest.json")
    result["frames"] = frame_manifest
    write_json(frames_dir / "manifest.json", frame_manifest)
    stage_frames = build_stage_frame_manifest(frame_manifest, result.get("duration_seconds"))
    result["stage_frame_manifest_path"] = str(frames_dir / "stage_frames.json")
    result["stage_frames"] = stage_frames
    write_json(frames_dir / "stage_frames.json", stage_frames)
    extract_focus_frames(video_path, focus_frames_dir, result, budget=budget)


def extract_focus_frames(
    video_path: Path,
    focus_frames_dir: Path,
    result: dict[str, Any],
    budget: ResourceBudget | None = None,
) -> None:
    budget = budget or current_budget() or ResourceBudget()
    try:
        duration = finite_nonnegative(
            result.get("duration_seconds"),
            "video duration",
            maximum=budget.limits.max_source_duration,
        )
    except ValueError:
        result["errors"].append("duration unavailable: skipped focus frame extraction")
        return

    segments: list[tuple[str, float, float]] = [("hook", 0.0, min(5.0, duration))]
    if duration > 5:
        cta_start = max(0.0, duration - 5.0)
        if cta_start > 5.0:
            segments.append(("cta", cta_start, min(5.0, duration - cta_start)))

    manifest: list[dict[str, Any]] = []
    for label, start, length in segments:
        expected_frames = max(1, int(math.ceil(length * 2.0)))
        reserved_frames = 0
        if budget is not None:
            try:
                budget.reserve_frames(expected_frames)
                reserved_frames = expected_frames
            except ResourceBudgetExceeded as exc:
                result["errors"].append(f"{label} focus frame extraction skipped: {exc}")
                continue
        pattern = focus_frames_dir / f"{label}_%04d.jpg"
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "info",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{length:.3f}",
            "-i",
            str(video_path),
            "-vf",
            "fps=2,showinfo",
            "-frames:v",
            str(expected_frames),
            "-fs",
            str(max(1, budget.limits.max_local_artifact_bytes - budget.local_artifact_bytes)),
            str(pattern),
        ]
        completed = run_command(command, budget=budget)
        if completed.returncode != 0:
            for frame in focus_frames_dir.glob(f"{label}_*.jpg"):
                frame.unlink(missing_ok=True)
            if budget is not None and reserved_frames:
                budget.release_frames(reserved_frames)
            result["errors"].append(f"{label} focus frame extraction failed: {completed.stderr.strip()}")
            continue

        frames = sorted(focus_frames_dir.glob(f"{label}_*.jpg"), key=focus_frame_sort_key)
        if not frames:
            budget.release_frames(reserved_frames)
            result["errors"].append(f"{label} focus frame extraction produced no frames")
            continue
        if not _reserve_artifacts(frames, budget, result, f"{label} focus frames"):
            budget.release_frames(reserved_frames)
            continue
        if budget is not None and reserved_frames > len(frames):
            budget.release_frames(reserved_frames - len(frames))
        frame_timestamps = _showinfo_timestamps(completed.stderr, len(frames), offset=start)
        if any(timestamp is None for timestamp in frame_timestamps):
            result["errors"].append(
                f"{label} focus frame timestamps unavailable or incomplete; frame evidence timestamps omitted"
            )
        for frame, timestamp in zip(frames, frame_timestamps):
            manifest.append(
                {
                    "label": label,
                    "timestamp_seconds": round(timestamp, 2) if timestamp is not None else None,
                    "path": str(frame),
                    "filename": frame.name,
                }
            )

    result["focus_frame_count"] = len(list(focus_frames_dir.glob("*.jpg")))
    result["focus_frame_manifest_path"] = str(focus_frames_dir / "manifest.json")
    result["focus_frames"] = manifest
    write_json(focus_frames_dir / "manifest.json", manifest)


def extract_anchor_frames(
    video_path: Path,
    frames_dir: Path,
    result: dict[str, Any],
    budget: ResourceBudget | None = None,
) -> None:
    """Extract one accurate frame at structural boundaries missed by base sampling.

    The base corpus remains bounded and deterministic.  This supplemental pass
    is driven by shot cuts, OCR subtitle boundaries, and stage window edges so
    a one-second sampler cannot silently turn a short CTA or text change into
    an unavailable visual fact.
    """
    budget = budget or current_budget() or ResourceBudget()
    duration = parse_timestamp_seconds(result.get("duration_seconds"))
    if duration is None or duration <= 0 or not video_path.is_file():
        return
    anchor_dir = frames_dir / "anchors"
    anchor_dir.mkdir(parents=True, exist_ok=True)
    for stale in anchor_dir.glob("anchor_*.jpg"):
        stale.unlink(missing_ok=True)

    existing = [
        parse_timestamp_seconds(item.get("timestamp_seconds"))
        for item in result.get("frames", [])
        if isinstance(item, dict)
    ]
    existing_times = [value for value in existing if value is not None]
    anchors = _collect_anchor_times(result, duration)
    filtered: list[tuple[float, str]] = []
    for timestamp, reason in anchors:
        if any(abs(timestamp - previous) < MIN_ANCHOR_GAP_SECONDS for previous in existing_times):
            continue
        if filtered and abs(timestamp - filtered[-1][0]) < MIN_ANCHOR_GAP_SECONDS:
            filtered[-1] = (filtered[-1][0], f"{filtered[-1][1]}+{reason}")
            continue
        filtered.append((timestamp, reason))
        if len(filtered) >= MAX_SUPPLEMENTAL_ANCHOR_FRAMES:
            break

    if not filtered:
        result["analysis_anchor_frame_count"] = 0
        return

    manifest = [item for item in result.get("frames", []) if isinstance(item, dict)]
    for index, (timestamp, reason) in enumerate(filtered, start=1):
        if budget is not None:
            try:
                budget.reserve_frames(1)
            except ResourceBudgetExceeded as exc:
                result["errors"].append(f"supplemental anchor frame skipped: {exc}")
                break
        output = anchor_dir / f"anchor_{index:04d}.jpg"
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "info",
            "-y",
            "-i",
            str(video_path),
            "-ss",
            f"{timestamp:.3f}",
            "-frames:v",
            "1",
            "-vf",
            "showinfo",
            "-q:v",
            "2",
            "-fs",
            str(max(1, budget.limits.max_local_artifact_bytes - budget.local_artifact_bytes)),
            str(output),
        ]
        completed = run_command(command, budget=budget)
        if completed.returncode != 0 or not output.is_file():
            output.unlink(missing_ok=True)
            if budget is not None:
                budget.release_frames(1)
            result["errors"].append(
                f"supplemental anchor frame failed at {timestamp:.2f}s: {completed.stderr.strip()}"
            )
            continue
        if not _reserve_artifacts([output], budget, result, "supplemental anchor frames"):
            if budget is not None:
                budget.release_frames(1)
            continue
        actual = _showinfo_timestamps(completed.stderr, 1)[0]
        if actual is None or actual > duration + 0.05 or abs(actual - timestamp) > 0.5:
            actual = timestamp
        entry = {
            "timestamp_seconds": round(actual, 3),
            "requested_timestamp_seconds": round(timestamp, 3),
            "path": str(output),
            "filename": output.name,
            "source": "structural_anchor",
            "anchor_type": reason,
        }
        manifest.append(entry)

    result["frames"] = sorted(manifest, key=lambda item: parse_timestamp_seconds(item.get("timestamp_seconds")) or 0.0)
    result["frame_count"] = len(result["frames"])
    result["analysis_anchor_frame_count"] = sum(
        1 for item in result["frames"] if item.get("source") == "structural_anchor"
    )
    result["frame_manifest_path"] = str(frames_dir / "manifest.json")
    write_json(frames_dir / "manifest.json", result["frames"])


def _collect_anchor_times(result: dict[str, Any], duration: float) -> list[tuple[float, str]]:
    """Collect deterministic anchor times in stable temporal order."""
    anchors: dict[float, set[str]] = {}

    def add(value: Any, reason: str) -> None:
        timestamp = parse_timestamp_seconds(value)
        if timestamp is None or not math.isfinite(timestamp) or timestamp <= 0 or timestamp >= duration:
            return
        anchors.setdefault(round(timestamp, 3), set()).add(reason)

    for stage_name, _label, start, end in stage_time_ranges(duration):
        add(start, "stage_boundary")
        add(end, "stage_boundary")

    for key, reason in (("shot_track_path", "scene_boundary"), ("subtitle_track_path", "subtitle_boundary")):
        raw_path = str(result.get(key) or "").strip()
        if not raw_path:
            continue
        try:
            data = json.loads(Path(raw_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if key == "shot_track_path":
            items = data.get("shots", []) if isinstance(data, dict) else []
        else:
            items = data.get("segments", []) if isinstance(data, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            add(item.get("start_sec"), reason)
            add(item.get("end_sec"), reason)

    ordered: list[tuple[float, str]] = []
    for timestamp in sorted(anchors):
        ordered.append((timestamp, "+".join(sorted(anchors[timestamp]))))
    return ordered


def extract_audio(video_path: Path, audio_path: Path, result: dict[str, Any]) -> None:
    budget = current_budget() or ResourceBudget()
    audio_path.unlink(missing_ok=True)
    try:
        duration = finite_nonnegative(
            result.get("duration_seconds"),
            "video duration",
            maximum=budget.limits.max_source_duration,
        )
    except ValueError:
        result["errors"].append("duration unavailable or invalid: skipped audio extraction")
        return
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
        "-t",
        f"{duration:.3f}",
        "-fs",
        str(max(1, budget.limits.max_local_artifact_bytes - budget.local_artifact_bytes)),
        str(audio_path),
    ]
    completed = run_command(command, budget=budget)
    if completed.returncode != 0:
        audio_path.unlink(missing_ok=True)
        result["errors"].append(f"audio extraction failed: {completed.stderr.strip()}")
        return
    if not audio_path.is_file():
        result["errors"].append("audio extraction produced no audio")
        return
    if not _reserve_artifacts([audio_path], budget, result, "audio"):
        return
    result["audio_path"] = str(audio_path)
