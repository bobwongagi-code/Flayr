"""Frozen input loading and identity helpers for compact evaluations.

This module owns the non-model artifacts shared by a compact evaluation
cohort.  It reads locked facts and frame manifests, validates paths, and
computes source digests; model calls and raw-video handling remain in
``compact_eval``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..artifacts import get_stage_frame_entries, parse_time_range_seconds
from ..stage_catalog import DEFAULT_STAGES

MAX_STAGE_FRAME_INPUTS_PER_STAGE = 4
_STAGE_CODE_RE = re.compile(r"^(S[1-6])(?:\s|$)")


class CompactEvaluationError(ValueError):
    """Raised when a frozen bundle or compact model result is invalid."""


@dataclass(frozen=True)
class FrozenCompactBundle:
    """The exact non-model inputs shared by one model comparison cohort."""

    run_dir: Path
    context: dict[str, Any]
    allowed_evidence_ids: dict[str, dict[str, set[str]]]
    visual_inputs: tuple[dict[str, str], ...]
    source_digest: str
    stage_time_ranges: dict[str, dict[str, str]] = field(default_factory=dict)
    input_mode: str = "locked_facts_and_frames"
    video_inputs: tuple[dict[str, Any], ...] = ()
    source_run: str | None = None
    audit_provenance: dict[str, Any] = field(default_factory=dict)


def _read_json(path: Path, *, required: bool = True) -> dict[str, Any]:
    if not path.is_file():
        if required:
            raise CompactEvaluationError(f"required frozen artifact is missing: {path}")
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CompactEvaluationError(f"invalid frozen JSON artifact: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CompactEvaluationError(f"frozen artifact root must be an object: {path}")
    return value


def _compact_fact_unit(unit: dict[str, Any]) -> dict[str, Any]:
    """Keep only facts needed for a bounded judgment prompt.

    Cache metadata, generated audit fields, and large internal diagnostics are
    intentionally excluded so the comparison does not depend on one model's
    full-analysis formatting.
    """
    fields = (
        "id",
        "time_range",
        "information",
        "voiceover",
        "voiceover_zh",
        "visual_fact",
        "subtitle_fact",
        "evidence_strength",
        "functions",
        "product_visible",
        "trust_source_signals",
        "trust_source_reference",
        "fact_quality",
    )
    return {field: unit.get(field) for field in fields if field in unit}


def _facts_for_role(run_dir: Path, role: str) -> dict[str, Any]:
    source = _read_json(run_dir / f"video_facts_{role}.json")
    units = source.get("evidence_units")
    if not isinstance(units, list) or not units:
        raise CompactEvaluationError(f"{role} has no frozen evidence_units")
    compact_units = [item for item in units if isinstance(item, dict)]
    if not compact_units:
        raise CompactEvaluationError(f"{role} has no valid frozen evidence_units")
    return {
        "content_summary": str(source.get("content_summary") or ""),
        "communication_strategy": str(source.get("communication_strategy") or ""),
        "evidence_units": [_compact_fact_unit(item) for item in compact_units],
    }


def _stage_code(stage: str) -> str:
    match = _STAGE_CODE_RE.match(str(stage or "").strip())
    if not match:
        raise CompactEvaluationError(f"invalid stage label: {stage!r}")
    return match.group(1)


def _allowed_evidence_ids(facts: dict[str, Any]) -> dict[str, set[str]]:
    result = {stage.code: set() for stage in DEFAULT_STAGES}
    for unit in facts.get("evidence_units", []):
        if not isinstance(unit, dict):
            continue
        evidence_id = str(unit.get("id") or "").strip()
        functions = unit.get("functions")
        if not evidence_id or not isinstance(functions, list):
            continue
        for function in functions:
            token = str(function or "").strip().upper()
            match = re.match(r"^(S[1-6])(?:_|$)", token)
            if match and match.group(1) in result:
                result[match.group(1)].add(evidence_id)
    return result


def _stage_frame_inputs(
    run_dir: Path,
    role: str,
    *,
    image_to_data_url_fn: Callable[..., str],
) -> list[dict[str, str]]:
    role_dir = run_dir / role
    preprocess_path = role_dir / "_preprocess.json"
    if preprocess_path.is_file():
        try:
            info = json.loads(preprocess_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CompactEvaluationError(f"invalid preprocess manifest: {preprocess_path}: {exc}") from exc
    else:
        info = {"stage_frame_manifest_path": str(role_dir / "frames" / "stage_frames.json")}
    value = get_stage_frame_entries(info)
    manifest_path = Path(
        str(info.get("analysis_stage_frame_manifest_path") or role_dir / "frames" / "analysis_stage_frames.json")
    )
    if not isinstance(value, list):
        raise CompactEvaluationError(f"stage frame manifest must be a list: {manifest_path}")

    selected: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for stage in DEFAULT_STAGES:
        entries = [
            item
            for item in value
            if isinstance(item, dict) and str(item.get("stage") or "") == stage.name
        ]
        if len(entries) > MAX_STAGE_FRAME_INPUTS_PER_STAGE:
            raise CompactEvaluationError(
                f"{stage.code} has {len(entries)} frozen frame inputs; "
                f"the explicit contract maximum is {MAX_STAGE_FRAME_INPUTS_PER_STAGE}"
            )
        for item in entries:
            raw_path = str(item.get("path") or "").strip()
            path = _resolve_frozen_visual_path(raw_path, run_dir)
            if not path.is_file():
                fallback = run_dir / role / "contact_sheets" / f"stage_{int(stage.code[1:]):02d}.jpg"
                if fallback.is_file():
                    path = fallback
                else:
                    raise CompactEvaluationError(f"frozen visual input is missing: {path}")
            resolved = str(path.resolve())
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            selected.append(
                {
                    "role": role,
                    "stage": stage.code,
                    "label": f"{role} {stage.name} @ {item.get('timestamp_seconds', '')}s",
                    "path": resolved,
                    "sha256": _file_digest(path),
                    "data_url": image_to_data_url_fn(path, max_bytes=4 * 1024 * 1024),
                }
            )
    return selected


def _resolve_frozen_visual_path(raw_path: str, run_dir: Path) -> Path:
    """Keep frozen visual inputs inside the evaluation run directory."""
    candidate = Path(str(raw_path or "")).expanduser()
    if not candidate.is_absolute():
        candidate = run_dir / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(run_dir.expanduser().resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise CompactEvaluationError(f"frozen visual input escapes run directory: {raw_path}") from exc
    return resolved


def _stable_digest(value: Any) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise CompactEvaluationError(f"cannot hash frozen visual input: {path}: {exc}") from exc
    return digest.hexdigest()


def _stage_time_ranges(run_dir: Path) -> dict[str, dict[str, str]]:
    """Read already-produced stage windows for diagnostics only.

    Stage windows are intentionally excluded from the model-input digest. They
    are used after a response is produced to explain temporal mismatches, not
    to change the locked prompt shared by the model cohort.
    """
    analysis = _read_json(run_dir / "analysis_result.json", required=False)
    rows = analysis.get("stage_analysis")
    if not isinstance(rows, list):
        return {"creator": {}, "benchmark": {}}
    result = {"creator": {}, "benchmark": {}}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            stage_code = _stage_code(str(row.get("stage") or row.get("stage_name") or ""))
        except CompactEvaluationError:
            continue
        for role in ("creator", "benchmark"):
            value = row.get(f"{role}_time_range")
            if isinstance(value, str) and parse_time_range_seconds(value, None) is not None:
                result[role][stage_code] = value
    return result
