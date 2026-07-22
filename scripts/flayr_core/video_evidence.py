"""Secondary video evidence artifacts for Flayr.

These artifacts make the existing frames, audio, and transcript easier to audit.
They do not change scoring directly.
"""

from __future__ import annotations

import html
import json
import math
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
    get_focus_frame_entries,
    get_frame_entries,
    get_stage_frame_entries,
    parse_timestamp_seconds,
    sample_evenly,
)
from .utils import write_json, write_text

SIGNATURE_SIZE = 16
DEDUP_THRESHOLD_PERCENT = 8.0
DEDUP_WINDOW = 4


def build_video_evidence_artifacts(role_dir: Path, info: dict[str, Any]) -> dict[str, Any]:
    """Write dedup reports, contact sheets, transcript packs, and timeline views."""
    result: dict[str, Any] = {
        "status": "completed",
        "errors": [],
        "frame_selection_report_path": None,
        "frame_selection_report_html_path": None,
        "contact_sheets_dir": None,
        "timeline_views_dir": None,
        "timeline_views": [],
        "transcript_pack_path": None,
        "transcript_pack_json_path": None,
        "audit_path": None,
    }

    try:
        selection = build_frame_selection_report(role_dir, info)
        result.update(selection)
    except Exception as exc:  # pragma: no cover - artifact generation should not break analysis
        result["errors"].append(f"frame selection report failed: {exc}")

    try:
        contact_sheets = build_contact_sheets(role_dir, info)
        result.update(contact_sheets)
    except Exception as exc:  # pragma: no cover
        result["errors"].append(f"contact sheets failed: {exc}")

    try:
        transcript_pack = build_transcript_pack(role_dir, info)
        result.update(transcript_pack)
    except Exception as exc:  # pragma: no cover
        result["errors"].append(f"transcript pack failed: {exc}")

    try:
        timeline_views = build_timeline_views(role_dir, info)
        result.update(timeline_views)
    except Exception as exc:  # pragma: no cover
        result["errors"].append(f"timeline views failed: {exc}")

    audit = audit_video_evidence(role_dir, result)
    result["audit_path"] = audit.get("path")
    if audit.get("warnings"):
        result["errors"].extend(audit["warnings"])

    if result["errors"]:
        result["status"] = "partial"
    return result


def audit_video_evidence(role_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    """Self-check generated evidence artifacts and write a small audit file."""
    checks = [
        ("selection_report", result.get("frame_selection_report_path")),
        ("selection_report_html", result.get("frame_selection_report_html_path")),
    ]
    if (role_dir / "transcript.srt").is_file():
        checks.extend(
            [
                ("transcript_pack", result.get("transcript_pack_path")),
                ("transcript_pack_json", result.get("transcript_pack_json_path")),
            ]
        )
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

    audit = {
        "status": "pass" if not warnings else "warn",
        "warnings": warnings,
        "items": audit_items,
    }
    path = role_dir / "video_evidence_audit.json"
    write_json(path, audit)
    audit["path"] = str(path)
    return audit


def build_frame_selection_report(role_dir: Path, info: dict[str, Any]) -> dict[str, Any]:
    if Image is None:
        return {}
    frames = get_frame_entries(info)
    if not frames:
        return {}

    kept_signatures: list[list[tuple[int, int, int]]] = []
    decisions: list[dict[str, Any]] = []
    kept_count = 0
    for entry in frames:
        path = Path(str(entry.get("path") or ""))
        signature = image_signature(path)
        if not signature:
            decisions.append({**entry, "kept": False, "diff_percent": None, "reason": "unreadable"})
            continue

        if not kept_signatures:
            kept = True
            diff_percent = 100.0
            reason = "first_frame"
        else:
            recent = kept_signatures[-DEDUP_WINDOW:]
            diffs = [pixel_diff_percent(signature, previous) for previous in recent]
            diff_percent = min(diffs) if diffs else 100.0
            kept = diff_percent >= DEDUP_THRESHOLD_PERCENT
            reason = "visual_change" if kept else "near_duplicate"

        if kept:
            kept_signatures.append(signature)
            kept_count += 1
        decisions.append(
            {
                **entry,
                "kept": kept,
                "diff_percent": round(diff_percent, 2) if diff_percent is not None else None,
                "reason": reason,
            }
        )

    report = {
        "strategy": {
            "signature": f"{SIGNATURE_SIZE}x{SIGNATURE_SIZE} RGB pixel difference",
            "threshold_percent": DEDUP_THRESHOLD_PERCENT,
            "sliding_window": DEDUP_WINDOW,
            "note": "Selection report is audit-only; frames are not deleted.",
        },
        "frame_count": len(decisions),
        "kept_count": kept_count,
        "dropped_count": len(decisions) - kept_count,
        "decisions": decisions,
    }
    frames_dir = Path(str(info.get("frames_dir") or role_dir / "frames"))
    json_path = frames_dir / "selection_report.json"
    html_path = frames_dir / "selection_report.html"
    write_json(json_path, report)
    write_selection_report_html(html_path, report)
    return {
        "frame_selection_report_path": str(json_path),
        "frame_selection_report_html_path": str(html_path),
        "dedup_kept_frame_count": kept_count,
    }


def image_signature(path: Path) -> list[tuple[int, int, int]]:
    if not path.exists():
        return []
    with Image.open(path) as image:
        small = image.convert("RGB").resize((SIGNATURE_SIZE, SIGNATURE_SIZE))
        return list(small.getdata())


def pixel_diff_percent(
    current: list[tuple[int, int, int]],
    previous: list[tuple[int, int, int]],
    tolerance: int = 25,
) -> float:
    total = min(len(current), len(previous))
    if total <= 0:
        return 100.0
    changed = 0
    for left, right in zip(current[:total], previous[:total]):
        if any(abs(left[index] - right[index]) > tolerance for index in range(3)):
            changed += 1
    return changed / total * 100


def write_selection_report_html(path: Path, report: dict[str, Any]) -> None:
    rows = []
    for item in report.get("decisions", []):
        rel = html.escape(str(item.get("filename") or ""))
        status = "keep" if item.get("kept") else "drop"
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('timestamp_seconds', '')))}s</td>"
            f"<td><img src=\"{rel}\" alt=\"{rel}\"></td>"
            f"<td class=\"{status}\">{status}</td>"
            f"<td>{html.escape(str(item.get('diff_percent')))}</td>"
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
<p>Kept {report.get('kept_count')} / {report.get('frame_count')} frames. Audit-only; original frames remain available.</p>
<table><thead><tr><th>Time</th><th>Frame</th><th>Decision</th><th>Diff %</th><th>Reason</th></tr></thead><tbody>
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
    srt_path = Path(str(info.get("transcript_segments_path") or role_dir / "transcript.srt"))
    segments = parse_srt_segments(srt_path)
    if not segments:
        return {}

    json_path = role_dir / "transcript_packed.json"
    md_path = role_dir / "transcript_packed.md"
    write_json(json_path, segments)
    lines = ["# Packed transcript", ""]
    for segment in segments:
        lines.append(
            f"[{segment['start_seconds']:06.2f}-{segment['end_seconds']:06.2f}] {segment['text']}"
        )
    write_text(md_path, "\n".join(lines) + "\n")
    return {
        "transcript_pack_path": str(md_path),
        "transcript_pack_json_path": str(json_path),
        "transcript_segment_count": len(segments),
    }


