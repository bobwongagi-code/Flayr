"""Video evidence artifacts and the canonical analysis-frame manifest."""

from __future__ import annotations

import html
import json
import math
import os
import re
import wave
from pathlib import Path
from typing import Any

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - optional artifact dependency
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    ImageFont = None  # type: ignore[assignment]

from .artifacts import (
    build_stage_frame_manifest,
    get_analysis_frame_entries,
    get_focus_frame_entries,
    get_frame_entries,
    get_stage_frame_entries,
    parse_timestamp_seconds,
    resolve_artifact_path,
    sample_evenly,
    select_frames_for_time_range,
    stage_time_ranges,
)
from .frame_selection import (
    ACTION_CHANGE_THRESHOLD_PERCENT,
    GLOBAL_CHANGE_THRESHOLD_PERCENT,
    LOCAL_CHANGE_THRESHOLD_PERCENT,
    MAX_DENSITY_GAP_SECONDS,
    build_analysis_frame_manifest,
)
from .utils import write_json, write_text
from .transcript import (
    current_transcript_segments_path,
    group_transcript_words,
    load_transcript_words,
    parse_srt_segments,
    parse_srt_time_range,
    parse_srt_timestamp,
    parse_transcript_words,
)

TRANSCRIPT_WINDOW_CONTRACT_VERSION = 1

def build_video_evidence_artifacts(role_dir: Path, info: dict[str, Any]) -> dict[str, Any]:
    """Build the canonical evidence manifest and its audit views."""
    result: dict[str, Any] = {
        "status": "completed",
        "errors": [],
        "frame_selection_report_path": None,
        "frame_selection_report_html_path": None,
        "analysis_frame_manifest_path": None,
        "analysis_frames": [],
        "analysis_stage_frame_manifest_path": None,
        "analysis_stage_frames": [],
        "contact_sheets_dir": None,
        "timeline_views_dir": None,
        "timeline_views": [],
        "transcript_pack_path": None,
        "transcript_pack_json_path": None,
        "transcript_windowed_path": None,
        "transcript_windowed_json_path": None,
        "transcript_window_contract_version": TRANSCRIPT_WINDOW_CONTRACT_VERSION,
        "audit_path": None,
    }
    word_path = resolve_artifact_path(info, info.get("transcript_words_path"), require_file=True)
    if word_path is not None:
        result["transcript_words_path"] = str(word_path)

    try:
        selection = build_frame_selection_report(role_dir, info)
        result.update(selection)
    except Exception as exc:  # pragma: no cover - artifact generation should not break analysis
        result["errors"].append(f"frame selection report failed: {exc}")

    # Downstream views must consume the same canonical manifest that the model
    # receives on the first generation, not the pre-selection raw metadata.
    view_info = dict(info)
    view_info.update(result)
    previous_evidence = info.get("video_evidence") if isinstance(info.get("video_evidence"), dict) else {}
    view_info["video_evidence"] = {**previous_evidence, **result}

    try:
        contact_sheets = build_contact_sheets(role_dir, view_info)
        result.update(contact_sheets)
    except Exception as exc:  # pragma: no cover
        result["errors"].append(f"contact sheets failed: {exc}")

    try:
        transcript_pack = build_transcript_pack(role_dir, view_info)
        result.update(transcript_pack)
    except Exception as exc:  # pragma: no cover
        result["errors"].append(f"transcript pack failed: {exc}")

    try:
        timeline_views = build_timeline_views(role_dir, view_info)
        result.update(timeline_views)
    except Exception as exc:  # pragma: no cover
        result["errors"].append(f"timeline views failed: {exc}")

    audit = audit_video_evidence(role_dir, result, info)
    result["audit_path"] = audit.get("path")
    if audit.get("warnings"):
        result["errors"].extend(audit["warnings"])

    if result["errors"]:
        result["status"] = "partial"
    return result


