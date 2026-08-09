"""Compact, isolated model-contract evaluation for Flayr.

This module deliberately does not call the production comparison payload,
repair path, finalizer, or report writers.  It measures whether a model can
make six stage judgments under a small, stable response contract.  A valid
result is an experiment artifact, never an ``analysis_result.json``.
"""

from __future__ import annotations

import base64
import binascii
from copy import deepcopy
import hashlib
import json
import math
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..artifacts import get_stage_frame_entries, parse_time_range_seconds
from ..evidence_states import EVIDENCE_STATE_STRENGTHS, S4_EFFECT_EVIDENCE_STATES, S5_TRUST_STATES
from ..postprocess.utils import evidence_overlaps_range
from ..resources import ResourceBudget, ResourceBudgetExceeded, ResourceLimits, encode_file_data_url
from ..report_metadata import current_code_commit
from ..stage_catalog import DEFAULT_STAGES
from ..utils import write_bytes, write_json
from ..video import probe_duration_seconds
from .api import call_llm_api, extract_chat_completion_text, image_to_data_url, read_llm_api_key, video_to_data_url
from .parse import parse_json_text


COMPACT_EVAL_SCHEMA_VERSION = 1
VISUAL_EXTRACTION_SCHEMA_VERSION = 2
MODEL_INDEPENDENT_SCHEMA_VERSION = 2
S4_FACT_STATE_SCHEMA_VERSION = 1
S4_JUDGMENT_SCHEMA_VERSION = 1
S4_SINGLE_PASS_SCHEMA_VERSION = 1
S4_FREE_TEXT_STEPS_SCHEMA_VERSION = 1
S5_AUDIT_SCHEMA_VERSION = 1
COMPACT_EVAL_ROLE = "compact_judgment_on_locked_facts"
SEVERITY_ONLY_ROLE = "severity_judgment_on_locked_facts"
VISUAL_EXTRACTION_ROLE = "visual_fact_extraction_on_locked_frames"
MODEL_INDEPENDENT_ROLE = "model_independent_comparison_on_model_facts"
S4_FACT_STATE_ROLE = "s4_fact_state_on_locked_facts"
S4_JUDGMENT_ROLE = "s4_judgment_on_locked_fact_state"
S4_SINGLE_PASS_ROLE = "s4_single_pass_judgment_on_locked_facts"
S4_FREE_TEXT_STEPS_ROLE = "s4_free_text_steps_judgment_on_locked_facts"
S5_AUDIT_ROLE = "s5_source_audit_on_locked_facts"
EVALUATION_ROLES = frozenset({"model_calibration", "mechanism_regression", "blind_validation"})
DECISION_SCOPE_BY_ROLE = {
    "model_calibration": "calibration_only",
    "mechanism_regression": "mechanism_regression_only",
    "blind_validation": "blind_validation_only",
}
COMPACT_OUTPUT_BUDGET = 8192
COMPACT_MAX_REASON_CHARS = 240
COMPACT_MAX_BASIS_CHARS = 320
COMPACT_MAX_EVIDENCE_IDS = 4
S4_MAX_EVIDENCE_IDS = 8
S4_FREE_TEXT_MAX_CHARS = 240
S5_MAX_EVIDENCE_IDS = 8
MODEL_INDEPENDENT_WINNERS = frozenset({"benchmark", "creator", "tie", "uncertain"})
MODEL_INDEPENDENT_RELATIONS = frozenset({"benchmark_better", "creator_better", "tie", "uncertain"})
MODEL_INDEPENDENT_GAPS = frozenset({"none", "small", "medium", "large", "uncertain"})
EXTRACTION_MAX_UNITS = 12
EXTRACTION_MAX_INFORMATION_CHARS = 240
RAW_VIDEO_ENCODING = {
    "fps": 2.0,
    "max_width": 480,
    "max_duration_seconds": 180.0,
    "max_data_bytes": 8 * 1024 * 1024,
    "timeout_seconds": 300,
}
RAW_VIDEO_ROLES = ("benchmark", "creator")
COMPACT_SEVERITIES = frozenset({"small", "medium", "large"})
COMPACT_STATES = frozenset({"none", "partial", "complete", "uncertain"})
COMPACT_CONFIDENCES = frozenset({"high", "medium", "low"})
FACT_QUALITY_FIELDS = {
    "subject": frozenset({"correct", "incorrect", "uncertain", "not_applicable"}),
    "visibility": frozenset({"clear", "partial", "obscured", "uncertain", "not_applicable"}),
    "composition": frozenset({"central", "supporting", "weak", "uncertain", "not_applicable"}),
    "completion": frozenset({"complete", "partial", "none", "uncertain", "not_applicable"}),
    "proof": frozenset(
        {"direct_comparison", "result_only", "claim_only", "none", "uncertain", "not_applicable"}
    ),
    "causal_link": frozenset({"supported", "weak", "unsupported", "uncertain", "not_applicable"}),
}
S4_STATE_VISIBILITY = FACT_QUALITY_FIELDS["visibility"]
S4_STATE_PROOF = FACT_QUALITY_FIELDS["proof"]
S4_STATE_CAUSAL_LINK = FACT_QUALITY_FIELDS["causal_link"]
S5_AUDIT_STATES = frozenset(S5_TRUST_STATES)


def _stage_evidence_id_limit(value: int | None) -> int:
    """Resolve an isolated-contract limit without changing the production default."""
    limit = COMPACT_MAX_EVIDENCE_IDS if value is None else value
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 16:
        raise CompactEvaluationError("max_stage_evidence_ids must be an integer between 1 and 16")
    return limit


def contract_limits_for_variant(variant: str) -> dict[str, int]:
    """Return every numeric response limit enforced by the isolated contract."""
    limits = {"output_budget": COMPACT_OUTPUT_BUDGET}
    if variant in {
        "evidence_grounded",
        "model_independent",
        "severity_only",
        "severity_only_scaffold",
    }:
        limits["stage_count"] = len(DEFAULT_STAGES)
    if variant in {"evidence_grounded", "model_independent"}:
        limits.update(
            {
                "max_reason_chars": COMPACT_MAX_REASON_CHARS,
                "max_stage_evidence_ids": COMPACT_MAX_EVIDENCE_IDS,
            }
        )
    if variant == "model_independent":
        limits["max_overall_reason_chars"] = COMPACT_MAX_BASIS_CHARS
    if variant in {"severity_scaffold", "severity_only_scaffold"}:
        limits["max_decision_basis_chars"] = COMPACT_MAX_BASIS_CHARS
    if variant in {"visual_extraction", "visual_extraction_on_raw_video"}:
        limits.update(
            {
                "max_evidence_units_per_role": EXTRACTION_MAX_UNITS,
                "max_information_chars": EXTRACTION_MAX_INFORMATION_CHARS,
            }
        )
    if variant in {"s4_fact_state", "s4_single_pass"}:
        limits.update(
            {
                "stage_count": 1,
                "max_reason_chars": COMPACT_MAX_REASON_CHARS,
                "max_stage_evidence_ids": S4_MAX_EVIDENCE_IDS,
            }
        )
    if variant == "s4_single_pass":
        limits["max_decision_basis_chars"] = COMPACT_MAX_BASIS_CHARS
    if variant == "s4_free_text_steps":
        limits.update(
            {
                "stage_count": 1,
                "max_stage_facts_chars": S4_FREE_TEXT_MAX_CHARS,
                "max_comparison_chars": S4_FREE_TEXT_MAX_CHARS,
                "max_purchase_impact_chars": S4_FREE_TEXT_MAX_CHARS,
            }
        )
    if variant == "s5_audit":
        limits.update(
            {
                "stage_count": 1,
                "max_reason_chars": COMPACT_MAX_REASON_CHARS,
                "max_stage_evidence_ids": S5_MAX_EVIDENCE_IDS,
                "max_decision_basis_chars": COMPACT_MAX_BASIS_CHARS,
            }
        )
    if variant == "s4_judgment":
        limits["stage_count"] = 1
        limits["max_decision_basis_chars"] = COMPACT_MAX_BASIS_CHARS
    return limits


def _contract_error_codes(errors: list[str]) -> list[str]:
    """Map validator messages to stable aggregate categories for cohort reports."""
    codes: set[str] = set()
    for error in errors:
        text = str(error)
        if "schema_version" in text:
            codes.add("schema_version")
        if "unsupported" in text or "missing" in text:
            codes.add("shape")
        if "evidence_ids" in text:
            if "exceeds max_stage_evidence_ids" in text:
                codes.add("evidence_ids_too_many")
            elif "duplicate" in text:
                codes.add("evidence_ids_duplicate")
            elif "outside" in text:
                codes.add("evidence_ids_wrong_stage_or_role")
            else:
                codes.add("evidence_ids_invalid")
        if "evidence_units contains more than" in text:
            codes.add("evidence_units_too_many")
        if "exactly six items" in text:
            codes.add("stage_count")
        if "information" in text:
            codes.add("information_invalid_or_too_long")
        if "time_range" in text:
            codes.add("time_range_invalid_or_out_of_bounds")
        if "fact_quality" in text:
            codes.add("fact_quality_invalid")
        if ".relation" in text:
            codes.add("stage_relation_invalid")
        if ".gap_magnitude" in text:
            codes.add("gap_magnitude_invalid")
        if ".severity" in text:
            codes.add("legacy_severity_invalid")
    return sorted(codes) or ["contract_invalid"]


def _validate_relation_gap_pair(
    relation: Any,
    gap: Any,
    *,
    path: str,
    direction_field: str = "relation",
    magnitude_field: str = "gap_magnitude",
) -> list[str]:
    """Reject contradictory direction/magnitude combinations mechanically."""
    if relation == "tie" and gap not in {"none", "uncertain"}:
        return [f"{path}.{direction_field}=tie is incompatible with {magnitude_field}={gap!r}"]
    if gap == "none" and relation not in {"tie", "uncertain"}:
        return [f"{path}.{magnitude_field}=none is incompatible with {direction_field}={relation!r}"]
    return []


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


def _stage_frame_inputs(run_dir: Path, role: str) -> list[dict[str, str]]:
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
        for item in entries[:4]:
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
                    "data_url": image_to_data_url(path, max_bytes=4 * 1024 * 1024),
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


def _raw_video_cache_path(cache_dir: Path, role: str, source_sha256: str) -> Path:
    encoding_digest = _stable_digest(RAW_VIDEO_ENCODING)[:16]
    return cache_dir / f"{role}-{source_sha256[:16]}-{encoding_digest}.mp4"


def _data_url_bytes(data_url: str) -> bytes:
    prefix, separator, encoded = str(data_url).partition(",")
    if separator != "," or not prefix.startswith("data:video/"):
        raise CompactEvaluationError("raw video encoder returned an invalid data URL")
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise CompactEvaluationError("raw video encoder returned invalid base64") from exc


def _raw_video_inputs(run_dir: Path, *, cache_dir: Path | None = None) -> list[dict[str, Any]]:
    """Prepare bounded data URLs, reusing persisted bytes when available."""
    analysis = _read_json(run_dir / "analysis.json", required=False)
    videos = analysis.get("videos")
    if not isinstance(videos, dict):
        raise CompactEvaluationError("raw video extraction requires analysis.json videos")
    selected: list[dict[str, str]] = []
    for role in ("benchmark", "creator"):
        info = videos.get(role)
        path = Path(str(info.get("path") or "")).expanduser() if isinstance(info, dict) else Path()
        if not path.is_file():
            raise CompactEvaluationError(f"raw video input is missing for {role}: {path}")
        duration = None
        if isinstance(info, dict):
            try:
                candidate = float(info.get("duration_seconds"))
                if math.isfinite(candidate) and candidate > 0:
                    duration = candidate
            except (TypeError, ValueError):
                pass
        if duration is None:
            duration = probe_duration_seconds(path)
        if duration is None or not math.isfinite(duration) or duration <= 0:
            raise CompactEvaluationError(f"raw video duration is unavailable for {role}: {path}")
        source_sha256 = _file_digest(path)
        cached_path = (
            _raw_video_cache_path(cache_dir, role, source_sha256)
            if cache_dir is not None
            else None
        )
        if cached_path is not None and cached_path.is_file():
            try:
                data_url = encode_file_data_url(
                    cached_path,
                    max_bytes=RAW_VIDEO_ENCODING["max_data_bytes"],
                    expected_kind="video",
                )
            except (OSError, ValueError, ResourceBudgetExceeded) as exc:
                raise CompactEvaluationError(f"frozen raw-video cache is invalid: {cached_path}") from exc
        else:
            data_url = video_to_data_url(
                path,
                **RAW_VIDEO_ENCODING,
            )
            if data_url and cached_path is not None:
                raw_bytes = _data_url_bytes(data_url)
                if len(raw_bytes) >= RAW_VIDEO_ENCODING["max_data_bytes"]:
                    raise CompactEvaluationError("raw video encoder returned data at or above the byte limit")
                write_bytes(cached_path, raw_bytes)
                data_url = encode_file_data_url(
                    cached_path,
                    max_bytes=RAW_VIDEO_ENCODING["max_data_bytes"],
                    expected_kind="video",
                )
        if not data_url:
            raise CompactEvaluationError(f"raw video input could not be bounded for {role}: {path}")
        selected.append(
            {
                "role": role,
                "label": f"{role} raw video",
                "path": str(path.resolve()),
                "sha256": source_sha256,
                "data_url_sha256": hashlib.sha256(data_url.encode("utf-8")).hexdigest(),
                "duration_seconds": round(duration, 3),
                "data_url": data_url,
            }
        )
    return selected


def frozen_raw_video_source_identity(run_dir: Path) -> dict[str, Any]:
    """Return the source-file identity without encoding or sending video."""
    run_dir = run_dir.expanduser().resolve()
    analysis = _read_json(run_dir / "analysis.json", required=False)
    videos = analysis.get("videos")
    if not isinstance(videos, dict):
        raise CompactEvaluationError("raw video source identity requires analysis.json videos")
    identity: dict[str, Any] = {"video_role_order": [], "video_source_sha256": []}
    for role in ("benchmark", "creator"):
        info = videos.get(role)
        path = Path(str(info.get("path") or "")).expanduser() if isinstance(info, dict) else Path()
        if not path.is_file():
            raise CompactEvaluationError(f"raw video source is missing for {role}: {path}")
        identity["video_role_order"].append(role)
        identity["video_source_sha256"].append(_file_digest(path))
    return identity


