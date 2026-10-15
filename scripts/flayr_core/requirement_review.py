"""Offline requirement-to-evidence review; no retrieval, scoring, or model calls."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .artifacts import get_analysis_frame_entries, parse_time_range_seconds, parse_timestamp_seconds
from .human_review import HumanReviewError, load_analysis
from .utils import write_json


class RequirementReviewError(HumanReviewError):
    """A requirement, candidate, or source binding cannot be safely reviewed."""


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RequirementReviewError(f"Expected an object: {path}")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RequirementReviewError("Expected non-empty text")
    return value.strip()


def _keys(value: Any, required: set[str], optional: set[str] | None = None) -> None:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - (optional or set()):
        raise RequirementReviewError("Unexpected or missing fields; scores and automatic completion are not accepted")


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bind(path: Path, bindings: dict[str, str]) -> Path:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise RequirementReviewError(f"Missing source file: {path}")
    bindings[str(path)] = _hash(path)
    return path


def _local(base: Path, raw: Any) -> Path:
    path = Path(_text(raw)).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def _requirements(path: Path, bindings: dict[str, str]) -> dict[str, Any]:
    bundle = _read(_bind(path, bindings))
    _keys(bundle, {"id", "version", "approved_by", "requirements"})
    for key in ("id", "version", "approved_by"):
        _text(bundle[key])
    items = bundle["requirements"]
    if not isinstance(items, list) or not items:
        raise RequirementReviewError("An explicitly approved requirement list is required")
    ids = set()
    for item in items:
        _keys(item, {"id", "text", "source_type", "source_path", "source_locator"})
        identifier = _text(item["id"])
        if identifier in ids:
            raise RequirementReviewError(f"Duplicate requirement: {identifier}")
        ids.add(identifier)
        _text(item["text"])
        if item["source_type"] not in {"user_provided", "breakdown_explicit"}:
            raise RequirementReviewError("Inferred teaching requirements are not accepted")
        source = _bind(_local(path.parent, item["source_path"]), bindings)
        locator = item["source_locator"]
        _keys(locator, {"kind", "value"})
        if locator["kind"] == "quote":
            quote = _text(locator["value"])
            if quote not in source.read_text(encoding="utf-8"):
                raise RequirementReviewError("Requirement quote is absent from its source")
        elif locator["kind"] == "page":
            if type(locator["value"]) is not int or locator["value"] < 1:
                raise RequirementReviewError("Page must be a positive integer")
        elif locator["kind"] == "time_range":
            if parse_time_range_seconds(locator["value"], None) is None:
                raise RequirementReviewError("Invalid reference time range")
        else:
            raise RequirementReviewError("Unsupported source locator")
        item["source_path"] = str(source)
    return bundle


def build_packet(run_dir: Path, requirements: Path, links: Path) -> dict[str, Any]:
    """Bind manually supplied candidates to existing creator evidence, never infer matches."""
    root = run_dir.expanduser().resolve()
    requirements, links = requirements.expanduser().resolve(), links.expanduser().resolve()
    analysis = load_analysis(root)  # Reuse the existing success/degraded source gate.
    bindings: dict[str, str] = {}
    _bind(root / "analysis.json", bindings)
    bundle = _requirements(requirements, bindings)
    candidate_map = _read(_bind(links, bindings))
    expected_ids = {item["id"] for item in bundle["requirements"]}
    if candidate_map.keys() - expected_ids:
        raise RequirementReviewError("Candidate map references an unknown requirement")
    info = analysis.get("videos", {}).get("creator")
    if not isinstance(info, dict):
        raise RequirementReviewError("Missing creator video")
    video = _bind(_local(root, info.get("path")), bindings)
    duration = info.get("duration_seconds")
    duration_range = parse_time_range_seconds([0, duration], duration)
    if isinstance(duration, bool) or duration_range is None or duration_range[1] <= 0:
        raise RequirementReviewError("Invalid creator duration")
    facts = analysis.get("video_understanding", {}).get("creator", {})
    units = facts.get("evidence_units")
    if not isinstance(units, list):
        raise RequirementReviewError("Missing creator evidence ledger; not equivalent to an empty search")
    by_id = {}
    for unit in units:
        if not isinstance(unit, dict) or not isinstance(unit.get("id"), str) or not unit["id"].strip():
            raise RequirementReviewError("Malformed evidence ID")
        if unit["id"] in by_id:
            raise RequirementReviewError("Duplicate evidence ID")
        by_id[unit["id"]] = unit
    frames = get_analysis_frame_entries(info)
    rows = []
    for requirement in bundle["requirements"]:
        ids = candidate_map.get(requirement["id"], [])
        if not isinstance(ids, list) or any(not isinstance(value, str) for value in ids) or len(set(ids)) != len(ids):
            raise RequirementReviewError("Candidates must be distinct evidence IDs")
        candidates = []
        for identifier in ids:
            if identifier not in by_id:
                raise RequirementReviewError(f"Unknown creator evidence: {identifier}")
            unit = by_id[identifier]
            bounds = parse_time_range_seconds(unit.get("time_range"), duration)
            if bounds is None:
                raise RequirementReviewError(f"Invalid evidence time range: {identifier}")
            start, end = bounds
            observations = {}
            for key in ("visual_fact", "subtitle_fact", "voiceover", "voiceover_zh", "information"):
                value = unit.get(key) or ""
                if not isinstance(value, str):
                    raise RequirementReviewError(f"Invalid evidence text: {identifier}/{key}")
                observations[key] = value
            screenshots = []
            seen = set()
            for frame in frames:
                timestamp = parse_timestamp_seconds(frame.get("timestamp_seconds"))
                if timestamp is None or not start <= timestamp <= end:
                    continue
                path = _local(root, frame.get("path"))
                if not path.is_relative_to(root / "creator"):
                    raise RequirementReviewError("Candidate screenshot is outside the creator source directory")
                if str(path) in seen:
                    continue
                seen.add(str(path))
                _bind(path, bindings)
                screenshots.append({"path": str(path), "seconds": timestamp})
            candidates.append({
                "evidence_id": identifier, "time_range": [start, end],
                "observations": observations,
                "screenshots": sorted(screenshots, key=lambda entry: entry["seconds"]),
            })
        rows.append({"requirement": requirement, "candidates": candidates})
    return {
        "schema_version": 1, "mode": "candidate_review_only",
        "inputs": {"run_dir": str(root), "requirements": str(requirements), "links": str(links)},
        "source_hashes": bindings, "bundle": {key: bundle[key] for key in ("id", "version", "approved_by")},
        "video": str(video), "rows": rows, "human_notes": [],
    }


def init_packet(run_dir: Path, requirements: Path, links: Path, output: Path) -> dict[str, Any]:
    output = output.expanduser().resolve()
    if output.exists():
        raise RequirementReviewError("Review packet already exists; do not overwrite prior review")
    packet = build_packet(run_dir, requirements, links)
    write_json(output, packet)
    return packet


def load_packet(path: Path) -> dict[str, Any]:
    packet = _read(path)
    _keys(packet, {"schema_version", "mode", "inputs", "source_hashes", "bundle", "video", "rows", "human_notes"})
    _keys(packet["inputs"], {"run_dir", "requirements", "links"})
    current = build_packet(**{key: Path(value) for key, value in packet["inputs"].items()})
    if {key: value for key, value in packet.items() if key != "human_notes"} != {
        key: value for key, value in current.items() if key != "human_notes"
    }:
        raise RequirementReviewError("Sources or candidate packet changed; create a new review packet")
    if not isinstance(packet["human_notes"], list):
        raise RequirementReviewError("Invalid human review history")
    ids = {row["requirement"]["id"] for row in packet["rows"]}
    for note in packet["human_notes"]:
        _keys(note, {"requirement_id", "reviewer", "note", "recorded_at"})
        if note["requirement_id"] not in ids:
            raise RequirementReviewError("Unknown requirement in review history")
        for value in note.values():
            _text(value)
    return packet


def record_note(path: Path, requirement_id: str, reviewer: str, note: str) -> dict[str, Any]:
    packet = load_packet(path)
    if requirement_id not in {row["requirement"]["id"] for row in packet["rows"]}:
        raise RequirementReviewError("Unknown requirement")
    packet["human_notes"].append({
        "requirement_id": requirement_id, "reviewer": _text(reviewer), "note": _text(note),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    })
    write_json(path, packet)
    return packet


def render_packet(packet: dict[str, Any]) -> str:
    """Internal plain-text view; descriptions remain unverified ledger excerpts."""
    lines = ["要求与候选片段核对（内部审核）", "候选不是已满足；未提供候选不代表没做。账本描述尚需核实。",
             f"返稿视频：{packet['video']}", f"要求版本：{packet['bundle']['id']} / {packet['bundle']['version']}"]
    for row in packet["rows"]:
        requirement = row["requirement"]
        lines.extend([f"\n[{requirement['id']}] {requirement['text']}",
                      f"要求出处：{requirement['source_path']} / {requirement['source_locator']}"])
        if not row["candidates"]:
            lines.append("尚未提供候选，需人工定位；不作执行与否的结论。")
        for candidate in row["candidates"]:
            lines.append(f"候选 {candidate['evidence_id']}：{candidate['time_range']} 秒")
            for channel, text in candidate["observations"].items():
                if text:
                    lines.append(f"  原账本 {channel}（未核实）：{text}")
            if not candidate["screenshots"]:
                lines.append("  本时间段没有已绑定截图，请回看原视频；不据此否定画面。")
            for frame in candidate["screenshots"]:
                lines.append(f"  截图 {frame['seconds']} 秒：{frame['path']}")
        for note in packet["human_notes"]:
            if note["requirement_id"] == requirement["id"]:
                lines.append(f"人工记录 {note['reviewer']} / {note['recorded_at']}：{note['note']}")
    return "\n".join(lines)