def audit_video_evidence(
    role_dir: Path,
    result: dict[str, Any],
    info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Self-check artifacts and the semantic integrity of selected evidence."""
    checks = [
        ("selection_report", result.get("frame_selection_report_path")),
        ("selection_report_html", result.get("frame_selection_report_html_path")),
        ("analysis_frame_manifest", result.get("analysis_frame_manifest_path")),
        ("analysis_stage_frame_manifest", result.get("analysis_stage_frame_manifest_path")),
    ]
    if result.get("transcript_pack_path"):
        checks.extend(
            [
                ("transcript_pack", result.get("transcript_pack_path")),
                ("transcript_pack_json", result.get("transcript_pack_json_path")),
            ]
        )
    if result.get("transcript_windowed_path"):
        checks.extend(
            [
                ("transcript_windowed", result.get("transcript_windowed_path")),
                ("transcript_windowed_json", result.get("transcript_windowed_json_path")),
            ]
        )
    if result.get("transcript_words_path"):
        checks.append(("transcript_words", result.get("transcript_words_path")))
    views = result.get("timeline_views") if isinstance(result.get("timeline_views"), list) else []
    for item in views:
        if isinstance(item, dict):
            checks.append((f"timeline_view_{item.get('label') or 'unknown'}", item.get("path")))
    contact_sheets = result.get("contact_sheets") if isinstance(result.get("contact_sheets"), list) else []
    for index, path in enumerate(contact_sheets, start=1):
        checks.append((f"contact_sheet_{index:02d}", path))

    audit_items = []
    warnings = []
    for name, raw_path in checks:
        path = Path(str(raw_path or ""))
        exists = path.is_file()
        if not exists:
            warnings.append(f"video evidence missing: {name}")
        audit_items.append({"name": name, "path": str(path) if raw_path else "", "exists": exists})

    coverage = audit_analysis_frame_manifest(info or {}, result)
    warnings.extend(coverage.get("warnings", []))
    canonical_paths = {
        str(entry.get("path") or "")
        for entry in result.get("analysis_frames", [])
        if isinstance(entry, dict) and str(entry.get("path") or "")
    }
    for item in views:
        if not isinstance(item, dict):
            continue
        frame_paths = item.get("frame_paths")
        if not isinstance(frame_paths, list):
            warnings.append(f"timeline view missing frame provenance: {item.get('label') or 'unknown'}")
            continue
        if item.get("selection_source") != "analysis_frame_manifest":
            warnings.append(f"timeline view did not use canonical manifest: {item.get('label') or 'unknown'}")
        if item.get("transcript_scope") == "insufficient_precision":
            warnings.append(
                f"timeline view transcript lacks window-level timing: {item.get('label') or 'unknown'}"
            )
        outside = [str(path) for path in frame_paths if str(path) not in canonical_paths]
        if outside:
            warnings.append(
                f"timeline view uses frames outside canonical manifest: "
                f"{item.get('label') or 'unknown'}: {', '.join(outside[:3])}"
            )
    audit = {
        "status": "pass" if not warnings else "warn",
        "warnings": warnings,
        "items": audit_items,
        "coverage": coverage,
    }
    path = role_dir / "video_evidence_audit.json"
    write_json(path, audit)
    audit["path"] = str(path)
    return audit


def audit_analysis_frame_manifest(info: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Validate selected frame paths, time bounds, uniqueness, and coverage."""
    duration = parse_timestamp_seconds(info.get("duration_seconds"))
    frames = result.get("analysis_frames") if isinstance(result.get("analysis_frames"), list) else []
    warnings: list[str] = []
    paths: set[str] = set()
    invalid_timestamps = 0
    missing_paths = 0
    timestamps: list[float] = []
    reason_counts: dict[str, int] = {}
    for entry in frames:
        if not isinstance(entry, dict):
            warnings.append("analysis frame manifest contains a non-object entry")
            continue
        path = str(entry.get("path") or "")
        if not path or not Path(path).is_file():
            missing_paths += 1
        if path and str(info.get("work_dir") or "").strip() and resolve_artifact_path(
            info, path, require_root=True
        ) is None:
            warnings.append(f"analysis frame path escapes role directory: {path}")
        if path in paths and path:
            warnings.append(f"analysis frame manifest contains duplicate path: {path}")
        if path:
            paths.add(path)
        timestamp = parse_timestamp_seconds(entry.get("timestamp_seconds"))
        if timestamp is None or not math.isfinite(timestamp) or timestamp < 0:
            invalid_timestamps += 1
        else:
            timestamps.append(timestamp)
            if duration is not None and timestamp > duration + 0.05:
                warnings.append(f"analysis frame timestamp exceeds source duration: {timestamp:.2f}s > {duration:.2f}s")
        reasons = entry.get("selection_reasons") if isinstance(entry.get("selection_reasons"), list) else []
        for reason in reasons:
            reason_counts[str(reason)] = reason_counts.get(str(reason), 0) + 1

    if missing_paths:
        warnings.append(f"analysis frame manifest has {missing_paths} missing frame paths")
    if invalid_timestamps:
        warnings.append(f"analysis frame manifest has {invalid_timestamps} invalid timestamps")
    timestamps.sort()
    max_gap = max((right - left for left, right in zip(timestamps, timestamps[1:])), default=0.0)
    if len(timestamps) > 1 and max_gap > MAX_DENSITY_GAP_SECONDS * 2.0:
        warnings.append(
            f"analysis frame manifest has a sparse gap of {max_gap:.2f}s; "
            f"expected <= {MAX_DENSITY_GAP_SECONDS * 2.0:.2f}s"
        )
    if len(frames) == 0:
        warnings.append("analysis frame manifest is empty")

    stage_frames = result.get("analysis_stage_frames") if isinstance(result.get("analysis_stage_frames"), list) else []
    stage_counts: dict[str, int] = {}
    for entry in stage_frames:
        if isinstance(entry, dict):
            stage = str(entry.get("stage") or "unknown")
            stage_counts[stage] = stage_counts.get(stage, 0) + 1
            path = str(entry.get("path") or "")
            if path and path not in paths:
                warnings.append(f"analysis stage frame is outside canonical manifest: {path}")

    direct_stage_coverage: dict[str, int] = {}
    if duration is not None:
        for stage_name, _label, start, end in stage_time_ranges(duration):
            count = sum(start <= timestamp <= end for timestamp in timestamps)
            direct_stage_coverage[stage_name] = count
            if count == 0 and end - start > 0.5:
                warnings.append(f"analysis frame manifest has no direct coverage for {stage_name}")

    return {
        "status": "pass" if not warnings else "warn",
        "frame_count": len(frames),
        "stage_frame_count": len(stage_frames),
        "max_timestamp_gap_seconds": round(max_gap, 3),
        "reason_counts": reason_counts,
        "stage_counts": stage_counts,
        "direct_stage_coverage": direct_stage_coverage,
        "warnings": warnings,
    }


def build_frame_selection_report(role_dir: Path, info: dict[str, Any]) -> dict[str, Any]:
    selection = build_analysis_frame_manifest(info)
    frames = selection.get("frames") if isinstance(selection, dict) else []
    decisions = selection.get("decisions") if isinstance(selection, dict) else []
    if not isinstance(frames, list) or not frames:
        return {}

    kept_count = len(frames)

    report = {
        "strategy": {
            "version": selection.get("strategy_version", "v2"),
            "signals": [
                "global_rgb",
                "local_3x3_regions",
                "edge_motion_like",
                "scene_boundaries",
                "subtitle_boundaries",
                "speech_boundaries",
            ],
            "global_threshold_percent": GLOBAL_CHANGE_THRESHOLD_PERCENT,
            "local_threshold_percent": LOCAL_CHANGE_THRESHOLD_PERCENT,
            "action_threshold_percent": ACTION_CHANGE_THRESHOLD_PERCENT,
            "density_floor_seconds": MAX_DENSITY_GAP_SECONDS,
            "note": "Canonical manifest controls visual inputs; original frames remain available for audit.",
        },
        "frame_count": len(decisions) if isinstance(decisions, list) else 0,
        "kept_count": kept_count,
        "dropped_count": max(0, (len(decisions) if isinstance(decisions, list) else 0) - kept_count),
        "anchor_count": selection.get("anchor_count", 0),
        "decisions": decisions if isinstance(decisions, list) else [],
        "selected_frames": frames,
    }
    frames_dir = Path(str(info.get("frames_dir") or role_dir / "frames"))
    json_path = frames_dir / "selection_report.json"
    html_path = frames_dir / "selection_report.html"
    manifest_path = frames_dir / "analysis_manifest.json"
    write_json(json_path, report)
    write_json(manifest_path, frames)
    write_selection_report_html(html_path, report)
    analysis_stage_frames = build_stage_frame_manifest(frames, info.get("duration_seconds"))
    stage_manifest_path = frames_dir / "analysis_stage_frames.json"
    write_json(stage_manifest_path, analysis_stage_frames)
    return {
        "frame_selection_report_path": str(json_path),
        "frame_selection_report_html_path": str(html_path),
        "dedup_kept_frame_count": kept_count,
        "analysis_frame_manifest_path": str(manifest_path),
        "analysis_frames": frames,
        "analysis_frame_count": len(frames),
        "analysis_stage_frame_manifest_path": str(stage_manifest_path),
        "analysis_stage_frames": analysis_stage_frames,
    }


def write_selection_report_html(path: Path, report: dict[str, Any]) -> None:
    rows = []
    for item in report.get("decisions", []):
        raw_frame = str(item.get("path") or "").strip()
        try:
            rel = os.path.relpath(raw_frame, path.parent) if raw_frame else str(item.get("filename") or "")
        except (TypeError, ValueError):
            rel = str(item.get("filename") or "")
        rel = html.escape(rel.replace(os.sep, "/"))
        status = "keep" if item.get("kept") else "drop"
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('timestamp_seconds', '')))}s</td>"
            f"<td><img src=\"{rel}\" alt=\"{rel}\"></td>"
            f"<td class=\"{status}\">{status}</td>"
            f"<td>g={html.escape(str(item.get('global_diff_percent')))} / "
            f"l={html.escape(str(item.get('local_diff_percent')))} / "
            f"a={html.escape(str(item.get('action_diff_percent')))}</td>"
            f"<td>{html.escape(str(item.get('reason') or ''))}</td>"
            "</tr>"
        )
    content = f"""<!doctype html>
<meta charset="utf-8">
<title>Frame selection report</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:24px;color:#17202a}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #d8dee9;padding:8px;text-align:left}}
img{{width:120px;border-radius:4px}}.keep{{color:#087f5b;font-weight:700}}.drop{{color:#c92a2a;font-weight:700}}
</style>
<h1>Frame selection report</h1>
<p>Selected {report.get('kept_count')} / {report.get('frame_count')} frames for analysis. Original frames remain available for audit.</p>
<table><thead><tr><th>Time</th><th>Frame</th><th>Decision</th><th>Diff % (global/local/action)</th><th>Reason</th></tr></thead><tbody>
{''.join(rows)}
</tbody></table>
"""
    write_text(path, content)