def _load_frozen_bundle(
    run_dir: Path,
    *,
    include_images: bool,
    require_facts: bool,
    require_videos: bool = False,
    video_cache_dir: Path | None = None,
) -> FrozenCompactBundle:
    """Load one completed preprocessing run as a fixed cohort input."""
    run_dir = run_dir.expanduser().resolve()
    if not run_dir.is_dir():
        raise CompactEvaluationError(f"run directory does not exist: {run_dir}")
    foundation_record = _read_json(run_dir / "product_foundation.json")
    if foundation_record.get("completion_status") not in {None, "completed"}:
        raise CompactEvaluationError("product_foundation.json is not a completed artifact")
    foundation = foundation_record.get("foundation")
    if not isinstance(foundation, dict):
        raise CompactEvaluationError("product_foundation.json has no structured foundation")
    eligibility = _read_json(run_dir / "comparison_eligibility.json", required=False)
    facts = {role: _facts_for_role(run_dir, role) for role in ("creator", "benchmark")} if require_facts else {}
    allowed = {role: _allowed_evidence_ids(facts[role]) for role in facts}
    visual_inputs: list[dict[str, str]] = []
    if include_images:
        for role in ("benchmark", "creator"):
            visual_inputs.extend(_stage_frame_inputs(run_dir, role))
        if not visual_inputs:
            raise CompactEvaluationError("include_images was requested but no stage frames were found")
    video_inputs = (
        _raw_video_inputs(run_dir, cache_dir=video_cache_dir)
        if require_videos
        else []
    )
    if require_videos and not video_inputs:
        raise CompactEvaluationError("raw video extraction requires creator and benchmark videos")

    source_identity: dict[str, Any] = {
        "input_mode": (
            "raw_video_only"
            if require_videos
            else "locked_facts_and_frames"
            if require_facts
            else "visual_frames_only"
        ),
        "product_foundation": foundation,
        "comparison_eligibility": eligibility,
        "facts": facts,
        "visual_inputs": [
            {key: item[key] for key in ("role", "stage", "label", "path", "sha256")}
            for item in visual_inputs
        ],
    }
    if require_videos:
        source_identity["video_inputs"] = [
            {
                key: item[key]
                for key in ("role", "label", "path", "sha256", "data_url_sha256", "duration_seconds")
            }
            for item in video_inputs
        ]
        source_identity["raw_video_encoding"] = RAW_VIDEO_ENCODING
    if require_videos:
        experiment_boundary = (
            "这是原始视频视觉事实抽取实验，不是完整 Flayr 生产分析。"
            "只抽取原始视频明确支持的证据事实，不输出 severity、报告、improvements 或 derive 结果。"
        )
    elif require_facts:
        experiment_boundary = (
            "这是锁定事实包上的紧凑模型判断实验，不是完整 Flayr 生产分析。"
            "不得补写未在事实包或附图中出现的证据；不输出报告、improvements 或 derive 结果。"
        )
    else:
        experiment_boundary = (
            "这是固定关键帧上的视觉事实抽取实验，不是原始视频端到端生产分析。"
            "只抽取画面明确支持的证据事实，不输出 severity、报告、improvements 或 derive 结果。"
        )
    context = {
        "product_foundation": foundation,
        "comparison_eligibility": eligibility,
        "stages": [
            {"code": stage.code, "name": stage.name, "question": stage.core_question}
            for stage in DEFAULT_STAGES
        ],
        "facts": facts,
        "experiment_boundary": experiment_boundary,
    }
    return FrozenCompactBundle(
        run_dir=run_dir,
        context=context,
        allowed_evidence_ids=allowed,
        visual_inputs=tuple(visual_inputs),
        source_digest=_stable_digest(source_identity),
        stage_time_ranges=_stage_time_ranges(run_dir),
        input_mode=(
            "raw_video_only"
            if require_videos
            else "locked_facts_and_frames"
            if require_facts
            else "visual_frames_only"
        ),
        video_inputs=tuple(video_inputs),
    )


def load_frozen_compact_bundle(run_dir: Path, *, include_images: bool = True) -> FrozenCompactBundle:
    """Load frozen facts and optional frames for the judgment variants."""
    return _load_frozen_bundle(run_dir, include_images=include_images, require_facts=True)


def load_frozen_visual_bundle(run_dir: Path, *, include_images: bool = True) -> FrozenCompactBundle:
    """Load fixed frames without model-produced facts for extraction tests.

    This is deliberately named ``visual`` rather than ``video``: the current
    evaluator sends the same bounded stage contact sheets to every provider.
    A true raw-video extraction experiment requires a separate, provider-tested
    video input path and must not be silently conflated with this one.
    """
    return _load_frozen_bundle(run_dir, include_images=include_images, require_facts=False)


def load_frozen_video_bundle(
    run_dir: Path,
    *,
    cache_dir: Path | None = None,
) -> FrozenCompactBundle:
    """Load bounded original videos without facts or frame attachments.

    When ``cache_dir`` is supplied, the first load persists the exact bounded
    MP4 bytes and later processes reuse those bytes instead of re-encoding the
    source video. This is required for strict cross-process controls.
    """
    return _load_frozen_bundle(
        run_dir,
        include_images=False,
        require_facts=False,
        require_videos=True,
        video_cache_dir=cache_dir,
    )


def select_frozen_video_bundle(
    bundle: FrozenCompactBundle,
    roles: tuple[str, ...],
) -> FrozenCompactBundle:
    """Select or reorder already-encoded videos without reading or encoding them.

    Control experiments must vary only the number and order of video blocks. The
    returned bundle therefore reuses the exact ``data_url`` objects loaded into
    ``bundle`` and changes only the input selection metadata and digest.
    """
    if bundle.input_mode != "raw_video_only":
        raise CompactEvaluationError("video control selection requires a raw_video_only bundle")
    requested = tuple(str(role).strip() for role in roles)
    if not requested or any(role not in RAW_VIDEO_ROLES for role in requested):
        raise CompactEvaluationError(f"video control roles must use {RAW_VIDEO_ROLES}")
    if len(set(requested)) != len(requested):
        raise CompactEvaluationError("video control roles must not contain duplicates")
    by_role = {str(item.get("role")): item for item in bundle.video_inputs}
    if set(by_role) != set(RAW_VIDEO_ROLES):
        raise CompactEvaluationError("frozen raw-video bundle must contain benchmark and creator inputs")
    selected = tuple(by_role[role] for role in requested)
    context = dict(bundle.context)
    context["raw_video_roles"] = list(requested)
    context["raw_video_input_order"] = list(requested)
    source_identity = {
        "base_source_digest": bundle.source_digest,
        "input_mode": "raw_video_only",
        "raw_video_encoding": RAW_VIDEO_ENCODING,
        "video_inputs": [
            {
                key: item[key]
                for key in ("role", "label", "path", "sha256", "data_url_sha256", "duration_seconds")
            }
            for item in selected
        ],
    }
    return replace(
        bundle,
        context=context,
        source_digest=_stable_digest(source_identity),
        video_inputs=selected,
    )


def _build_multimodal_payload(
    model: str,
    bundle: FrozenCompactBundle,
    *,
    system: str,
    user_text: str,
    output_budget: int,
    output_budget_field: str,
) -> dict[str, Any]:
    """Build the common request envelope shared by every isolated variant."""
    if output_budget < 1024 or output_budget > 16384:
        raise CompactEvaluationError("compact output_budget must be between 1024 and 16384")
    if output_budget_field not in {"max_tokens", "max_completion_tokens"}:
        raise CompactEvaluationError("output_budget_field must be max_tokens or max_completion_tokens")
    if bundle.visual_inputs or bundle.video_inputs:
        content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
        for item in bundle.video_inputs:
            content.append({"type": "text", "text": f"原始视频：{item['label']}"})
            content.append({"type": "video_url", "video_url": {"url": item["data_url"]}})
        for item in bundle.visual_inputs:
            content.append({"type": "text", "text": f"附图：{item['label']}"})
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": item["data_url"], "detail": "low"},
                }
            )
        user_content: str | list[dict[str, Any]] = content
    else:
        user_content = user_text
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.0,
        output_budget_field: output_budget,
    }


def build_compact_eval_payload(
    model: str,
    bundle: FrozenCompactBundle,
    *,
    output_budget: int = COMPACT_OUTPUT_BUDGET,
    output_budget_field: str = "max_tokens",
    max_stage_evidence_ids: int | None = None,
) -> dict[str, Any]:
    """Build the same small response contract for every model under test."""
    evidence_id_limit = _stage_evidence_id_limit(max_stage_evidence_ids)
    response_shape = {
        "schema_version": COMPACT_EVAL_SCHEMA_VERSION,
        "stage_judgments": [
            {
                "stage": "S1 Hook",
                "severity": "small|medium|large",
                "confidence": "high|medium|low",
                "creator": {
                    "observation_state": "none|partial|complete|uncertain",
                    "evidence_ids": ["C1"],
                    "reason": "一句不超过240字的事实依据",
                },
                "benchmark": {
                    "observation_state": "none|partial|complete|uncertain",
                    "evidence_ids": ["B1"],
                    "reason": "一句不超过240字的事实依据",
                },
                "rationale": "一句不超过240字的阶段差距依据",
            }
        ] * 6,
    }
    system = (
        "你是 Flayr 的紧凑模型判断器。只输出严格 JSON，不要 Markdown，不要解释。"
        "这是模型横向能力实验，不是生产报告。只允许输出 response_shape 中的字段。"
        "必须固定输出六个阶段，顺序为 S1 Hook、S2 产品引出、S3 使用过程、S4 效果呈现、S5 信任放大、S6 CTA。"
        "severity 只能是 small、medium、large；不要使用模型之外的分数。"
        "每侧 observation_state 只能是 none、partial、complete、uncertain。"
        "evidence_ids 只能引用同侧、对应阶段事实清单中已经存在的 ID；没有明确证据时填空数组。"
        "不能把标杆事实复制成达人事实，也不能用相邻阶段 ID 补足当前阶段。"
        f"每侧每阶段最多引用 {evidence_id_limit} 个 evidence_ids；超过这个数量会被合同拒绝。"
        "不要输出 product foundation、improvements、建议话术、长篇摘要或任何额外字段。"
        "若视觉证据与事实包无法确定，填 uncertain，不要猜测。"
        f"严格输出形状示例：{json.dumps(response_shape, ensure_ascii=False, separators=(',', ':'))}"
    )
    user_text = (
        "请基于下面锁定的产品、阶段和视频事实完成六阶段比较。"
        "附图是同一批固定关键帧，仅用于视觉核对；不能把未附图的画面当作已观察事实。\n\n"
        + json.dumps(bundle.context, ensure_ascii=False, indent=2)
    )
    return _build_multimodal_payload(
        model,
        bundle,
        system=system,
        user_text=user_text,
        output_budget=output_budget,
        output_budget_field=output_budget_field,
    )


def build_model_independent_payload(
    model: str,
    bundle: FrozenCompactBundle,
    *,
    output_budget: int = COMPACT_OUTPUT_BUDGET,
    output_budget_field: str = "max_tokens",
    max_stage_evidence_ids: int | None = None,
) -> dict[str, Any]:
    """Build the frozen protocol's model-independent comparison contract."""
    evidence_id_limit = _stage_evidence_id_limit(max_stage_evidence_ids)
    response_shape = {
        "schema_version": MODEL_INDEPENDENT_SCHEMA_VERSION,
        "overall": {
            "winner": "benchmark|creator|tie|uncertain",
            "gap": "none|small|medium|large|uncertain",
            "confidence": "high|medium|low",
            "reason": "一句不超过320字的整体判断依据",
        },
        "stage_judgments": [
            {
                "stage": "S1 Hook",
                "relation": "benchmark_better|creator_better|tie|uncertain",
                "gap_magnitude": "none|small|medium|large|uncertain",
                "confidence": "high|medium|low",
                "creator": {
                    "observation_state": "none|partial|complete|uncertain",
                    "evidence_ids": ["C1"],
                    "reason": "一句不超过240字的事实依据",
                },
                "benchmark": {
                    "observation_state": "none|partial|complete|uncertain",
                    "evidence_ids": ["B1"],
                    "reason": "一句不超过240字的事实依据",
                },
                "rationale": "一句不超过240字的阶段差距依据",
            }
        ] * 6,
    }
    system = (
        "你是 Flayr 的模型独立比较判断器。只输出严格 JSON，不要 Markdown，不要解释。"
        "这是与人工初始判断并行的独立判断层，不得读取或假设存在任何 human_initial、GT 或其他模型结果。"
        "输入事实来自本模型此前完成的视觉事实抽取，事实包已经锁定；本次不得新增、改写或删除事实。"
        "必须输出 overall 和六个阶段，阶段顺序固定为 S1 Hook、S2 产品引出、S3 使用过程、S4 效果呈现、S5 信任放大、S6 CTA。"
        "overall.winner 只能是 benchmark、creator、tie、uncertain；overall.gap 只能是 none、small、medium、large、uncertain。"
        "阶段必须把方向和大小分开填写：relation 表示该阶段谁更好，只能是 benchmark_better、creator_better、tie、uncertain；"
        "gap_magnitude 表示差距大小，只能是 none、small、medium、large、uncertain。"
        "relation=tie 时 gap_magnitude 必须是 none 或 uncertain；gap_magnitude=none 时 relation 必须是 tie 或 uncertain。"
        "如果 creator 更好、双方持平或事实不确定，不要为了兼容旧 severity 把它强行写成 small；请使用对应 relation 和 gap_magnitude。"
        "每侧 observation_state 只能是 none、partial、complete、uncertain。"
        "evidence_ids 只能引用同侧、对应阶段事实清单中已经存在的 ID；没有明确证据时填空数组。"
        "不能把标杆事实复制成达人事实，也不能用相邻阶段 ID 补足当前阶段。"
        f"每侧每阶段最多引用 {evidence_id_limit} 个 evidence_ids；每条 reason 和 rationale 都必须是可核对的事实依据，"
        "不要输出隐藏推理过程、报告、improvements、derive 结果或任何额外字段。输出前检查所有 evidence_ids 的阶段归属，并确保整个 JSON 最后以一个完整的右花括号结束。"
        f"严格输出形状示例：{json.dumps(response_shape, ensure_ascii=False, separators=(',', ':'))}"
    )
    user_text = (
        "请独立完成一份整体比较和六阶段判断。只使用下面已经锁定的产品、阶段和本模型视觉事实；"
        "这份输出将与人工初始判断分层对齐，不能根据人工结论反向调整。\n\n"
        + json.dumps(bundle.context, ensure_ascii=False, indent=2)
    )
    return _build_multimodal_payload(
        model,
        bundle,
        system=system,
        user_text=user_text,
        output_budget=output_budget,
        output_budget_field=output_budget_field,
    )