def parse_srt_segments(path: Path) -> list[dict[str, Any]]:
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
        spoken = " ".join(lines[time_line_index + 1:]).strip()
        if not spoken:
            continue
        segments.append({"start_seconds": round(start, 2), "end_seconds": round(end, 2), "text": spoken})
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

    transcript = parse_srt_segments(Path(str(info.get("transcript_segments_path") or role_dir / "transcript.srt")))
    written: list[dict[str, Any]] = []
    for label, start, end in ranges:
        out_path = out_dir / f"{label}.jpg"
        write_timeline_view(out_path, info, transcript, label, start, end)
        written.append({"label": label, "path": str(out_path), "start_seconds": round(start, 2), "end_seconds": round(end, 2)})
    return {"timeline_views_dir": str(out_dir), "timeline_views": written}


def write_timeline_view(
    out_path: Path,
    info: dict[str, Any],
    transcript: list[dict[str, Any]],
    label: str,
    start: float,
    end: float,
) -> None:
    width = 1280
    height = 760
    canvas = Image.new("RGB", (width, height), "#f8fafc")
    draw = ImageDraw.Draw(canvas)
    title_font = load_font(30)
    label_font = load_font(20)
    small_font = load_font(16)
    draw.text((30, 24), f"{label.upper()} timeline {start:.1f}s-{end:.1f}s", fill="#0f172a", font=title_font)

    frames = frames_for_range(info, start, end, limit=8)
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
    draw.text((30, 552), "Transcript in window", fill="#334155", font=label_font)

    y = 588
    for segment in transcript:
        if float(segment["end_seconds"]) < start or float(segment["start_seconds"]) > end:
            continue
        line = f"[{segment['start_seconds']:.2f}-{segment['end_seconds']:.2f}] {segment['text']}"
        for wrapped in wrap_text(line, 76):
            draw.text((30, y), wrapped, fill="#0f172a", font=small_font)
            y += 24
            if y > height - 28:
                draw.text((30, y), "...", fill="#64748b", font=small_font)
                canvas.save(out_path, quality=92)
                return
    canvas.save(out_path, quality=92)


def frames_for_range(info: dict[str, Any], start: float, end: float, limit: int) -> list[dict[str, Any]]:
    focus = [
        entry for entry in get_focus_frame_entries(info)
        if (timestamp := parse_timestamp_seconds(entry.get("timestamp_seconds"))) is not None
        and start <= timestamp <= end
    ]
    if focus:
        return sample_evenly(focus, limit)
    full = [
        entry for entry in get_frame_entries(info)
        if (timestamp := parse_timestamp_seconds(entry.get("timestamp_seconds"))) is not None
        and start <= timestamp <= end
    ]
    return sample_evenly(full, limit)


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
