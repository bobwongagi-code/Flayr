"""Offline identity/range checks for an explicitly supplied review evidence index."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from urllib.parse import urlsplit


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path):
    return parse_json(Path(path).read_text(encoding="utf-8"))


def parse_json(text):
    def reject_constant(value):
        raise ValueError(f"Non-finite JSON number: {value}")

    return json.loads(
        text, object_pairs_hook=_unique_object, parse_constant=reject_constant
    )


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _keys(value, required, optional=()):
    if (
        not isinstance(value, dict)
        or not set(required) <= value.keys()
        or (value.keys() - set(required) - set(optional))
    ):
        raise ValueError(
            f"Expected keys {sorted(required)}; optional {sorted(optional)}"
        )


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _mapping(value, label):
    if not isinstance(value, dict) or not value or not all(_text(k) for k in value):
        raise ValueError(f"{label} must be a nonempty ID map")
    return value


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _bound_file(base, item):
    raw = item["path"]
    expected = item["sha256"]
    if not _text(raw) or urlsplit(raw).scheme or "\x00" in raw:
        raise ValueError("Source path must be a local file")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("Source SHA-256 must be 64 lowercase hex characters")
    path = (base / raw).resolve()
    if not path.is_file() or sha256(path) != expected:
        raise ValueError(f"Missing or changed source: {raw}")
    return path


def check_index(packet_path, data, index_path):
    """Return mechanical coverage, never semantic approval or video metadata proof."""
    packet_path, index_path = Path(packet_path).resolve(), Path(index_path).resolve()
    index_raw = index_path.read_bytes()
    index = parse_json(index_raw.decode("utf-8"))
    _keys(
        index,
        {"schema_version", "packet_sha256", "teaching", "videos", "evidence", "cases"},
    )
    if type(index["schema_version"]) is not int or index["schema_version"] != 1:
        raise ValueError("Unsupported evidence index version")
    packet_raw = packet_path.read_bytes()
    packet_hash = hashlib.sha256(packet_raw).hexdigest()
    if data != parse_json(packet_raw.decode("utf-8")):
        raise ValueError("Review packet changed during validation")
    if index["packet_sha256"] != packet_hash:
        raise ValueError("Review packet changed; rebuild its evidence index")
    base = index_path.parent
    teaching = index["teaching"]
    _keys(teaching, {"path", "sha256", "points"})
    teaching_path = _bound_file(base, teaching)
    if teaching_path != (packet_path.parent / data["teaching_reference"]).resolve():
        raise ValueError("Evidence index points to another teaching file")
    teaching_raw = teaching_path.read_bytes()
    if hashlib.sha256(teaching_raw).hexdigest() != teaching["sha256"]:
        raise ValueError("Teaching file changed during validation")
    teaching_text = teaching_raw.decode("utf-8")
    points = _mapping(teaching["points"], "teaching.points")
    for point_id, quote in points.items():
        if (
            not _text(quote)
            or quote not in teaching_text
            or not re.search(
                r"(?<![A-Za-z0-9_])" + re.escape(point_id) + r"(?![A-Za-z0-9_])", quote
            )
        ):
            raise ValueError(f"Teaching ID/quote not found: {point_id}")
    videos = _mapping(index["videos"], "videos")
    for video_id, video in videos.items():
        _keys(video, {"path", "sha256", "duration_seconds"})
        _bound_file(base, video)
        if not _number(video["duration_seconds"]) or video["duration_seconds"] <= 0:
            raise ValueError(f"Invalid declared duration: {video_id}")
    evidence = _mapping(index["evidence"], "evidence")
    for evidence_id, entry in evidence.items():
        _keys(entry, {"video", "time_range", "frames"})
        if not isinstance(entry["video"], str) or entry["video"] not in videos:
            raise ValueError(f"Unknown evidence video: {evidence_id}")
        times = entry["time_range"]
        duration = videos[entry["video"]]["duration_seconds"]
        if (
            not isinstance(times, list)
            or len(times) != 2
            or not all(map(_number, times))
            or not (0 <= times[0] <= times[1] <= duration)
        ):
            raise ValueError(
                f"Evidence range outside declared video duration: {evidence_id}"
            )
        if not isinstance(entry["frames"], list):
            # JSON contract errors share ValueError so callers fail consistently.
            raise ValueError(f"frames must be a list: {evidence_id}")  # noqa: TRY004
        for frame in entry["frames"]:
            _keys(frame, {"path", "sha256", "time_seconds"})
            _bound_file(base, frame)
            stamp = frame["time_seconds"]
            if not _number(stamp) or not times[0] <= stamp <= times[1]:
                raise ValueError(f"Frame outside evidence range: {evidence_id}")
    bindings = _mapping(index["cases"], "cases")
    if set(bindings) != {case["id"] for case in data["cases"]}:
        raise ValueError("Evidence index must cover exactly the packet's case IDs")
    unresolved, bound_facts = [], 0
    for case in data["cases"]:
        case_id = case["id"]
        binding = bindings[case_id]
        _keys(binding, {"video", "facts"})
        if not isinstance(binding["video"], str) or binding["video"] not in videos:
            raise ValueError(f"Unknown case video: {case_id}")
        if not set(case["teaching_points"]) <= points.keys():
            raise ValueError(f"Unknown teaching ID in case: {case_id}")
        facts = binding["facts"]
        if not isinstance(facts, list) or len(facts) != len(case["facts"]):
            raise ValueError(f"Bind each fact position exactly once: {case_id}")
        for position, refs in enumerate(facts):
            if isinstance(refs, dict):
                _keys(refs, {"unresolved"})
                if not _text(refs["unresolved"]):
                    raise ValueError("Unresolved fact requires a specific reason")
                unresolved.append(
                    {"case": case_id, "fact": position, "reason": refs["unresolved"]}
                )
                continue
            if (
                not isinstance(refs, list)
                or not refs
                or not all(_text(r) for r in refs)
                or len(set(refs)) != len(refs)
            ):
                raise ValueError(
                    f"Expected unique evidence IDs: {case_id} fact {position}"
                )
            for ref in refs:
                if ref not in evidence or evidence[ref]["video"] != binding["video"]:
                    raise ValueError(
                        f"Unknown or wrong-video evidence: {case_id} / {ref}"
                    )
            bound_facts += 1
    return {
        "packet_sha256": packet_hash,
        "index_sha256": hashlib.sha256(index_raw).hexdigest(),
        "bound_facts": bound_facts,
        "unresolved_facts": unresolved,
        "scope": "Declared IDs, exact quotes, file bytes and ranges only; no semantic approval",
    }