def validate_model_independent_result(
    result: Any,
    bundle: FrozenCompactBundle,
    *,
    max_stage_evidence_ids: int | None = None,
) -> list[str]:
    """Validate the independent comparison contract without semantic repair."""
    evidence_id_limit = _stage_evidence_id_limit(max_stage_evidence_ids)
    if not isinstance(result, dict):
        return ["result must be an object"]
    errors: list[str] = []
    expected_root = {"schema_version", "overall", "stage_judgments"}
    if result.get("schema_version") != MODEL_INDEPENDENT_SCHEMA_VERSION:
        errors.append("schema_version mismatch")
    extra_root = set(result) - expected_root
    if extra_root:
        errors.append(f"unsupported root fields: {sorted(extra_root)}")

    overall = result.get("overall")
    if not isinstance(overall, dict):
        errors.append("overall must be an object")
    else:
        expected_overall = {"winner", "gap", "confidence", "reason"}
        extra = set(overall) - expected_overall
        missing = expected_overall - set(overall)
        if extra:
            errors.append(f"overall has unsupported fields: {sorted(extra)}")
        if missing:
            errors.append(f"overall is missing fields: {sorted(missing)}")
        if overall.get("winner") not in MODEL_INDEPENDENT_WINNERS:
            errors.append("overall.winner is invalid")
        if overall.get("gap") not in MODEL_INDEPENDENT_GAPS:
            errors.append("overall.gap is invalid")
        if overall.get("confidence") not in COMPACT_CONFIDENCES:
            errors.append("overall.confidence is invalid")
        reason = overall.get("reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > COMPACT_MAX_BASIS_CHARS:
            errors.append(f"overall.reason must be a non-empty string <= {COMPACT_MAX_BASIS_CHARS} chars")
        errors.extend(
            _validate_relation_gap_pair(
                overall.get("winner"),
                overall.get("gap"),
                path="overall",
                direction_field="winner",
                magnitude_field="gap",
            )
        )

    judgments = result.get("stage_judgments")
    expected_stages = [stage.name for stage in DEFAULT_STAGES]
    if not isinstance(judgments, list) or len(judgments) != len(expected_stages):
        return errors + ["stage_judgments must contain exactly six items"]
    for index, (item, expected_stage) in enumerate(zip(judgments, expected_stages)):
        path = f"stage_judgments[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{path} must be an object")
            continue
        expected_fields = {"stage", "relation", "gap_magnitude", "confidence", "creator", "benchmark", "rationale"}
        extra = set(item) - expected_fields
        missing = expected_fields - set(item)
        if extra:
            errors.append(f"{path} has unsupported fields: {sorted(extra)}")
        if missing:
            errors.append(f"{path} is missing fields: {sorted(missing)}")
        if item.get("stage") != expected_stage:
            errors.append(f"{path}.stage must be {expected_stage!r}")
        if item.get("relation") not in MODEL_INDEPENDENT_RELATIONS:
            errors.append(f"{path}.relation is invalid")
        if item.get("gap_magnitude") not in MODEL_INDEPENDENT_GAPS:
            errors.append(f"{path}.gap_magnitude is invalid")
        if item.get("confidence") not in COMPACT_CONFIDENCES:
            errors.append(f"{path}.confidence is invalid")
        rationale = item.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > COMPACT_MAX_REASON_CHARS:
            errors.append(f"{path}.rationale must be a non-empty string <= {COMPACT_MAX_REASON_CHARS} chars")
        errors.extend(
            _validate_relation_gap_pair(
                item.get("relation"),
                item.get("gap_magnitude"),
                path=path,
            )
        )
        try:
            stage_code = _stage_code(str(item.get("stage") or ""))
        except CompactEvaluationError:
            continue
        errors.extend(
            _validate_side(
                item.get("creator"),
                role="creator",
                stage_code=stage_code,
                allowed_ids=bundle.allowed_evidence_ids,
                path=f"{path}.creator",
                max_stage_evidence_ids=evidence_id_limit,
            )
        )
        errors.extend(
            _validate_side(
                item.get("benchmark"),
                role="benchmark",
                stage_code=stage_code,
                allowed_ids=bundle.allowed_evidence_ids,
                path=f"{path}.benchmark",
                max_stage_evidence_ids=evidence_id_limit,
            )
        )
    return errors


def _s4_state_shape() -> dict[str, Any]:
    return {
        "schema_version": S4_FACT_STATE_SCHEMA_VERSION,
        "stage": "S4 效果呈现",
        "creator": {
            "effect_evidence_state": "none|result_only|verified|uncertain",
            "visibility": "clear|partial|obscured|uncertain|not_applicable",
            "proof": "direct_comparison|result_only|claim_only|none|uncertain|not_applicable",
            "causal_link": "supported|weak|unsupported|uncertain|not_applicable",
            "evidence_ids": ["C1"],
            "reason": "一句不超过240字的事实依据",
        },
        "benchmark": {
            "effect_evidence_state": "none|result_only|verified|uncertain",
            "visibility": "clear|partial|obscured|uncertain|not_applicable",
            "proof": "direct_comparison|result_only|claim_only|none|uncertain|not_applicable",
            "causal_link": "supported|weak|unsupported|uncertain|not_applicable",
            "evidence_ids": ["B1"],
            "reason": "一句不超过240字的事实依据",
        },
    }


def build_s4_fact_state_payload(
    model: str,
    bundle: FrozenCompactBundle,
    *,
    output_budget: int = COMPACT_OUTPUT_BUDGET,
    output_budget_field: str = "max_tokens",
) -> dict[str, Any]:
    """Build the first S4 step: classify effect evidence from locked facts."""

    response_shape = _s4_state_shape()
    system = (
        "你是 Flayr 的 S4 事实状态判定器。只输出严格 JSON，不要 Markdown，不要解释。"
        "本次只判定双方效果证据状态，不输出 severity、relation、gap 或改进建议。"
        "输入事实已经锁定；不得新增、改写、合并或跨角色移动 evidence_units。"
        "effect_evidence_state 的判断必须严格区分：none=没有效果证据；"
        "result_only=只有结果画面或结果口播，没有产品导致结果的因果桥；"
        "verified=效果肉眼可见，且产品使用/过程与结果之间存在可信连接；"
        "uncertain=事实冲突、看不清或证据不足。"
        "visibility、proof、causal_link 只能描述锁定事实本身；不确定就填 uncertain。"
        f"evidence_ids 只能引用同侧 S4 事实，最多 {S4_MAX_EVIDENCE_IDS} 个；不能引用相邻阶段。"
        f"严格输出形状示例：{json.dumps(response_shape, ensure_ascii=False, separators=(',', ':'))}"
    )
    user_text = (
        "请先分别判断 creator 和 benchmark 的 S4 效果事实状态。"
        "这一步的结果会被锁定后交给下一步判断层，不能提前比较谁更好。\n\n"
        + json.dumps(bundle.context, ensure_ascii=False, indent=2)
    )
    return _build_multimodal_payload(
        model,
        bundle,
        system=system,
        user_text=user_text,
        output_budget=output_budget,
        output_budget_field=output_budget_field,
    )


def _validate_s4_state_side(
    value: Any,
    *,
    role: str,
    allowed_ids: dict[str, set[str]],
    path: str,
) -> list[str]:
    if not isinstance(value, dict):
        return [f"{path} must be an object"]
    expected = {"effect_evidence_state", "visibility", "proof", "causal_link", "evidence_ids", "reason"}
    errors: list[str] = []
    extra = set(value) - expected
    missing = expected - set(value)
    if extra:
        errors.append(f"{path} has unsupported fields: {sorted(extra)}")
    if missing:
        errors.append(f"{path} is missing fields: {sorted(missing)}")
    if value.get("effect_evidence_state") not in S4_EFFECT_EVIDENCE_STATES:
        errors.append(f"{path}.effect_evidence_state is invalid")
    if value.get("visibility") not in S4_STATE_VISIBILITY:
        errors.append(f"{path}.visibility is invalid")
    if value.get("proof") not in S4_STATE_PROOF:
        errors.append(f"{path}.proof is invalid")
    if value.get("causal_link") not in S4_STATE_CAUSAL_LINK:
        errors.append(f"{path}.causal_link is invalid")
    evidence_ids = value.get("evidence_ids")
    if not isinstance(evidence_ids, list) or any(not isinstance(item, str) or not item.strip() for item in evidence_ids):
        errors.append(f"{path}.evidence_ids must be a list of non-empty strings")
        evidence_ids = []
    if len(evidence_ids) > S4_MAX_EVIDENCE_IDS:
        errors.append(f"{path}.evidence_ids exceeds max_stage_evidence_ids={S4_MAX_EVIDENCE_IDS}")
    if len(set(evidence_ids)) != len(evidence_ids):
        errors.append(f"{path}.evidence_ids contains duplicate IDs")
    for evidence_id in evidence_ids:
        if evidence_id not in allowed_ids.get(role, {}).get("S4", set()):
            errors.append(f"{path}.evidence_ids contains {evidence_id!r} outside {role}/S4")
    state = value.get("effect_evidence_state")
    proof = value.get("proof")
    causal_link = value.get("causal_link")
    if state in {"result_only", "verified"} and not evidence_ids:
        errors.append(f"{path}.{state} requires evidence_ids")
    if state == "none":
        if proof in {"direct_comparison", "result_only", "claim_only"}:
            errors.append(f"{path}.none has effect proof")
        if causal_link == "supported":
            errors.append(f"{path}.none has supported causal_link")
    elif state == "result_only":
        if proof == "direct_comparison":
            errors.append(f"{path}.result_only has direct_comparison proof")
        if causal_link == "supported":
            errors.append(f"{path}.result_only has supported causal_link")
    elif state == "verified":
        if proof != "direct_comparison":
            errors.append(f"{path}.verified requires direct_comparison proof")
        if causal_link != "supported":
            errors.append(f"{path}.verified requires supported causal_link")
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > COMPACT_MAX_REASON_CHARS:
        errors.append(f"{path}.reason must be a non-empty string <= {COMPACT_MAX_REASON_CHARS} chars")
    return errors


def validate_s4_fact_state_result(result: Any, bundle: FrozenCompactBundle) -> list[str]:
    """Validate the first S4 step without semantic repair."""

    if not isinstance(result, dict):
        return ["result must be an object"]
    errors: list[str] = []
    expected_root = {"schema_version", "stage", "creator", "benchmark"}
    if result.get("schema_version") != S4_FACT_STATE_SCHEMA_VERSION:
        errors.append("schema_version mismatch")
    extra = set(result) - expected_root
    missing = expected_root - set(result)
    if extra:
        errors.append(f"unsupported root fields: {sorted(extra)}")
    if missing:
        errors.append(f"missing root fields: {sorted(missing)}")
    if result.get("stage") != "S4 效果呈现":
        errors.append("stage must be S4 效果呈现")
    for role in RAW_VIDEO_ROLES:
        errors.extend(
            _validate_s4_state_side(
                result.get(role),
                role=role,
                allowed_ids=bundle.allowed_evidence_ids,
                path=role,
            )
        )
    return errors


def _s4_single_pass_shape() -> dict[str, Any]:
    return {
        **_s4_state_shape(),
        "schema_version": S4_SINGLE_PASS_SCHEMA_VERSION,
        "relation": "benchmark_better|creator_better|tie|uncertain",
        "gap_magnitude": "none|small|medium|large|uncertain",
        "confidence": "high|medium|low",
        "decision_basis": "一到两句不超过320字的可审计判断依据",
    }


def build_s4_single_pass_payload(
    model: str,
    bundle: FrozenCompactBundle,
    *,
    output_budget: int = COMPACT_OUTPUT_BUDGET,
    output_budget_field: str = "max_tokens",
) -> dict[str, Any]:
    """Build a one-call S4 state-and-judgment control contract.

    This deliberately keeps S4 evidence-state classification and the final
    comparison in one response.  It is the one-call control for the separate
    locked-state two-call experiment below; neither path reaches production
    analysis or the resolver.
    """

    response_shape = _s4_single_pass_shape()
    system = (
        "你是 Flayr 的 S4 单次混合判断器。只输出严格 JSON，不要 Markdown，不要解释。"
        "输入事实已经锁定；不得新增、改写、合并或跨角色移动 evidence_units。"
        "在同一个 JSON 中，先分别填写 creator 与 benchmark 的效果事实状态，再填写 relation 和 gap_magnitude。"
        "effect_evidence_state 必须严格区分：none=没有效果证据；"
        "result_only=只有结果画面或结果口播，没有产品导致结果的因果桥；"
        "verified=效果肉眼可见，且产品使用/过程与结果之间存在可信连接；"
        "uncertain=事实冲突、看不清或证据不足。"
        "visibility、proof、causal_link 只能描述锁定事实本身；不确定就填 uncertain。"
        f"evidence_ids 只能引用同侧 S4 事实，最多 {S4_MAX_EVIDENCE_IDS} 个；不能引用相邻阶段。"
        "基于刚填写的双方状态给出简短 decision_basis，再输出 relation 和 gap_magnitude。"
        "relation=tie 时 gap_magnitude 只能是 none 或 uncertain；gap_magnitude=none 时 relation 只能是 tie 或 uncertain。"
        f"严格输出形状示例：{json.dumps(response_shape, ensure_ascii=False, separators=(',', ':'))}"
    )
    user_text = (
        "请执行一次完整但隔离的 S4 判断：先写双方事实状态，再判断双方关系和差距。"
        "不要输出其他阶段、severity 或改进建议。\n\n"
        + json.dumps(bundle.context, ensure_ascii=False, indent=2)
    )
    return _build_multimodal_payload(
        model,
        bundle,
        system=system,
        user_text=user_text,
        output_budget=output_budget,
        output_budget_field=output_budget_field,
    )


def _validate_s4_judgment_fields(result: dict[str, Any], *, path: str) -> list[str]:
    errors: list[str] = []
    if result.get("relation") not in MODEL_INDEPENDENT_RELATIONS:
        errors.append(f"{path}.relation is invalid")
    if result.get("gap_magnitude") not in MODEL_INDEPENDENT_GAPS:
        errors.append(f"{path}.gap_magnitude is invalid")
    if result.get("confidence") not in COMPACT_CONFIDENCES:
        errors.append(f"{path}.confidence is invalid")
    basis = result.get("decision_basis")
    if not isinstance(basis, str) or not basis.strip() or len(basis) > COMPACT_MAX_BASIS_CHARS:
        errors.append(f"{path}.decision_basis must be a non-empty string <= {COMPACT_MAX_BASIS_CHARS} chars")
    errors.extend(
        _validate_relation_gap_pair(
            result.get("relation"),
            result.get("gap_magnitude"),
            path=path,
        )
    )
    return errors


def validate_s4_single_pass_result(result: Any, bundle: FrozenCompactBundle) -> list[str]:
    """Validate the one-call S4 state-and-judgment control without repair."""

    if not isinstance(result, dict):
        return ["result must be an object"]
    expected_root = {
        "schema_version",
        "stage",
        "creator",
        "benchmark",
        "relation",
        "gap_magnitude",
        "confidence",
        "decision_basis",
    }
    errors: list[str] = []
    if result.get("schema_version") != S4_SINGLE_PASS_SCHEMA_VERSION:
        errors.append("schema_version mismatch")
    extra = set(result) - expected_root
    missing = expected_root - set(result)
    if extra:
        errors.append(f"unsupported root fields: {sorted(extra)}")
    if missing:
        errors.append(f"missing root fields: {sorted(missing)}")
    if result.get("stage") != "S4 效果呈现":
        errors.append("stage must be S4 效果呈现")
    for role in RAW_VIDEO_ROLES:
        errors.extend(
            _validate_s4_state_side(
                result.get(role),
                role=role,
                allowed_ids=bundle.allowed_evidence_ids,
                path=role,
            )
        )
    errors.extend(_validate_s4_judgment_fields(result, path="s4"))
    return errors


def _s4_free_text_steps_shape() -> dict[str, Any]:
    return {
        "schema_version": S4_FREE_TEXT_STEPS_SCHEMA_VERSION,
        "stage": "S4 效果呈现",
        "creator_stage_facts": "不超过240字的 creator S4 事实描述",
        "benchmark_stage_facts": "不超过240字的 benchmark S4 事实描述",
        "comparison": "不超过240字的双方差异描述",
        "purchase_impact": "不超过240字的该差异对购买说服力影响",
        "relation": "benchmark_better|creator_better|tie|uncertain",
        "gap_magnitude": "none|small|medium|large|uncertain",
        "confidence": "high|medium|low",
    }