def build_contact_sheets(role_dir: Path, info: dict[str, Any]) -> dict[str, Any]:
    if Image is None or ImageDraw is None:
        return {}
    out_dir = role_dir / "contact_sheets"
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    focus_entries = get_focus_frame_entries(info)
    for label in ("hook", "cta"):
        entries = [entry for entry in focus_entries if entry.get("label") == label]
        if entries:
            out_path = out_dir / f"{label}.jpg"
            write_contact_sheet(entries, out_path, title=f"{label.upper()} focus frames")
            written.append(str(out_path))

    by_stage: dict[str, list[dict[str, Any]]] = {}
    for entry in get_stage_frame_entries(info):
        stage = str(entry.get("stage") or "stage").replace("/", "-")
        by_stage.setdefault(stage, []).append(entry)
    for index, (stage, entries) in enumerate(by_stage.items(), start=1):
        out_path = out_dir / f"stage_{index:02d}.jpg"
        write_contact_sheet(entries, out_path, title=stage)
        written.append(str(out_path))

    return {"contact_sheets_dir": str(out_dir), "contact_sheets": written}


def write_contact_sheet(
    entries: list[dict[str, Any]],
    out_path: Path,
    title: str,
    cols: int = 3,
    cell_width: int = 260,
    cell_height: int = 360,
) -> None:
    entries = [entry for entry in entries if Path(str(entry.get("path") or "")).exists()]
    if not entries:
        return
    rows = math.ceil(len(entries) / cols)
    title_height = 42
    label_height = 30
    width = cols * cell_width
    height = title_height + rows * (cell_height + label_height)
    canvas = Image.new("RGB", (width, height), "#f8fafc")
    draw = ImageDraw.Draw(canvas)
    title_font = load_font(24)
    label_font = load_font(18)
    draw.text((16, 10), title, fill="#111827", font=title_font)

    for index, entry in enumerate(entries):
        col = index % cols
        row = index // cols
        x = col * cell_width
        y = title_height + row * (cell_height + label_height)
        frame_path = Path(str(entry.get("path") or ""))
        with Image.open(frame_path) as image:
            tile = fit_image(image.convert("RGB"), cell_width, cell_height)
        canvas.paste(tile, (x, y))
        timestamp = format_seconds(entry.get("timestamp_seconds"))
        label = f"{timestamp} · {entry.get('label') or entry.get('stage') or 'frame'}"
        draw.text((x + 10, y + cell_height + 4), label, fill="#475569", font=label_font)

    canvas.save(out_path, quality=92)