def build_s4_free_text_steps_payload(
    model: str,
    bundle: FrozenCompactBundle,
    *,
    output_budget: int = COMPACT_OUTPUT_BUDGET,
    output_budget_field: str = "max_tokens",
) -> dict[str, Any]:
    """Build a one-call free-text S4 reasoning control with fixed outputs."""

    response_shape = _s4_free_text_steps_shape()
    system = (
        "你是 Flayr 的 S4 五步自由文本判断器。只输出严格 JSON，不要 Markdown，不要解释。"
        "输入事实已经锁定；不得新增、改写、合并或跨角色移动 evidence_units。"
        "严格按输出字段的顺序完成五步："
        "(1) creator_stage_facts，(2) benchmark_stage_facts，(3) comparison，"
        "(4) purchase_impact，(5) relation 与 gap_magnitude。"
        "前四步是简短、可审计的公开判断依据，不要输出隐藏推理过程。"
        "不要输出 effect_evidence_state、visibility、proof、causal_link、evidence_ids、severity 或改进建议。"
        "relation=tie 时 gap_magnitude 只能是 none 或 uncertain；gap_magnitude=none 时 relation 只能是 tie 或 uncertain。"
        f"每段自由文本最多 {S4_FREE_TEXT_MAX_CHARS} 字符。"
        f"严格输出形状示例：{json.dumps(response_shape, ensure_ascii=False, separators=(',', ':'))}"
    )
    user_text = (
        "请只基于锁定的 S4 事实按五步完成比较，不要补充输入中不存在的视觉或口播内容。\n\n"
        + json.dumps(bundle.context, ensure_ascii=False, indent=2)
    )
    return _build_multimodal_payload(
        model,
        bundle,
        system=system,
        user_text=user_text,
        output_budget=output_budget,
        output_budget_field=output_budget_field,
    )


def validate_s4_free_text_steps_result(result: Any) -> list[str]:
    """Validate the free-text control while retaining its intentionally open semantics."""

    if not isinstance(result, dict):
        return ["result must be an object"]
    expected = {
        "schema_version",
        "stage",
        "creator_stage_facts",
        "benchmark_stage_facts",
        "comparison",
        "purchase_impact",
        "relation",
        "gap_magnitude",
        "confidence",
    }
    errors: list[str] = []
    if result.get("schema_version") != S4_FREE_TEXT_STEPS_SCHEMA_VERSION:
        errors.append("schema_version mismatch")
    extra = set(result) - expected
    missing = expected - set(result)
    if extra:
        errors.append(f"unsupported root fields: {sorted(extra)}")
    if missing:
        errors.append(f"missing root fields: {sorted(missing)}")
    if result.get("stage") != "S4 效果呈现":
        errors.append("stage must be S4 效果呈现")
    for field_name in ("creator_stage_facts", "benchmark_stage_facts", "comparison", "purchase_impact"):
        value = result.get(field_name)
        if not isinstance(value, str) or not value.strip() or len(value) > S4_FREE_TEXT_MAX_CHARS:
            errors.append(f"{field_name} must be a non-empty string <= {S4_FREE_TEXT_MAX_CHARS} chars")
    if result.get("relation") not in MODEL_INDEPENDENT_RELATIONS:
        errors.append("relation is invalid")
    if result.get("gap_magnitude") not in MODEL_INDEPENDENT_GAPS:
        errors.append("gap_magnitude is invalid")
    if result.get("confidence") not in COMPACT_CONFIDENCES:
        errors.append("confidence is invalid")
    errors.extend(
        _validate_relation_gap_pair(
            result.get("relation"),
            result.get("gap_magnitude"),
            path="s4",
        )
    )
    return errors


def validate_s4_fact_state_artifact_metadata(
    record: Any,
    *,
    expected_model: str,
    expected_source_digest: str,
) -> list[str]:
    """Validate the provenance envelope before a locked S4 state is consumed."""
    if not isinstance(record, dict):
        return ["artifact root must be an object"]
    errors: list[str] = []
    if record.get("status") != "completed":
        errors.append("artifact status must be completed")
    if record.get("variant") != "s4_fact_state":
        errors.append("artifact variant must be s4_fact_state")
    if record.get("schema_version") != S4_FACT_STATE_SCHEMA_VERSION:
        errors.append("artifact schema_version mismatch")
    if record.get("model") != expected_model:
        errors.append("artifact model does not match requested judgment model")
    if record.get("source_digest") != expected_source_digest:
        errors.append("artifact source_digest does not match the locked base bundle")
    for provenance_field in ("source_commit", "protocol_hash"):
        value = record.get(provenance_field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"artifact is missing provenance field: {provenance_field}")
    return errors


def build_s4_state_locked_bundle(
    base_bundle: FrozenCompactBundle,
    state_result: dict[str, Any],
    *,
    state_artifact: str = "",
    state_source_digest: str | None = None,
    state_model: str | None = None,
    expected_model: str | None = None,
) -> FrozenCompactBundle:
    """Create the second-step S4 bundle from a validated, immutable state result."""

    if state_artifact and not state_source_digest:
        raise CompactEvaluationError("S4 fact state artifact is missing source_digest provenance")
    if state_source_digest is not None and state_source_digest != base_bundle.source_digest:
        raise CompactEvaluationError("S4 fact state source_digest does not match the locked base bundle")
    if expected_model is not None and state_model != expected_model:
        raise CompactEvaluationError("S4 fact state was produced by a different model")
    locked_state = deepcopy(state_result)
    errors = validate_s4_fact_state_result(locked_state, base_bundle)
    if errors:
        raise CompactEvaluationError("invalid S4 fact state: " + "; ".join(errors[:8]))
    # The second step must not receive the original fact pack again.  Keeping
    # it in the prompt would let the judgment model silently re-extract or
    # reinterpret facts, defeating the extraction-vs-judgment split.
    context = {
        "s4_fact_state": locked_state,
    }
    context["experiment_boundary"] = (
        "这是 S4 两步判断实验的第二步。S4 effect_evidence_state、proof、causal_link 和 visibility "
        "已经由第一步判定并锁定；本次只能基于这些状态输出 relation、gap_magnitude 和简短依据，不能重判或改写事实。"
    )
    provenance = {
        "state_artifact": state_artifact,
        "state_model": state_model,
        "state_source_digest": state_source_digest,
        "state_result_digest": _stable_digest(locked_state),
        "base_source_digest": base_bundle.source_digest,
        "human_initial_loaded": False,
        "gt_loaded": False,
    }
    context["s4_fact_state_provenance"] = provenance
    return replace(
        base_bundle,
        context=context,
        source_digest=_stable_digest({"base": base_bundle.source_digest, "s4_state": locked_state}),
        input_mode="s4_fact_state_locked",
        visual_inputs=(),
        video_inputs=(),
    )


def build_s4_judgment_payload(
    model: str,
    bundle: FrozenCompactBundle,
    *,
    output_budget: int = COMPACT_OUTPUT_BUDGET,
    output_budget_field: str = "max_tokens",
) -> dict[str, Any]:
    """Build the second S4 step: judge relation and gap from locked state."""

    if bundle.input_mode != "s4_fact_state_locked" or "s4_fact_state" not in bundle.context:
        raise CompactEvaluationError("S4 judgment requires a locked S4 fact-state bundle")
    response_shape = {
        "schema_version": S4_JUDGMENT_SCHEMA_VERSION,
        "stage": "S4 效果呈现",
        "relation": "benchmark_better|creator_better|tie|uncertain",
        "gap_magnitude": "none|small|medium|large|uncertain",
        "confidence": "high|medium|low",
        "decision_basis": "一到两句不超过320字的可审计判断依据",
    }
    system = (
        "你是 Flayr 的 S4 判断器。只输出严格 JSON，不要 Markdown，不要解释。"
        "输入中的 S4 事实状态已经锁定，不得重新抽取、改写或补充证据。"
        "先根据 creator 与 benchmark 的 effect_evidence_state、proof、causal_link、visibility 形成简短判断依据，"
        "再输出 relation 和 gap_magnitude。relation 只能是 benchmark_better、creator_better、tie、uncertain；"
        "gap_magnitude 只能是 none、small、medium、large、uncertain。"
        "relation=tie 时 gap_magnitude 只能是 none 或 uncertain；gap_magnitude=none 时 relation 只能是 tie 或 uncertain。"
        f"严格输出形状示例：{json.dumps(response_shape, ensure_ascii=False, separators=(',', ':'))}"
    )
    user_text = (
        "请只基于已经锁定的 S4 事实状态进行比较。不要把 result_only 当作 verified，"
        "也不要因为事实不确定而强行给出 large。\n\n"
        + json.dumps(bundle.context, ensure_ascii=False, indent=2)
    )
    return _build_multimodal_payload(
        model,
        bundle,
        system=system,
        user_text=user_text,
        output_budget=output_budget,
        output_budget_field=output_budget_field,
    )


def validate_s4_judgment_result(result: Any) -> list[str]:
    if not isinstance(result, dict):
        return ["result must be an object"]
    expected = {"schema_version", "stage", "relation", "gap_magnitude", "confidence", "decision_basis"}
    errors: list[str] = []
    if result.get("schema_version") != S4_JUDGMENT_SCHEMA_VERSION:
        errors.append("schema_version mismatch")
    extra = set(result) - expected
    missing = expected - set(result)
    if extra:
        errors.append(f"unsupported root fields: {sorted(extra)}")
    if missing:
        errors.append(f"missing root fields: {sorted(missing)}")
    if result.get("stage") != "S4 效果呈现":
        errors.append("stage must be S4 效果呈现")
    if result.get("relation") not in MODEL_INDEPENDENT_RELATIONS:
        errors.append("relation is invalid")
    if result.get("gap_magnitude") not in MODEL_INDEPENDENT_GAPS:
        errors.append("gap_magnitude is invalid")
    if result.get("confidence") not in COMPACT_CONFIDENCES:
        errors.append("confidence is invalid")
    basis = result.get("decision_basis")
    if not isinstance(basis, str) or not basis.strip() or len(basis) > COMPACT_MAX_BASIS_CHARS:
        errors.append(f"decision_basis must be a non-empty string <= {COMPACT_MAX_BASIS_CHARS} chars")
    errors.extend(
        _validate_relation_gap_pair(
            result.get("relation"),
            result.get("gap_magnitude"),
            path="s4",
        )
    )
    return errors


def build_s5_audit_payload(
    model: str,
    bundle: FrozenCompactBundle,
    *,
    output_budget: int = COMPACT_OUTPUT_BUDGET,
    output_budget_field: str = "max_tokens",
) -> dict[str, Any]:
    """Build an audit-only S5 source-state and comparison contract."""

    response_shape = {
        "schema_version": S5_AUDIT_SCHEMA_VERSION,
        "stage": "S5 信任放大",
        "creator": {
            "trust_state": "explicit_absence|product_claim_or_offer|credible_source|uncertain",
            "evidence_ids": ["C1"],
            "reason": "一句不超过240字的事实依据",
        },
        "benchmark": {
            "trust_state": "explicit_absence|product_claim_or_offer|credible_source|uncertain",
            "evidence_ids": ["B1"],
            "reason": "一句不超过240字的事实依据",
        },
        "relation": "benchmark_better|creator_better|tie|uncertain",
        "gap_magnitude": "none|small|medium|large|uncertain",
        "confidence": "high|medium|low",
        "decision_basis": "一到两句不超过320字的可审计判断依据",
    }
    system = (
        "你是 Flayr 的 S5 来源审计器。只输出严格 JSON，不要 Markdown，不要解释。"
        "本结果只用于 audit，不改写生产 severity。先分别分类双方信任来源，再比较方向和差距。"
        "explicit_absence 只有在事实明确显示没有独立来源时使用；缺字段、看不清或不确定必须使用 uncertain。"
        "product_claim_or_offer 表示只有产品功效主张、参数、价格、优惠或赠品，没有独立信任来源；"
        "它不能与 explicit_absence 等同。credible_source 必须有可见、可追溯且可信的来源事实。"
        f"evidence_ids 只能引用同侧 S5 事实，最多 {S5_MAX_EVIDENCE_IDS} 个。"
        "relation 和 gap_magnitude 必须分开判断；不要把未知当作双方都没有。"
        f"严格输出形状示例：{json.dumps(response_shape, ensure_ascii=False, separators=(',', ':'))}"
    )
    user_text = "请基于已锁定事实完成 S5 来源审计和比较。\n\n" + json.dumps(bundle.context, ensure_ascii=False, indent=2)
    return _build_multimodal_payload(
        model,
        bundle,
        system=system,
        user_text=user_text,
        output_budget=output_budget,
        output_budget_field=output_budget_field,
    )


def _validate_s5_audit_side(value: Any, *, role: str, allowed_ids: dict[str, set[str]], path: str) -> list[str]:
    if not isinstance(value, dict):
        return [f"{path} must be an object"]
    expected = {"trust_state", "evidence_ids", "reason"}
    errors: list[str] = []
    extra = set(value) - expected
    missing = expected - set(value)
    if extra:
        errors.append(f"{path} has unsupported fields: {sorted(extra)}")
    if missing:
        errors.append(f"{path} is missing fields: {sorted(missing)}")
    if value.get("trust_state") not in S5_AUDIT_STATES:
        errors.append(f"{path}.trust_state is invalid")
    ids = value.get("evidence_ids")
    if not isinstance(ids, list) or any(not isinstance(item, str) or not item.strip() for item in ids):
        errors.append(f"{path}.evidence_ids must be a list of non-empty strings")
        ids = []
    if len(ids) > S5_MAX_EVIDENCE_IDS:
        errors.append(f"{path}.evidence_ids exceeds max_stage_evidence_ids={S5_MAX_EVIDENCE_IDS}")
    if len(set(ids)) != len(ids):
        errors.append(f"{path}.evidence_ids contains duplicate IDs")
    for evidence_id in ids:
        if evidence_id not in allowed_ids.get(role, {}).get("S5", set()):
            errors.append(f"{path}.evidence_ids contains {evidence_id!r} outside {role}/S5")
    trust_state = value.get("trust_state")
    if trust_state in {"product_claim_or_offer", "credible_source"} and not ids:
        errors.append(f"{path}: {trust_state} requires evidence_ids")
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > COMPACT_MAX_REASON_CHARS:
        errors.append(f"{path}.reason must be a non-empty string <= {COMPACT_MAX_REASON_CHARS} chars")
    return errors


def validate_s5_audit_result(result: Any, bundle: FrozenCompactBundle) -> list[str]:
    if not isinstance(result, dict):
        return ["result must be an object"]
    expected = {"schema_version", "stage", "creator", "benchmark", "relation", "gap_magnitude", "confidence", "decision_basis"}
    errors: list[str] = []
    if result.get("schema_version") != S5_AUDIT_SCHEMA_VERSION:
        errors.append("schema_version mismatch")
    extra = set(result) - expected
    missing = expected - set(result)
    if extra:
        errors.append(f"unsupported root fields: {sorted(extra)}")
    if missing:
        errors.append(f"missing fields: {sorted(missing)}")
    if result.get("stage") != "S5 信任放大":
        errors.append("stage must be S5 信任放大")
    for role in RAW_VIDEO_ROLES:
        errors.extend(_validate_s5_audit_side(result.get(role), role=role, allowed_ids=bundle.allowed_evidence_ids, path=role))
    if result.get("relation") not in MODEL_INDEPENDENT_RELATIONS:
        errors.append("relation is invalid")
    if result.get("gap_magnitude") not in MODEL_INDEPENDENT_GAPS:
        errors.append("gap_magnitude is invalid")
    if result.get("confidence") not in COMPACT_CONFIDENCES:
        errors.append("confidence is invalid")
    basis = result.get("decision_basis")
    if not isinstance(basis, str) or not basis.strip() or len(basis) > COMPACT_MAX_BASIS_CHARS:
        errors.append(f"decision_basis must be a non-empty string <= {COMPACT_MAX_BASIS_CHARS} chars")
    errors.extend(_validate_relation_gap_pair(result.get("relation"), result.get("gap_magnitude"), path="s5"))
    return errors


def build_severity_only_payload(
    model: str,
    bundle: FrozenCompactBundle,
    *,
    scaffold: bool = False,
    output_budget: int = COMPACT_OUTPUT_BUDGET,
    output_budget_field: str = "max_tokens",
) -> dict[str, Any]:
    """Build a judgment-only contract over the same frozen facts.

    The base variant measures severity judgment without asking the model to
    extract or cite facts. The scaffold variant adds a short audit rationale;
    it is intentionally not chain-of-thought and is capped at one or two
    sentences so its effect can be tested without changing the evidence input.
    """
    row: dict[str, Any] = {
        "stage": "S1 Hook",
    }
    if scaffold:
        row["decision_basis"] = "一到两句简短、可审计的判断依据"
    row.update({"severity": "small|medium|large", "confidence": "high|medium|low"})
    response_shape = {
        "schema_version": COMPACT_EVAL_SCHEMA_VERSION,
        "stage_judgments": [row] * 6,
    }
    system = (
        "你是 Flayr 的严重度判断器。只输出严格 JSON，不要 Markdown，不要解释。"
        "所有模型必须使用同一份锁定事实包和同一套输出合同；这是校准实验，不是生产分析。"
        "不要重新抽取视觉事实，不要输出 evidence_ids、观察状态、报告、improvements 或 derive 结果。"
        "必须固定输出六个阶段，顺序为 S1 Hook、S2 产品引出、S3 使用过程、S4 效果呈现、S5 信任放大、S6 CTA。"
        "severity 只能是 small、medium、large；confidence 只能是 high、medium、low。"
    )
    if scaffold:
        system += (
            "每个阶段先填写 decision_basis，再给出 severity；decision_basis 只能是一到两句简短判断依据，"
            f"长度不超过 {COMPACT_MAX_BASIS_CHARS} 字，不要写隐藏推理过程。"
        )
    else:
        system += "只保留 stage、severity、confidence 三个阶段字段，不要添加任何理由字段。"
    system += f"严格输出形状示例：{json.dumps(response_shape, ensure_ascii=False, separators=(',', ':'))}"
    user_text = (
        "请仅根据下面已经校验并锁定的事实判断每个阶段的严重度。"
        "不要因为事实包中缺少信息而自行补事实；信息不足时按你能支持的判断输出 confidence，不要改写事实包。\n\n"
        + json.dumps(bundle.context, ensure_ascii=False, indent=2)
    )
    return _build_multimodal_payload(
        model,
        bundle,
        system=system,
        user_text=user_text,
        output_budget=output_budget,
        output_budget_field=output_budget_field,
    )


def _visual_extraction_roles(bundle: FrozenCompactBundle) -> tuple[str, ...]:
    """Return the role order represented by a visual extraction input."""
    if bundle.input_mode == "raw_video_only":
        roles = tuple(str(item.get("role") or "").strip() for item in bundle.video_inputs)
    else:
        roles = ("creator", "benchmark")
    if not roles or any(role not in RAW_VIDEO_ROLES for role in roles):
        raise CompactEvaluationError("visual extraction input must contain creator and/or benchmark roles")
    if len(set(roles)) != len(roles):
        raise CompactEvaluationError("visual extraction input contains duplicate roles")
    return roles


def _visual_extraction_response_shape(roles: tuple[str, ...]) -> dict[str, Any]:
    shape: dict[str, Any] = {"schema_version": VISUAL_EXTRACTION_SCHEMA_VERSION}
    for role in roles:
        prefix = "C" if role == "creator" else "B"
        shape[f"{role}_evidence_units"] = [
            {
                "id": f"{prefix}1",
                "time_range": "0.0s - 1.0s",
                "information": "画面明确支持的事实",
                "functions": ["S1"],
                "evidence_strength": "direct|explicit|inferred|absent",
                "fact_quality": {
                    "subject": "correct|incorrect|uncertain|not_applicable",
                    "visibility": "clear|partial|obscured|uncertain|not_applicable",
                    "composition": "central|supporting|weak|uncertain|not_applicable",
                    "completion": "complete|partial|none|uncertain|not_applicable",
                    "proof": "direct_comparison|result_only|claim_only|none|uncertain|not_applicable",
                    "causal_link": "supported|weak|unsupported|uncertain|not_applicable",
                },
            }
        ]
    return shape


def build_visual_extraction_payload(
    model: str,
    bundle: FrozenCompactBundle,
    *,
    output_budget: int = COMPACT_OUTPUT_BUDGET,
    output_budget_field: str = "max_tokens",
) -> dict[str, Any]:
    """Build a visual-fact-only contract over raw video or fixed frames."""
    raw_video = bundle.input_mode == "raw_video_only"
    roles = _visual_extraction_roles(bundle)
    response_shape = _visual_extraction_response_shape(roles)
    role_text = "、".join(roles)
    if len(roles) == 1:
        role_instruction = (
            f"本次只输入 {roles[0]} 视频，只允许输出 {roles[0]}_evidence_units；"
            "不要输出另一个角色字段，也不要假设存在未输入的另一段视频。"
        )
        extraction_request = f"请只从输入的 {roles[0]} 原始视频中抽取可审计视觉事实。"
    else:
        role_instruction = (
            "本次输入包含两个角色的视频，必须分别归属事实；"
            "不能把一个视频的事实复制到另一个角色。"
        )
        extraction_request = "请从原始视频中分别抽取达人视频和标杆视频的可审计视觉事实。"
    system = (
        "你是 Flayr 的视觉事实抽取器。只输出严格 JSON，不要 Markdown，不要解释。"
        "本实验只测视觉事实抽取能力，不测 severity 判断。"
        f"只允许输出 {role_text} 对应的 evidence_units；每个已输入角色最多 {EXTRACTION_MAX_UNITS} 个证据单元。"
        + role_instruction
        + (
            "只记录原始视频明确支持的事实；看不清或不能确认的内容不要猜，直接省略。"
            if raw_video
            else "只记录固定关键帧明确支持的事实；看不清或不能确认的内容不要猜，直接省略。"
        )
        + "id 必须分别从 C1、C2... 和 B1、B2... 顺序编号；functions 只能使用 S1 到 S6。"
        + f"每条 information 不超过 {EXTRACTION_MAX_INFORMATION_CHARS} 字。"
        + "evidence_strength 只能是 direct、explicit、inferred、absent；"
        + "每条事实必须填写 fact_quality，用 subject、visibility、composition、completion、proof、causal_link 六个字段描述事实质量；"
        + "这些字段只能使用示例中的枚举值，不适用时填 not_applicable。不要输出 severity、confidence、报告或 improvements。"
        + f"严格输出形状示例：{json.dumps(response_shape, ensure_ascii=False, separators=(',', ':'))}"
    )
    request_prefix = extraction_request if raw_video else extraction_request.replace("原始视频", "附图")
    user_text = (
        request_prefix
        + (
            "输入包含原始视频、产品基础信息和阶段目录，不包含任何已有 video_facts.\n\n"
            if raw_video
            else "输入只有固定关键帧、产品基础信息和阶段目录，不包含任何已有 video_facts.\n\n"
        )
        + json.dumps(bundle.context, ensure_ascii=False, indent=2)
    )
    return _build_multimodal_payload(
        model,
        bundle,
        system=system,
        user_text=user_text,
        output_budget=output_budget,
        output_budget_field=output_budget_field,
    )


def _validate_side(
    value: Any,
    *,
    role: str,
    stage_code: str,
    allowed_ids: dict[str, set[str]],
    path: str,
    max_stage_evidence_ids: int = COMPACT_MAX_EVIDENCE_IDS,
) -> list[str]:
    if not isinstance(value, dict):
        return [f"{path} must be an object"]
    expected = {"observation_state", "evidence_ids", "reason"}
    errors: list[str] = []
    extra = set(value) - expected
    missing = expected - set(value)
    if extra:
        errors.append(f"{path} has unsupported fields: {sorted(extra)}")
    if missing:
        errors.append(f"{path} is missing fields: {sorted(missing)}")
    state = value.get("observation_state")
    if state not in COMPACT_STATES:
        errors.append(f"{path}.observation_state is invalid")
    evidence_ids = value.get("evidence_ids")
    if not isinstance(evidence_ids, list) or any(not isinstance(item, str) or not item.strip() for item in evidence_ids):
        errors.append(f"{path}.evidence_ids must be a list of non-empty strings")
        evidence_ids = []
    if len(evidence_ids) > max_stage_evidence_ids:
        errors.append(
            f"{path}.evidence_ids exceeds max_stage_evidence_ids={max_stage_evidence_ids}"
        )
    if len(set(evidence_ids)) != len(evidence_ids):
        errors.append(f"{path}.evidence_ids contains duplicate IDs")
    allowed = allowed_ids.get(role, {}).get(stage_code, set())
    for evidence_id in evidence_ids:
        if evidence_id not in allowed:
            errors.append(f"{path}.evidence_ids contains {evidence_id!r} outside {role}/{stage_code}")
    if state == "none" and evidence_ids:
        errors.append(f"{path}: none state cannot cite evidence")
    if state != "none" and not evidence_ids:
        errors.append(f"{path}: non-none state requires evidence_ids")
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > COMPACT_MAX_REASON_CHARS:
        errors.append(f"{path}.reason must be a non-empty string <= {COMPACT_MAX_REASON_CHARS} chars")
    return errors


def validate_compact_result(
    result: Any,
    bundle: FrozenCompactBundle,
    *,
    max_stage_evidence_ids: int | None = None,
) -> list[str]:
    """Return deterministic contract errors; never repair a compact result."""
    evidence_id_limit = _stage_evidence_id_limit(max_stage_evidence_ids)
    if not isinstance(result, dict):
        return ["result must be an object"]
    errors: list[str] = []
    if result.get("schema_version") != COMPACT_EVAL_SCHEMA_VERSION:
        errors.append("schema_version mismatch")
    expected_root = {"schema_version", "stage_judgments"}
    extra_root = set(result) - expected_root
    if extra_root:
        errors.append(f"unsupported root fields: {sorted(extra_root)}")
    judgments = result.get("stage_judgments")
    expected_stages = [stage.name for stage in DEFAULT_STAGES]
    if not isinstance(judgments, list) or len(judgments) != len(expected_stages):
        return errors + ["stage_judgments must contain exactly six items"]
    for index, (item, expected_stage) in enumerate(zip(judgments, expected_stages)):
        path = f"stage_judgments[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{path} must be an object")
            continue
        expected_fields = {"stage", "severity", "confidence", "creator", "benchmark", "rationale"}
        extra = set(item) - expected_fields
        missing = expected_fields - set(item)
        if extra:
            errors.append(f"{path} has unsupported fields: {sorted(extra)}")
        if missing:
            errors.append(f"{path} is missing fields: {sorted(missing)}")
        if item.get("stage") != expected_stage:
            errors.append(f"{path}.stage must be {expected_stage!r}")
        if item.get("severity") not in COMPACT_SEVERITIES:
            errors.append(f"{path}.severity is invalid")
        if item.get("confidence") not in COMPACT_CONFIDENCES:
            errors.append(f"{path}.confidence is invalid")
        rationale = item.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > COMPACT_MAX_REASON_CHARS:
            errors.append(f"{path}.rationale must be a non-empty string <= {COMPACT_MAX_REASON_CHARS} chars")
        try:
            stage_code = _stage_code(str(item.get("stage") or ""))
        except CompactEvaluationError:
            continue
        errors.extend(
            _validate_side(
                item.get("creator"),
                role="creator",
                stage_code=stage_code,
                allowed_ids=bundle.allowed_evidence_ids,
                path=f"{path}.creator",
                max_stage_evidence_ids=evidence_id_limit,
            )
        )
        errors.extend(
            _validate_side(
                item.get("benchmark"),
                role="benchmark",
                stage_code=stage_code,
                allowed_ids=bundle.allowed_evidence_ids,
                path=f"{path}.benchmark",
                max_stage_evidence_ids=evidence_id_limit,
            )
        )
    return errors


def validate_severity_only_result(result: Any, *, scaffold: bool = False) -> list[str]:
    """Validate a judgment-only result without allowing hidden evidence input."""
    if not isinstance(result, dict):
        return ["result must be an object"]
    errors: list[str] = []
    expected_root = {"schema_version", "stage_judgments"}
    if result.get("schema_version") != COMPACT_EVAL_SCHEMA_VERSION:
        errors.append("schema_version mismatch")
    extra_root = set(result) - expected_root
    if extra_root:
        errors.append(f"unsupported root fields: {sorted(extra_root)}")
    judgments = result.get("stage_judgments")
    expected_stages = [stage.name for stage in DEFAULT_STAGES]
    if not isinstance(judgments, list) or len(judgments) != len(expected_stages):
        return errors + ["stage_judgments must contain exactly six items"]
    expected_fields = {"stage", "severity", "confidence"}
    if scaffold:
        expected_fields.add("decision_basis")
    for index, (item, expected_stage) in enumerate(zip(judgments, expected_stages)):
        path = f"stage_judgments[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{path} must be an object")
            continue
        extra = set(item) - expected_fields
        missing = expected_fields - set(item)
        if extra:
            errors.append(f"{path} has unsupported fields: {sorted(extra)}")
        if missing:
            errors.append(f"{path} is missing fields: {sorted(missing)}")
        if item.get("stage") != expected_stage:
            errors.append(f"{path}.stage must be {expected_stage!r}")
        if item.get("severity") not in COMPACT_SEVERITIES:
            errors.append(f"{path}.severity is invalid")
        if item.get("confidence") not in COMPACT_CONFIDENCES:
            errors.append(f"{path}.confidence is invalid")
        if scaffold:
            basis = item.get("decision_basis")
            if not isinstance(basis, str) or not basis.strip() or len(basis) > COMPACT_MAX_BASIS_CHARS:
                errors.append(
                    f"{path}.decision_basis must be a non-empty string <= {COMPACT_MAX_BASIS_CHARS} chars"
                )
    return errors


def _validate_fact_quality(value: Any, *, path: str) -> list[str]:
    if not isinstance(value, dict):
        return [f"{path} must be an object"]
    expected = set(FACT_QUALITY_FIELDS)
    errors: list[str] = []
    extra = set(value) - expected
    missing = expected - set(value)
    if extra:
        errors.append(f"{path} has unsupported fields: {sorted(extra)}")
    if missing:
        errors.append(f"{path} is missing fields: {sorted(missing)}")
    for field, allowed in FACT_QUALITY_FIELDS.items():
        if field in value and value.get(field) not in allowed:
            errors.append(f"{path}.{field} is invalid")
    return errors