def fit_image(image: Image.Image, width: int, height: int) -> Image.Image:
    canvas = Image.new("RGB", (width, height), "#ffffff")
    image.thumbnail((width, height), Image.Resampling.LANCZOS)
    x = (width - image.width) // 2
    y = (height - image.height) // 2
    canvas.paste(image, (x, y))
    return canvas


def build_transcript_pack(role_dir: Path, info: dict[str, Any]) -> dict[str, Any]:
    srt_path = current_transcript_segments_path(info)
    segments = parse_srt_segments(srt_path) if srt_path is not None else []
    result: dict[str, Any] = {}
    if segments:
        json_path = role_dir / "transcript_packed.json"
        md_path = role_dir / "transcript_packed.md"
        write_json(json_path, segments)
        lines = ["# Raw segment transcript (audit view)", ""]
        for segment in segments:
            lines.append(
                f"[{segment['start_seconds']:06.2f}-{segment['end_seconds']:06.2f}] {segment['text']}"
            )
        write_text(md_path, "\n".join(lines) + "\n")
        result.update(
            {
                "transcript_pack_path": str(md_path),
                "transcript_pack_json_path": str(json_path),
                "transcript_segment_count": len(segments),
            }
        )
    words = load_transcript_words(info)
    if words:
        windowed = group_transcript_words(words)
        windowed_json_path = role_dir / "transcript_windowed.json"
        windowed_md_path = role_dir / "transcript_windowed.md"
        write_json(windowed_json_path, windowed)
        windowed_lines = ["# Window-safe transcript timeline", ""]
        for window in windowed:
            windowed_lines.append(
                f"[{window['start_seconds']:06.2f}-{window['end_seconds']:06.2f}] {window['text']}"
            )
        write_text(windowed_md_path, "\n".join(windowed_lines) + "\n")
        result.update(
            {
                "transcript_windowed_path": str(windowed_md_path),
                "transcript_windowed_json_path": str(windowed_json_path),
                "transcript_window_count": len(windowed),
            }
        )
    return result


def select_timeline_transcript(
    transcript: list[dict[str, Any]],
    transcript_words: list[dict[str, Any]],
    start: float,
    end: float,
) -> dict[str, Any]:
    """Select only speech that can be attributed to a requested time window.

    A segment that merely overlaps a window is not precise enough to display
    its entire text. Word timestamps are the authoritative fallback for that
    case; without them we show a warning instead of leaking full-video text.
    """
    if transcript_words:
        window_words = [
            word
            for word in transcript_words
            if float(word["end_seconds"]) > start and float(word["start_seconds"]) < end
        ]
        if not window_words:
            return {
                "scope": "word_window",
                "display_lines": ["窗口内未检测到带时间戳的口播。"],
                "word_count": 0,
                "segment_count": 0,
            }
        visible_start = max(start, float(window_words[0]["start_seconds"]))
        visible_end = min(end, float(window_words[-1]["end_seconds"]))
        text = " ".join(str(word["text"]) for word in window_words).strip()
        return {
            "scope": "word_window",
            "display_lines": [f"[{visible_start:.2f}-{visible_end:.2f}] {text}"],
            "word_count": len(window_words),
            "segment_count": 0,
        }

    overlapping = [
        segment
        for segment in transcript
        if float(segment["end_seconds"]) > start and float(segment["start_seconds"]) < end
    ]
    if not overlapping:
        return {
            "scope": "segment_window",
            "display_lines": ["窗口内未检测到带时间戳的口播。"],
            "word_count": 0,
            "segment_count": 0,
        }

    outside_window = any(
        float(segment["start_seconds"]) < start or float(segment["end_seconds"]) > end
        for segment in overlapping
    )
    if outside_window:
        ranges = ", ".join(
            f"{float(segment['start_seconds']):.2f}-{float(segment['end_seconds']):.2f}s"
            for segment in overlapping[:3]
        )
        return {
            "scope": "insufficient_precision",
            "display_lines": [
                f"转写时间粒度不足，未展示全文（原始分段：{ranges}）。",
                "请使用词级时间戳后再做窗口内口播归因。",
            ],
            "word_count": 0,
            "segment_count": len(overlapping),
        }

    return {
        "scope": "segment_window",
        "display_lines": [
            f"[{float(segment['start_seconds']):.2f}-{float(segment['end_seconds']):.2f}] {segment['text']}"
            for segment in overlapping
        ],
        "word_count": 0,
        "segment_count": len(overlapping),
    }