def _validate_extraction_units(
    value: Any,
    *,
    role: str,
    source_duration_seconds: float | None = None,
) -> list[str]:
    if not isinstance(value, list):
        return [f"{role}_evidence_units must be a list"]
    errors: list[str] = []
    if len(value) > EXTRACTION_MAX_UNITS:
        errors.append(f"{role}_evidence_units contains more than {EXTRACTION_MAX_UNITS} units")
    expected_prefix = "C" if role == "creator" else "B"
    expected_fields = {"id", "time_range", "information", "functions", "evidence_strength", "fact_quality"}
    ids: set[str] = set()
    for index, unit in enumerate(value):
        path = f"{role}_evidence_units[{index}]"
        if not isinstance(unit, dict):
            errors.append(f"{path} must be an object")
            continue
        extra = set(unit) - expected_fields
        missing = expected_fields - set(unit)
        if extra:
            errors.append(f"{path} has unsupported fields: {sorted(extra)}")
        if missing:
            errors.append(f"{path} is missing fields: {sorted(missing)}")
        evidence_id = unit.get("id")
        if not isinstance(evidence_id, str) or not re.fullmatch(rf"{expected_prefix}[1-9][0-9]*", evidence_id):
            errors.append(f"{path}.id must match {expected_prefix}N")
        elif evidence_id != f"{expected_prefix}{index + 1}":
            errors.append(f"{path}.id must be sequential starting at {expected_prefix}1")
        elif evidence_id in ids:
            errors.append(f"{path}.id is duplicated")
        else:
            ids.add(evidence_id)
        if parse_time_range_seconds(unit.get("time_range"), source_duration_seconds) is None:
            errors.append(f"{path}.time_range is invalid or outside source duration")
        information = unit.get("information")
        if not isinstance(information, str) or not information.strip() or len(information) > EXTRACTION_MAX_INFORMATION_CHARS:
            errors.append(
                f"{path}.information must be a non-empty string <= {EXTRACTION_MAX_INFORMATION_CHARS} chars"
            )
        functions = unit.get("functions")
        if not isinstance(functions, list) or not functions or any(
            not isinstance(function, str) or not re.fullmatch(r"S[1-6]", function) for function in functions
        ):
            errors.append(f"{path}.functions must contain S1-S6 values")
        if unit.get("evidence_strength") not in EVIDENCE_STATE_STRENGTHS:
            errors.append(f"{path}.evidence_strength is invalid")
        errors.extend(_validate_fact_quality(unit.get("fact_quality"), path=f"{path}.fact_quality"))
    return errors


def validate_visual_extraction_result(
    result: Any,
    *,
    expected_roles: tuple[str, ...] = ("creator", "benchmark"),
    source_durations: dict[str, float] | None = None,
) -> list[str]:
    """Validate visual extraction output; severity is intentionally forbidden."""
    if not isinstance(result, dict):
        return ["result must be an object"]
    roles = tuple(str(role).strip() for role in expected_roles)
    if not roles or any(role not in RAW_VIDEO_ROLES for role in roles) or len(set(roles)) != len(roles):
        return ["expected_roles must contain unique creator and/or benchmark roles"]
    errors: list[str] = []
    expected_root = {"schema_version", *(f"{role}_evidence_units" for role in roles)}
    if result.get("schema_version") != VISUAL_EXTRACTION_SCHEMA_VERSION:
        errors.append("schema_version mismatch")
    extra_root = set(result) - expected_root
    if extra_root:
        errors.append(f"unsupported root fields: {sorted(extra_root)}")
    source_durations = source_durations or {}
    for role in roles:
        errors.extend(
            _validate_extraction_units(
                result.get(f"{role}_evidence_units"),
                role=role,
                source_duration_seconds=source_durations.get(role),
            )
        )
    return errors


def build_model_owned_fact_bundle(
    base_bundle: FrozenCompactBundle,
    extraction_result: dict[str, Any],
    *,
    extraction_artifact: str = "",
) -> FrozenCompactBundle:
    """Create a judgment-only bundle from one model's validated extraction.

    This is the second layer of the frozen human/model protocol. The model
    receives its own already-produced visual facts as locked input, so the
    judgment call cannot silently replace extraction and judgment with one
    opaque answer. Human GT is deliberately absent from this bundle.
    """
    if not isinstance(extraction_result, dict):
        raise CompactEvaluationError("model-owned extraction result must be an object")
    source_durations = _bundle_video_durations(base_bundle)
    extraction_errors = validate_visual_extraction_result(
        extraction_result,
        expected_roles=RAW_VIDEO_ROLES,
        source_durations=source_durations,
    )
    if extraction_errors:
        raise CompactEvaluationError(
            "model-owned extraction is not valid: " + "; ".join(extraction_errors[:8])
        )

    facts: dict[str, dict[str, Any]] = {}
    for role in RAW_VIDEO_ROLES:
        units = extraction_result.get(f"{role}_evidence_units")
        facts[role] = {
            "content_summary": "",
            "communication_strategy": "",
            "evidence_units": [_compact_fact_unit(item) for item in units if isinstance(item, dict)],
        }
    allowed = {role: _allowed_evidence_ids(facts[role]) for role in facts}
    context = dict(base_bundle.context)
    context["facts"] = facts
    context["experiment_boundary"] = (
        "这是模型独立判断层实验。输入事实来自同一模型此前完成的 raw_video_only 抽取，"
        "事实包已经锁定；本次只判断整体比较和六阶段差距，不读取人工初始判断，不输出生产报告或 derive 结果。"
    )
    context["model_owned_fact_provenance"] = {
        "source_artifact": extraction_artifact,
        "source_digest": base_bundle.source_digest,
        "human_initial_loaded": False,
        "gt_loaded": False,
    }
    source_identity = {
        "base_source_digest": base_bundle.source_digest,
        "input_mode": "model_owned_locked_facts",
        "facts": facts,
        "extraction_artifact": extraction_artifact,
    }
    return replace(
        base_bundle,
        context=context,
        allowed_evidence_ids=allowed,
        source_digest=_stable_digest(source_identity),
        input_mode="model_owned_locked_facts",
        visual_inputs=(),
        video_inputs=(),
    )