def build_timeline_views(role_dir: Path, info: dict[str, Any]) -> dict[str, Any]:
    if Image is None or ImageDraw is None:
        return {}
    duration = parse_timestamp_seconds(info.get("duration_seconds"))
    if duration is None or duration <= 0:
        return {}
    out_dir = role_dir / "timeline_views"
    out_dir.mkdir(parents=True, exist_ok=True)
    ranges = [("hook", 0.0, min(6.0, float(duration)))]
    if duration > 6:
        ranges.append(("cta", max(0.0, float(duration) - 6.0), float(duration)))

    transcript_path = current_transcript_segments_path(info)
    transcript = parse_srt_segments(transcript_path) if transcript_path else []
    transcript_words = load_transcript_words(info)
    written: list[dict[str, Any]] = []
    for label, start, end in ranges:
        view = build_timeline_view_for_range(
            role_dir,
            info,
            label,
            start,
            end,
            transcript=transcript,
            transcript_words=transcript_words,
        )
        if view:
            written.append(view)
    return {"timeline_views_dir": str(out_dir), "timeline_views": written}


def build_timeline_view_for_range(
    role_dir: Path,
    info: dict[str, Any],
    label: str,
    start: float,
    end: float,
    *,
    transcript: list[dict[str, Any]] | None = None,
    transcript_words: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Render a filmstrip/waveform/transcript view for any requested window.

    Word timestamps are preferred because an SRT segment can span the entire
    video. A coarse segment that only overlaps the requested window must never
    be rendered as if its full text belongs to that window.
    """
    if Image is None or ImageDraw is None:
        return None
    duration = parse_timestamp_seconds(info.get("duration_seconds"))
    if duration is None or duration <= 0:
        return None
    try:
        start_value = max(0.0, min(float(start), duration))
        end_value = max(start_value, min(float(end), duration))
    except (TypeError, ValueError):
        return None
    if end_value <= start_value:
        end_value = min(duration, start_value + 0.5)
    safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(label or "timeline")).strip("_.") or "timeline"
    out_dir = role_dir / "timeline_views"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{safe_label}.jpg"
    if transcript is None:
        transcript_path = current_transcript_segments_path(info)
        transcript = parse_srt_segments(transcript_path) if transcript_path else []
    if transcript_words is None:
        transcript_words = load_transcript_words(info)
    frames = frames_for_range(info, start_value, end_value, limit=8)
    transcript_window = select_timeline_transcript(
        transcript,
        transcript_words,
        start_value,
        end_value,
    )
    write_timeline_view(
        out_path,
        info,
        transcript,
        safe_label,
        start_value,
        end_value,
        frames=frames,
        transcript_window=transcript_window,
    )
    selection_source = "analysis_frame_manifest" if get_analysis_frame_entries(info) else "compatibility_manifest"
    return {
        "label": safe_label,
        "path": str(out_path),
        "start_seconds": round(start_value, 2),
        "end_seconds": round(end_value, 2),
        "selection_source": selection_source,
        "frame_count": len(frames),
        "frame_paths": [str(item.get("path") or "") for item in frames if str(item.get("path") or "")],
        "transcript_scope": transcript_window["scope"],
        "transcript_word_count": transcript_window["word_count"],
        "transcript_segment_count": transcript_window["segment_count"],
    }


def write_timeline_view(
    out_path: Path,
    info: dict[str, Any],
    transcript: list[dict[str, Any]],
    label: str,
    start: float,
    end: float,
    *,
    frames: list[dict[str, Any]] | None = None,
    transcript_window: dict[str, Any] | None = None,
) -> None:
    width = 1280
    height = 760
    canvas = Image.new("RGB", (width, height), "#f8fafc")
    draw = ImageDraw.Draw(canvas)
    title_font = load_font(30)
    label_font = load_font(20)
    small_font = load_font(16)
    draw.text((30, 24), f"{label.upper()} timeline {start:.1f}s-{end:.1f}s", fill="#0f172a", font=title_font)

    frames = frames if frames is not None else frames_for_range(info, start, end, limit=8)
    cell_width = 150
    cell_height = 260
    x0 = 30
    y0 = 82
    for index, entry in enumerate(frames):
        x = x0 + index * (cell_width + 8)
        path = Path(str(entry.get("path") or ""))
        if not path.exists():
            continue
        with Image.open(path) as image:
            tile = fit_image(image.convert("RGB"), cell_width, cell_height)
        canvas.paste(tile, (x, y0))
        draw.text((x, y0 + cell_height + 6), format_seconds(entry.get("timestamp_seconds")), fill="#475569", font=small_font)

    waveform_box = (30, 395, width - 30, 535)
    draw.rectangle(waveform_box, fill="#ffffff", outline="#cbd5e1")
    draw_waveform(draw, waveform_box, Path(str(info.get("audio_path") or "")), start, end)
    transcript_window = transcript_window or select_timeline_transcript(transcript, [], start, end)
    draw.text(
        (30, 552),
        f"Transcript in window [{start:.1f}s-{end:.1f}s]",
        fill="#334155",
        font=label_font,
    )

    y = 588
    for line in transcript_window["display_lines"]:
        for wrapped in wrap_text(line, 76):
            draw.text((30, y), wrapped, fill="#0f172a", font=small_font)
            y += 24
            if y > height - 28:
                draw.text((30, y), "...", fill="#64748b", font=small_font)
                canvas.save(out_path, quality=92)
                return
    canvas.save(out_path, quality=92)


def frames_for_range(info: dict[str, Any], start: float, end: float, limit: int) -> list[dict[str, Any]]:
    full = [
        entry for entry in get_analysis_frame_entries(info)
        if (timestamp := parse_timestamp_seconds(entry.get("timestamp_seconds"))) is not None
        and start <= timestamp <= end
    ]
    if full:
        return sample_evenly(full, limit)
    focus = [
        entry for entry in get_focus_frame_entries(info)
        if (timestamp := parse_timestamp_seconds(entry.get("timestamp_seconds"))) is not None
        and start <= timestamp <= end
    ]
    if focus:
        return sample_evenly(focus, limit)
    # A requested review window may fall between two sparse samples. Reuse the
    # canonical range selector so the generated timeline still contains the
    # nearest auditable frame instead of an empty filmstrip.
    duration = parse_timestamp_seconds(info.get("duration_seconds"))
    if duration is None:
        return []
    return select_frames_for_time_range(info, f"{start:.3f}s - {end:.3f}s", limit=limit)


def draw_waveform(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], audio_path: Path, start: float, end: float) -> None:
    left, top, right, bottom = box
    center = (top + bottom) // 2
    if not audio_path.exists():
        draw.line((left, center, right, center), fill="#94a3b8", width=2)
        draw.text((left + 12, top + 12), "audio unavailable", fill="#64748b", font=load_font(16))
        return
    try:
        with wave.open(str(audio_path), "rb") as wav:
            rate = wav.getframerate()
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            start_frame = max(0, int(start * rate))
            end_frame = min(wav.getnframes(), int(end * rate))
            wav.setpos(start_frame)
            raw = wav.readframes(max(0, end_frame - start_frame))
    except Exception:
        draw.line((left, center, right, center), fill="#94a3b8", width=2)
        return
    if not raw or sample_width != 2:
        draw.line((left, center, right, center), fill="#94a3b8", width=2)
        return
    values = []
    step = sample_width * max(1, channels)
    for index in range(0, len(raw) - step + 1, step):
        sample = int.from_bytes(raw[index:index + 2], byteorder="little", signed=True)
        values.append(abs(sample) / 32768.0)
    if not values:
        draw.line((left, center, right, center), fill="#94a3b8", width=2)
        return
    columns = max(1, right - left - 20)
    bucket = max(1, len(values) // columns)
    for x_index in range(columns):
        chunk = values[x_index * bucket:(x_index + 1) * bucket]
        amplitude = max(chunk) if chunk else 0.0
        half = int(amplitude * (bottom - top - 24) / 2)
        x = left + 10 + x_index
        draw.line((x, center - half, x, center + half), fill="#2563eb")
    draw.line((left + 10, center, right - 10, center), fill="#94a3b8", width=1)


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in (
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
    ):
        candidate = Path(path)
        if candidate.exists():
            try:
                return ImageFont.truetype(str(candidate), size)
            except OSError:
                continue
    return ImageFont.load_default()


def format_seconds(value: Any) -> str:
    parsed = parse_timestamp_seconds(value)
    if parsed is None:
        return "?.?s"
    return f"{parsed:.1f}s"


def wrap_text(text: str, width: int) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= width:
        return [text]
    lines = []
    while text:
        lines.append(text[:width])
        text = text[width:]
    return lines