def _normalise_extraction_text(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _extraction_unit_signature(unit: dict[str, Any]) -> tuple[Any, ...]:
    parsed = parse_time_range_seconds(unit.get("time_range"), None)
    time_signature: tuple[Any, ...]
    if parsed is None:
        time_signature = (str(unit.get("time_range") or "").strip(),)
    else:
        time_signature = tuple(round(float(value), 3) for value in parsed)
    functions = tuple(sorted(str(value).strip().upper() for value in unit.get("functions", []) if str(value).strip()))
    return (_normalise_extraction_text(unit.get("information")), time_signature, functions)


def _extraction_information_signature(unit: dict[str, Any]) -> str:
    return _normalise_extraction_text(unit.get("information"))


def compare_visual_extraction_units(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare two extracted unit lists without treating generated IDs as content."""
    left_signatures = {_extraction_unit_signature(item) for item in left if isinstance(item, dict)}
    right_signatures = {_extraction_unit_signature(item) for item in right if isinstance(item, dict)}
    left_information = {_extraction_information_signature(item) for item in left if isinstance(item, dict)}
    right_information = {_extraction_information_signature(item) for item in right if isinstance(item, dict)}
    exact_intersection = left_signatures & right_signatures
    information_intersection = left_information & right_information
    signature_union = left_signatures | right_signatures
    information_union = left_information | right_information
    temporal_stage_matches = 0
    used_right_indexes: set[int] = set()
    candidates: list[tuple[float, int, int]] = []
    for left_index, left_item in enumerate(left):
        if not isinstance(left_item, dict):
            continue
        left_range = parse_time_range_seconds(left_item.get("time_range"), None)
        left_functions = {
            str(value).strip().upper()
            for value in left_item.get("functions", [])
            if isinstance(value, str)
        }
        if left_range is None:
            continue
        for right_index, right_item in enumerate(right):
            if not isinstance(right_item, dict):
                continue
            right_range = parse_time_range_seconds(right_item.get("time_range"), None)
            if right_range is None:
                continue
            right_functions = {
                str(value).strip().upper()
                for value in right_item.get("functions", [])
                if isinstance(value, str)
            }
            if not left_functions.intersection(right_functions):
                continue
            overlap = max(0.0, min(left_range[1], right_range[1]) - max(left_range[0], right_range[0]))
            shortest = min(left_range[1] - left_range[0], right_range[1] - right_range[0])
            if overlap > 0 and shortest > 0:
                candidates.append((overlap / shortest, left_index, right_index))
    for score, _, right_index in sorted(candidates, reverse=True):
        if score < 0.5 or right_index in used_right_indexes:
            continue
        used_right_indexes.add(right_index)
        temporal_stage_matches += 1
    return {
        "left_count": len(left),
        "right_count": len(right),
        "exact_signature_matches": len(exact_intersection),
        "exact_signature_jaccard": (
            len(exact_intersection) / len(signature_union) if signature_union else 1.0
        ),
        "information_matches": len(information_intersection),
        "information_jaccard": (
            len(information_intersection) / len(information_union) if information_union else 1.0
        ),
        "temporal_stage_matches": temporal_stage_matches,
        "temporal_stage_match_rate": (
            temporal_stage_matches / min(len(left), len(right)) if min(len(left), len(right)) else 0.0
        ),
    }


def _source_video_durations(run_dir: Path) -> dict[str, float]:
    analysis = _read_json(run_dir / "analysis.json", required=False)
    videos = analysis.get("videos")
    if not isinstance(videos, dict):
        return {}
    durations: dict[str, float] = {}
    for role in RAW_VIDEO_ROLES:
        value = videos.get(role, {}).get("duration_seconds") if isinstance(videos.get(role), dict) else None
        try:
            duration = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration) and duration > 0:
            durations[role] = duration
    return durations


def _bundle_video_durations(bundle: FrozenCompactBundle) -> dict[str, float]:
    durations: dict[str, float] = {}
    for item in bundle.video_inputs:
        role = str(item.get("role") or "").strip()
        try:
            duration = float(item.get("duration_seconds"))
        except (TypeError, ValueError):
            continue
        if role in RAW_VIDEO_ROLES and math.isfinite(duration) and duration > 0:
            durations[role] = duration
    return durations


def normalize_visual_extraction_result(
    result: dict[str, Any],
    bundle: FrozenCompactBundle,
) -> dict[str, Any]:
    """Build a comparison view without changing the model-produced result."""
    roles = _visual_extraction_roles(bundle)
    durations = _source_video_durations(bundle.run_dir)
    durations.update({role: value for role, value in _bundle_video_durations(bundle).items() if role not in durations})
    by_role: dict[str, list[dict[str, Any]]] = {}
    for role in roles:
        normalized_units: list[dict[str, Any]] = []
        units = result.get(f"{role}_evidence_units", [])
        if not isinstance(units, list):
            units = []
        for unit in units:
            if not isinstance(unit, dict):
                continue
            parsed = parse_time_range_seconds(unit.get("time_range"), durations.get(role))
            normalized_units.append(
                {
                    "id": unit.get("id"),
                    "time_range": unit.get("time_range"),
                    "normalized_start_seconds": round(parsed[0], 3) if parsed is not None else None,
                    "normalized_end_seconds": round(parsed[1], 3) if parsed is not None else None,
                    "information": unit.get("information"),
                    "functions": unit.get("functions"),
                    "evidence_strength": unit.get("evidence_strength"),
                    "fact_quality": unit.get("fact_quality"),
                }
            )
        by_role[role] = normalized_units
    return {
        "schema_version": VISUAL_EXTRACTION_SCHEMA_VERSION,
        "source_duration_seconds": durations,
        "by_role": by_role,
    }


def summarize_visual_extraction_result(
    result: dict[str, Any],
    bundle: FrozenCompactBundle,
) -> dict[str, Any]:
    """Produce offline coverage and duplication metrics for one valid result."""
    roles = _visual_extraction_roles(bundle)
    durations = _source_video_durations(bundle.run_dir)
    durations.update({role: value for role, value in _bundle_video_durations(bundle).items() if role not in durations})
    by_role: dict[str, Any] = {}
    for role in roles:
        units = result.get(f"{role}_evidence_units", [])
        if not isinstance(units, list):
            units = []
        ranges = [
            parse_time_range_seconds(item.get("time_range"), None)
            for item in units
            if isinstance(item, dict)
        ]
        ranges = [item for item in ranges if item is not None]
        first_start = min((item[0] for item in ranges), default=None)
        last_end = max((item[1] for item in ranges), default=None)
        duration = durations.get(role)
        by_role[role] = {
            "unit_count": len(units),
            "max_units_hit": len(units) >= EXTRACTION_MAX_UNITS,
            "first_start_seconds": first_start,
            "last_end_seconds": last_end,
            "source_duration_seconds": duration,
            "upper_bound_coverage_ratio": (
                last_end / duration if last_end is not None and duration and duration > 0 else None
            ),
            "stage_functions": sorted(
                {
                    function
                    for item in units
                    if isinstance(item, dict)
                    for function in item.get("functions", [])
                    if isinstance(function, str) and re.fullmatch(r"S[1-6]", function)
                }
            ),
            "missing_stage_functions": sorted(
                set(f"S{index}" for index in range(1, 7))
                - {
                    function
                    for item in units
                    if isinstance(item, dict)
                    for function in item.get("functions", [])
                    if isinstance(function, str) and re.fullmatch(r"S[1-6]", function)
                }
            ),
            "s6_present": any(
                isinstance(item, dict)
                and any(function == "S6" for function in item.get("functions", []))
                for item in units
            ),
            "evidence_strength_counts": {
                strength: sum(item.get("evidence_strength") == strength for item in units if isinstance(item, dict))
                for strength in ("direct", "explicit", "inferred", "absent")
            },
            "fact_quality_coverage": (
                sum(isinstance(item.get("fact_quality"), dict) for item in units if isinstance(item, dict))
                / len(units)
                if units
                else 0.0
            ),
            "fact_quality_by_stage": {},
        }
        for stage_code in ("S1", "S2", "S3", "S4", "S5", "S6"):
            stage_units = [
                item
                for item in units
                if isinstance(item, dict)
                and any(str(function).upper() == stage_code for function in item.get("functions", []))
            ]
            quality_summary: dict[str, dict[str, int]] = {}
            for field_name in FACT_QUALITY_FIELDS:
                quality_summary[field_name] = {
                    value: sum(
                        isinstance(item.get("fact_quality"), dict)
                        and item["fact_quality"].get(field_name) == value
                        for item in stage_units
                    )
                    for value in sorted(FACT_QUALITY_FIELDS[field_name])
                }
            by_role[role]["fact_quality_by_stage"][stage_code] = quality_summary
    summary: dict[str, Any] = {
        "video_role_order": list(roles),
        "by_role": by_role,
    }
    if set(roles) == set(RAW_VIDEO_ROLES):
        creator_units = result.get("creator_evidence_units", [])
        benchmark_units = result.get("benchmark_evidence_units", [])
        duplicate = compare_visual_extraction_units(creator_units, benchmark_units)
        denominator = min(len(creator_units), len(benchmark_units))
        summary["cross_role_duplicate_rate"] = (
            duplicate["exact_signature_matches"] / denominator if denominator else 0.0
        )
        summary["cross_role_information_duplicate_rate"] = (
            duplicate["information_matches"] / denominator if denominator else 0.0
        )
        summary["cross_role_duplicate_comparison"] = duplicate
    return summary


def _range_relation(unit_range: Any, stage_range: Any) -> tuple[str, float | None]:
    parsed_unit = parse_time_range_seconds(unit_range, None)
    parsed_stage = parse_time_range_seconds(stage_range, None)
    if parsed_unit is None or parsed_stage is None:
        return "unknown", None
    unit_start, unit_end = parsed_unit
    stage_start, stage_end = parsed_stage
    if min(unit_end, stage_end) > max(unit_start, stage_start):
        return "overlap", 0.0
    if math.isclose(unit_end, stage_start) or math.isclose(stage_end, unit_start):
        return "touching_boundary", 0.0
    if unit_end < stage_start:
        return "before", stage_start - unit_end
    if stage_end < unit_start:
        return "after", unit_start - stage_end
    return "unknown", None


def diagnose_compact_evidence_references(
    result: dict[str, Any],
    bundle: FrozenCompactBundle,
) -> dict[str, Any]:
    """Explain evidence-reference errors without changing a model result.

    The temporal decision uses the production ``evidence_overlaps_range``
    primitive. The extra relation/distance fields only classify the diagnostic
    output, so a boundary touch is distinguishable from a distant mismatch.
    """
    facts_by_role = {
        role: {
            str(unit.get("id")): unit
            for unit in bundle.context.get("facts", {}).get(role, {}).get("evidence_units", [])
            if isinstance(unit, dict) and str(unit.get("id") or "").strip()
        }
        for role in ("creator", "benchmark")
    }
    checks: list[dict[str, Any]] = []
    for item in result.get("stage_judgments", []) if isinstance(result, dict) else []:
        if not isinstance(item, dict):
            continue
        try:
            stage_code = _stage_code(str(item.get("stage") or ""))
        except CompactEvaluationError:
            continue
        for role in ("creator", "benchmark"):
            side = item.get(role)
            references = side.get("evidence_ids", []) if isinstance(side, dict) else []
            if not isinstance(references, list):
                continue
            stage_range = bundle.stage_time_ranges.get(role, {}).get(stage_code)
            for raw_id in references:
                evidence_id = str(raw_id).strip()
                unit = facts_by_role[role].get(evidence_id)
                flags: list[str] = []
                record: dict[str, Any] = {
                    "role": role,
                    "stage": stage_code,
                    "evidence_id": evidence_id,
                    "stage_time_range": stage_range,
                }
                if unit is None:
                    flags.append("unknown_evidence_id")
                    record.update({"status": "incomplete", "diagnostic_flags": flags})
                    checks.append(record)
                    continue
                unit_range = unit.get("time_range")
                function_tokens = {
                    str(function).strip().upper().split("_", 1)[0]
                    for function in unit.get("functions", [])
                    if isinstance(function, str)
                }
                function_match = stage_code in function_tokens if function_tokens else None
                overlap = None
                if stage_range is not None:
                    overlap = evidence_overlaps_range(unit, stage_range)
                    if overlap is False:
                        flags.append("evidence_temporal_mismatch")
                if function_match is False:
                    flags.append("stage_function_mismatch")
                relation, distance = _range_relation(unit_range, stage_range)
                record.update(
                    {
                        "evidence_time_range": unit_range,
                        "temporal_overlap": overlap,
                        "range_relation": relation,
                        "distance_seconds": distance,
                        "function_stage_match": function_match,
                        "diagnostic_flags": flags,
                        "status": "diagnostic_mismatch" if flags else "consistent",
                    }
                )
                checks.append(record)
    summary = {
        "total_references": len(checks),
        "consistent_references": sum(item["status"] == "consistent" for item in checks),
        "unknown_evidence_ids": sum("unknown_evidence_id" in item.get("diagnostic_flags", []) for item in checks),
        "temporal_mismatches": sum(
            "evidence_temporal_mismatch" in item.get("diagnostic_flags", []) for item in checks
        ),
        "function_stage_mismatches": sum(
            "stage_function_mismatch" in item.get("diagnostic_flags", []) for item in checks
        ),
        "touching_boundaries": sum(item.get("range_relation") == "touching_boundary" for item in checks),
    }
    return {"schema_version": 1, "summary": summary, "checks": checks}


def load_gt_stages(gt_path: Path, sample_id: str) -> dict[str, str]:
    """Load legacy severity-only labels for the existing compact runner."""
    labels = load_gt_stage_labels(gt_path, sample_id)
    return {
        stage: str(label.get("gap_magnitude"))
        for stage, label in labels.items()
        if label.get("gap_magnitude") in COMPACT_SEVERITIES
    }


def load_gt_stage_labels(gt_path: Path, sample_id: str) -> dict[str, dict[str, Any]]:
    """Load direction-aware labels without treating missing/NA as small.

    The old ``stages`` map remains supported. A frozen human-initial artifact
    may additionally provide ``human_gap`` and ``stage_relations``. The
    evaluator keeps all three states explicit so ``none`` cannot silently turn
    into a scored ``small`` label.
    """
    data = _read_json(gt_path)
    sample = data.get("samples", {}).get(sample_id) if isinstance(data.get("samples"), dict) else None
    stages = sample.get("stages") if isinstance(sample, dict) else None
    human_gap = sample.get("human_gap") if isinstance(sample, dict) else None
    if not isinstance(stages, dict) and not isinstance(human_gap, dict):
        raise CompactEvaluationError(f"GT sample has no stage labels: {sample_id}")
    gap_values = human_gap if isinstance(human_gap, dict) else stages
    relations = sample.get("stage_relations") if isinstance(sample, dict) else None
    if not isinstance(relations, dict) and isinstance(sample, dict):
        relations = sample.get("relations")
    relations = relations if isinstance(relations, dict) else {}
    statuses = sample.get("stage_label_statuses") if isinstance(sample, dict) else None
    statuses = statuses if isinstance(statuses, dict) else {}
    labels: dict[str, dict[str, Any]] = {}
    for stage_code in ("S1", "S2", "S3", "S4", "S5", "S6"):
        raw_gap = gap_values.get(stage_code) if isinstance(gap_values, dict) else None
        gap = str(raw_gap or "").strip().lower()
        status_info = statuses.get(stage_code) if isinstance(statuses.get(stage_code), dict) else {}
        status = str(status_info.get("status") or "").strip().lower()
        if gap in {"na", "not_applicable"} or status == "not_applicable":
            status = "not_applicable"
        elif gap in {"uncertain", "unknown"} or status == "uncertain":
            status = "uncertain"
        elif gap in {"none", "small", "medium", "large"}:
            status = "labeled"
        elif gap:
            status = "invalid"
        else:
            status = "missing"
        relation = str(relations.get(stage_code) or "").strip().lower() or None
        labels[stage_code] = {
            "gap_magnitude": gap or None,
            "relation": relation,
            "status": status,
            "reason": str(status_info.get("reason") or ""),
        }
    return labels


def score_compact_result(result: dict[str, Any], gt_stages: dict[str, str]) -> dict[str, Any]:
    rows = []
    excluded = {"not_applicable": 0, "uncertain": 0, "missing": 0, "invalid": 0}
    for item in result.get("stage_judgments", []):
        code = _stage_code(str(item.get("stage") or ""))
        gt = gt_stages.get(code)
        if gt is None:
            excluded["missing"] += 1
            continue
        prediction = item.get("severity")
        rows.append({"stage": code, "prediction": prediction, "gt": gt, "correct": prediction == gt})
    correct = sum(1 for row in rows if row["correct"])
    return {
        "labeled_stages": len(rows),
        "correct_stages": correct,
        "accuracy": correct / len(rows) if rows else None,
        "denominator": {
            "eligible": len(rows),
            "excluded_not_applicable": None,
            "excluded_uncertain": None,
            "excluded_missing_or_unlabeled": None,
            "excluded_invalid": None,
            "excluded_unrepresented_gt_stages": excluded["missing"],
            "exclusion_metadata_available": False,
            "basis": "legacy severity-only labels; excluded GT states are unavailable and must be supplied by the direction-aware evaluator",
        },
        "rows": rows,
    }


def _run_isolated_evaluation(
    *,
    model: str,
    bundle: FrozenCompactBundle,
    output_dir: Path,
    api_url: str,
    api_key_args: Any,
    payload: dict[str, Any],
    validator: Any,
    task_role: str,
    evaluation_role: str,
    variant: str,
    success_filename: str,
    failure_filename: str,
    call_kind: str,
    output_budget: int,
    output_budget_field: str,
    request_timeout_seconds: int,
    gt_stages: dict[str, str] | None = None,
    diagnostics: Any = None,
    max_stage_evidence_ids: int | None = None,
    experiment_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if evaluation_role not in EVALUATION_ROLES:
        raise CompactEvaluationError(f"unsupported evaluation_role: {evaluation_role}")
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale_name in (
        "compact_evaluation.json",
        "compact_failure.json",
        "severity_only_evaluation.json",
        "severity_only_failure.json",
        "severity_scaffold_evaluation.json",
        "severity_scaffold_failure.json",
        "visual_extraction_evaluation.json",
        "visual_extraction_failure.json",
        "s4_fact_state_evaluation.json",
        "s4_fact_state_failure.json",
        "s4_judgment_evaluation.json",
        "s4_judgment_failure.json",
        "s4_single_pass_evaluation.json",
        "s4_single_pass_failure.json",
        "s4_free_text_steps_evaluation.json",
        "s4_free_text_steps_failure.json",
        "s5_audit_evaluation.json",
        "s5_audit_failure.json",
        "raw_model_response.json",
        ".compact-request.json",
    ):
        (output_dir / stale_name).unlink(missing_ok=True)
    decision_scope = DECISION_SCOPE_BY_ROLE[evaluation_role]
    contract_limits = contract_limits_for_variant(variant)
    if max_stage_evidence_ids is not None and variant in {"evidence_grounded", "model_independent"}:
        contract_limits["max_stage_evidence_ids"] = _stage_evidence_id_limit(max_stage_evidence_ids)
    metadata = {
        "evaluation_role": evaluation_role,
        "decision_scope": decision_scope,
        "promotion_eligible": False,
        "promotion_note": "isolated model evaluation is not a production-model selection decision",
        "task_role": task_role,
        "variant": variant,
        "schema_version": (
            MODEL_INDEPENDENT_SCHEMA_VERSION
            if variant == "model_independent"
            else VISUAL_EXTRACTION_SCHEMA_VERSION
            if variant in {"visual_extraction", "visual_extraction_on_raw_video"}
            else S4_FACT_STATE_SCHEMA_VERSION
            if variant == "s4_fact_state"
            else S4_JUDGMENT_SCHEMA_VERSION
            if variant == "s4_judgment"
            else S4_SINGLE_PASS_SCHEMA_VERSION
            if variant == "s4_single_pass"
            else S4_FREE_TEXT_STEPS_SCHEMA_VERSION
            if variant == "s4_free_text_steps"
            else S5_AUDIT_SCHEMA_VERSION
            if variant == "s5_audit"
            else COMPACT_EVAL_SCHEMA_VERSION
        ),
        "model": model,
        "source_commit": current_code_commit(),
        "source_run": bundle.run_dir.name,
        "source_digest": bundle.source_digest,
        "input_mode": bundle.input_mode,
        "output_budget_field": output_budget_field,
        "output_budget": output_budget,
        "contract_limits": {**contract_limits, "output_budget": output_budget},
        "request_retry_policy": {"outer_attempts": 1, "transport_retries": 0},
        "request_timeout_seconds": request_timeout_seconds,
        "image_count": len(bundle.visual_inputs),
        "image_labels": [item["label"] for item in bundle.visual_inputs],
        "video_count": len(bundle.video_inputs),
        "video_labels": [item["label"] for item in bundle.video_inputs],
        "video_role_order": [item["role"] for item in bundle.video_inputs],
        "video_source_sha256": [item["sha256"] for item in bundle.video_inputs],
        "video_data_url_sha256": [item["data_url_sha256"] for item in bundle.video_inputs],
        "video_source_duration_seconds": [item["duration_seconds"] for item in bundle.video_inputs],
    }
    if experiment_metadata:
        metadata["experiment"] = dict(experiment_metadata)
    metadata["protocol_hash"] = _stable_digest(
        {
            "task_role": task_role,
            "variant": variant,
            "schema_version": metadata["schema_version"],
            "contract_limits": metadata["contract_limits"],
            "system_prompt": payload.get("messages", [{}])[0].get("content"),
        }
    )
    write_json(output_dir / "compact_request_metadata.json", metadata)
    api_key = read_llm_api_key(api_key_args)
    if not api_key:
        raise CompactEvaluationError("LLM API key is unavailable")
    limits = ResourceLimits(
        max_total_wall_time=min(max(float(request_timeout_seconds) + 30.0, 60.0), 1800.0),
        max_llm_calls=1,
        max_total_uploaded_bytes=64 * 1024 * 1024,
        max_download_bytes=32 * 1024 * 1024,
        max_cost_estimate=1.0,
    )
    budget = ResourceBudget(limits)
    token = budget.activate()
    raw_path = output_dir / "raw_model_response.json"
    response_meta: dict[str, Any] = {}
    try:
        payload_path = output_dir / ".compact-request.json"
        write_json(payload_path, payload)
        raw_text = call_llm_api(
            api_url,
            api_key,
            payload_path,
            raw_path,
            max_time_seconds=request_timeout_seconds,
            low_speed_time_seconds=min(180, max(30, request_timeout_seconds)),
            retries=0,
            budget=budget,
            call_kind=call_kind,
            cleanup_raw=False,
            response_meta=response_meta,
        )
        response = json.loads(raw_text)
        content = extract_chat_completion_text(response)
        parsed = parse_json_text(content)
        errors = validator(parsed)
        if errors:
            failure = {
                "status": "contract_failed",
                **metadata,
                "errors": errors,
                "failure_class": "contract_validation",
                "contract_error_codes": _contract_error_codes(errors),
                "candidate_result": parsed,
                "provider_meta": response_meta,
                "resource_budget": budget.snapshot(),
            }
            if diagnostics is not None:
                failure["evidence_diagnostics"] = diagnostics(parsed, bundle)
            write_json(output_dir / failure_filename, failure)
            return failure
        result: dict[str, Any] = {
            "status": "completed",
            **metadata,
            "result": parsed,
            "provider_meta": response_meta,
            "resource_budget": budget.snapshot(),
        }
        if variant == "visual_extraction_on_raw_video":
            result["normalized_evidence_units"] = normalize_visual_extraction_result(parsed, bundle)
        if gt_stages is not None:
            result["gt_score"] = score_compact_result(parsed, gt_stages)
        if diagnostics is not None:
            result["evidence_diagnostics"] = diagnostics(parsed, bundle)
        write_json(output_dir / success_filename, result)
        return result
    except ResourceBudgetExceeded as exc:
        failure = {
            "status": "request_failed",
            **metadata,
            "failure_class": "resource_limit",
            "error": str(exc)[:1000],
            "provider_meta": response_meta,
            "resource_budget": budget.snapshot(),
        }
        write_json(output_dir / failure_filename, failure)
        return failure
    except json.JSONDecodeError as exc:
        failure = {
            "status": "request_failed",
            **metadata,
            "failure_class": "response_parse",
            "error": str(exc)[:1000],
            "provider_meta": response_meta,
            "resource_budget": budget.snapshot(),
        }
        write_json(output_dir / failure_filename, failure)
        return failure
    except SystemExit as exc:
        failure = {
            "status": "request_failed",
            **metadata,
            "failure_class": "provider_or_transport",
            "error": str(exc)[:1000],
            "provider_meta": response_meta,
            "resource_budget": budget.snapshot(),
        }
        write_json(output_dir / failure_filename, failure)
        return failure
    except CompactEvaluationError as exc:
        failure = {
            "status": "request_failed",
            **metadata,
            "failure_class": "input_or_contract_setup",
            "error": str(exc)[:1000],
            "provider_meta": response_meta,
            "resource_budget": budget.snapshot(),
        }
        write_json(output_dir / failure_filename, failure)
        return failure
    except OSError as exc:
        failure = {
            "status": "request_failed",
            **metadata,
            "failure_class": "io_or_transport",
            "error": str(exc)[:1000],
            "provider_meta": response_meta,
            "resource_budget": budget.snapshot(),
        }
        write_json(output_dir / failure_filename, failure)
        return failure
    finally:
        budget.deactivate(token)


def run_compact_evaluation(
    *,
    model: str,
    bundle: FrozenCompactBundle,
    output_dir: Path,
    api_url: str,
    api_key_args: Any,
    output_budget: int = COMPACT_OUTPUT_BUDGET,
    output_budget_field: str = "max_tokens",
    request_timeout_seconds: int = 600,
    gt_stages: dict[str, str] | None = None,
    evaluation_role: str = "model_calibration",
    max_stage_evidence_ids: int | None = None,
) -> dict[str, Any]:
    """Run the original evidence-grounded compact contract in isolation."""
    payload = build_compact_eval_payload(
        model,
        bundle,
        output_budget=output_budget,
        output_budget_field=output_budget_field,
        max_stage_evidence_ids=max_stage_evidence_ids,
    )
    return _run_isolated_evaluation(
        model=model,
        bundle=bundle,
        output_dir=output_dir,
        api_url=api_url,
        api_key_args=api_key_args,
        payload=payload,
        validator=lambda value: validate_compact_result(
            value,
            bundle,
            max_stage_evidence_ids=max_stage_evidence_ids,
        ),
        task_role=COMPACT_EVAL_ROLE,
        evaluation_role=evaluation_role,
        variant="evidence_grounded",
        success_filename="compact_evaluation.json",
        failure_filename="compact_failure.json",
        call_kind="compact_eval",
        output_budget=output_budget,
        output_budget_field=output_budget_field,
        request_timeout_seconds=request_timeout_seconds,
        gt_stages=gt_stages,
        diagnostics=diagnose_compact_evidence_references,
        max_stage_evidence_ids=max_stage_evidence_ids,
        experiment_metadata=(
            {
                "name": "max_stage_evidence_ids",
                "baseline": COMPACT_MAX_EVIDENCE_IDS,
                "candidate": _stage_evidence_id_limit(max_stage_evidence_ids),
                "single_variable": True,
                "candidate_is_diagnostic_only": True,
                "production_default_unchanged": True,
            }
            if max_stage_evidence_ids is not None
            else None
        ),
    )


def run_model_independent_evaluation(
    *,
    model: str,
    bundle: FrozenCompactBundle,
    output_dir: Path,
    api_url: str,
    api_key_args: Any,
    output_budget: int = COMPACT_OUTPUT_BUDGET,
    output_budget_field: str = "max_tokens",
    request_timeout_seconds: int = 600,
    evaluation_role: str = "model_calibration",
    max_stage_evidence_ids: int | None = None,
) -> dict[str, Any]:
    """Run the frozen protocol's model-independent judgment layer."""
    payload = build_model_independent_payload(
        model,
        bundle,
        output_budget=output_budget,
        output_budget_field=output_budget_field,
        max_stage_evidence_ids=max_stage_evidence_ids,
    )
    return _run_isolated_evaluation(
        model=model,
        bundle=bundle,
        output_dir=output_dir,
        api_url=api_url,
        api_key_args=api_key_args,
        payload=payload,
        validator=lambda value: validate_model_independent_result(
            value,
            bundle,
            max_stage_evidence_ids=max_stage_evidence_ids,
        ),
        task_role=MODEL_INDEPENDENT_ROLE,
        evaluation_role=evaluation_role,
        variant="model_independent",
        success_filename="model_independent_evaluation.json",
        failure_filename="model_independent_failure.json",
        call_kind="model_independent_judgment",
        output_budget=output_budget,
        output_budget_field=output_budget_field,
        request_timeout_seconds=request_timeout_seconds,
        gt_stages=None,
        diagnostics=diagnose_compact_evidence_references,
        max_stage_evidence_ids=max_stage_evidence_ids,
        experiment_metadata=(
            {
                "name": "max_stage_evidence_ids",
                "baseline": COMPACT_MAX_EVIDENCE_IDS,
                "candidate": _stage_evidence_id_limit(max_stage_evidence_ids),
                "single_variable": True,
                "candidate_is_diagnostic_only": True,
                "production_default_unchanged": True,
            }
            if max_stage_evidence_ids is not None
            else None
        ),
    )


def run_severity_only_evaluation(
    *,
    model: str,
    bundle: FrozenCompactBundle,
    output_dir: Path,
    api_url: str,
    api_key_args: Any,
    scaffold: bool = False,
    output_budget: int = COMPACT_OUTPUT_BUDGET,
    output_budget_field: str = "max_tokens",
    request_timeout_seconds: int = 600,
    gt_stages: dict[str, str] | None = None,
    evaluation_role: str = "model_calibration",
) -> dict[str, Any]:
    """Run severity judgment on one shared, validated fact package."""
    payload = build_severity_only_payload(
        model,
        bundle,
        scaffold=scaffold,
        output_budget=output_budget,
        output_budget_field=output_budget_field,
    )
    return _run_isolated_evaluation(
        model=model,
        bundle=bundle,
        output_dir=output_dir,
        api_url=api_url,
        api_key_args=api_key_args,
        payload=payload,
        validator=lambda value: validate_severity_only_result(value, scaffold=scaffold),
        task_role=SEVERITY_ONLY_ROLE,
        evaluation_role=evaluation_role,
        variant="severity_only_scaffold" if scaffold else "severity_only",
        success_filename="severity_scaffold_evaluation.json" if scaffold else "severity_only_evaluation.json",
        failure_filename="severity_scaffold_failure.json" if scaffold else "severity_only_failure.json",
        call_kind="compact_severity_eval",
        output_budget=output_budget,
        output_budget_field=output_budget_field,
        request_timeout_seconds=request_timeout_seconds,
        gt_stages=gt_stages,
    )


def run_visual_extraction_evaluation(
    *,
    model: str,
    bundle: FrozenCompactBundle,
    output_dir: Path,
    api_url: str,
    api_key_args: Any,
    output_budget: int = COMPACT_OUTPUT_BUDGET,
    output_budget_field: str = "max_tokens",
    request_timeout_seconds: int = 600,
    evaluation_role: str = "model_calibration",
) -> dict[str, Any]:
    """Run visual fact extraction over fixed frames, without severity judgment."""
    if bundle.input_mode != "raw_video_only":
        raise CompactEvaluationError("visual extraction requires a raw_video_only bundle")
    roles = _visual_extraction_roles(bundle)
    source_durations = _bundle_video_durations(bundle)
    if any(role not in source_durations for role in roles):
        raise CompactEvaluationError("visual extraction requires a finite source duration for every video role")
    payload = build_visual_extraction_payload(
        model,
        bundle,
        output_budget=output_budget,
        output_budget_field=output_budget_field,
    )
    return _run_isolated_evaluation(
        model=model,
        bundle=bundle,
        output_dir=output_dir,
        api_url=api_url,
        api_key_args=api_key_args,
        payload=payload,
        validator=lambda value: validate_visual_extraction_result(
            value,
            expected_roles=roles,
            source_durations=source_durations,
        ),
        task_role=VISUAL_EXTRACTION_ROLE,
        evaluation_role=evaluation_role,
        variant="visual_extraction_on_raw_video",
        success_filename="visual_extraction_evaluation.json",
        failure_filename="visual_extraction_failure.json",
        call_kind="compact_extraction_eval",
        output_budget=output_budget,
        output_budget_field=output_budget_field,
        request_timeout_seconds=request_timeout_seconds,
    )


def run_s4_single_pass_evaluation(
    *,
    model: str,
    bundle: FrozenCompactBundle,
    output_dir: Path,
    api_url: str,
    api_key_args: Any,
    output_budget: int = COMPACT_OUTPUT_BUDGET,
    output_budget_field: str = "max_tokens",
    request_timeout_seconds: int = 600,
    evaluation_role: str = "model_calibration",
) -> dict[str, Any]:
    """Run the one-call structured S4 control against immutable facts."""

    payload = build_s4_single_pass_payload(
        model,
        bundle,
        output_budget=output_budget,
        output_budget_field=output_budget_field,
    )
    return _run_isolated_evaluation(
        model=model,
        bundle=bundle,
        output_dir=output_dir,
        api_url=api_url,
        api_key_args=api_key_args,
        payload=payload,
        validator=lambda value: validate_s4_single_pass_result(value, bundle),
        task_role=S4_SINGLE_PASS_ROLE,
        evaluation_role=evaluation_role,
        variant="s4_single_pass",
        success_filename="s4_single_pass_evaluation.json",
        failure_filename="s4_single_pass_failure.json",
        call_kind="s4_single_pass_eval",
        output_budget=output_budget,
        output_budget_field=output_budget_field,
        request_timeout_seconds=request_timeout_seconds,
    )


def run_s4_free_text_steps_evaluation(
    *,
    model: str,
    bundle: FrozenCompactBundle,
    output_dir: Path,
    api_url: str,
    api_key_args: Any,
    output_budget: int = COMPACT_OUTPUT_BUDGET,
    output_budget_field: str = "max_tokens",
    request_timeout_seconds: int = 600,
    evaluation_role: str = "model_calibration",
) -> dict[str, Any]:
    """Run the one-call five-step free-text S4 control on immutable facts."""

    payload = build_s4_free_text_steps_payload(
        model,
        bundle,
        output_budget=output_budget,
        output_budget_field=output_budget_field,
    )
    return _run_isolated_evaluation(
        model=model,
        bundle=bundle,
        output_dir=output_dir,
        api_url=api_url,
        api_key_args=api_key_args,
        payload=payload,
        validator=validate_s4_free_text_steps_result,
        task_role=S4_FREE_TEXT_STEPS_ROLE,
        evaluation_role=evaluation_role,
        variant="s4_free_text_steps",
        success_filename="s4_free_text_steps_evaluation.json",
        failure_filename="s4_free_text_steps_failure.json",
        call_kind="s4_free_text_steps_eval",
        output_budget=output_budget,
        output_budget_field=output_budget_field,
        request_timeout_seconds=request_timeout_seconds,
    )


def run_s4_fact_state_evaluation(
    *,
    model: str,
    bundle: FrozenCompactBundle,
    output_dir: Path,
    api_url: str,
    api_key_args: Any,
    output_budget: int = COMPACT_OUTPUT_BUDGET,
    output_budget_field: str = "max_tokens",
    request_timeout_seconds: int = 600,
    evaluation_role: str = "model_calibration",
) -> dict[str, Any]:
    """Run S4 fact-state classification without severity or resolver logic."""
    payload = build_s4_fact_state_payload(
        model,
        bundle,
        output_budget=output_budget,
        output_budget_field=output_budget_field,
    )
    return _run_isolated_evaluation(
        model=model,
        bundle=bundle,
        output_dir=output_dir,
        api_url=api_url,
        api_key_args=api_key_args,
        payload=payload,
        validator=lambda value: validate_s4_fact_state_result(value, bundle),
        task_role=S4_FACT_STATE_ROLE,
        evaluation_role=evaluation_role,
        variant="s4_fact_state",
        success_filename="s4_fact_state_evaluation.json",
        failure_filename="s4_fact_state_failure.json",
        call_kind="s4_fact_state_eval",
        output_budget=output_budget,
        output_budget_field=output_budget_field,
        request_timeout_seconds=request_timeout_seconds,
    )


def run_s4_judgment_evaluation(
    *,
    model: str,
    bundle: FrozenCompactBundle,
    output_dir: Path,
    api_url: str,
    api_key_args: Any,
    output_budget: int = COMPACT_OUTPUT_BUDGET,
    output_budget_field: str = "max_tokens",
    request_timeout_seconds: int = 600,
    evaluation_role: str = "model_calibration",
) -> dict[str, Any]:
    """Run S4 relation/gap judgment from an immutable fact-state bundle."""
    payload = build_s4_judgment_payload(
        model,
        bundle,
        output_budget=output_budget,
        output_budget_field=output_budget_field,
    )
    return _run_isolated_evaluation(
        model=model,
        bundle=bundle,
        output_dir=output_dir,
        api_url=api_url,
        api_key_args=api_key_args,
        payload=payload,
        validator=validate_s4_judgment_result,
        task_role=S4_JUDGMENT_ROLE,
        evaluation_role=evaluation_role,
        variant="s4_judgment",
        success_filename="s4_judgment_evaluation.json",
        failure_filename="s4_judgment_failure.json",
        call_kind="s4_judgment_eval",
        output_budget=output_budget,
        output_budget_field=output_budget_field,
        request_timeout_seconds=request_timeout_seconds,
    )


def run_s5_audit_evaluation(
    *,
    model: str,
    bundle: FrozenCompactBundle,
    output_dir: Path,
    api_url: str,
    api_key_args: Any,
    output_budget: int = COMPACT_OUTPUT_BUDGET,
    output_budget_field: str = "max_tokens",
    request_timeout_seconds: int = 600,
    evaluation_role: str = "model_calibration",
) -> dict[str, Any]:
    """Run the S5 trust-state audit; it never writes production severity."""
    payload = build_s5_audit_payload(
        model,
        bundle,
        output_budget=output_budget,
        output_budget_field=output_budget_field,
    )
    return _run_isolated_evaluation(
        model=model,
        bundle=bundle,
        output_dir=output_dir,
        api_url=api_url,
        api_key_args=api_key_args,
        payload=payload,
        validator=lambda value: validate_s5_audit_result(value, bundle),
        task_role=S5_AUDIT_ROLE,
        evaluation_role=evaluation_role,
        variant="s5_audit",
        success_filename="s5_audit_evaluation.json",
        failure_filename="s5_audit_failure.json",
        call_kind="s5_audit_eval",
        output_budget=output_budget,
        output_budget_field=output_budget_field,
        request_timeout_seconds=request_timeout_seconds,
    )
