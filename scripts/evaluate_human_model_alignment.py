#!/usr/bin/env python3
"""Score saved model artifacts against human labels without making API calls.

This evaluator is deliberately outside the model request path. It keeps the
two layers separate:

* extraction is compared with human ``key_events`` using a stage-and-time
  overlap proxy, never with generated evidence IDs;
* judgment is compared with human gap magnitude and, when present, direction;
* unavailable, not-applicable, uncertain, and failed cells remain explicit in
  the denominator metadata.

The extraction precision metric is named ``temporal_stage_precision_proxy`` on
purpose. A human must still verify semantic truth; stage/time overlap alone
cannot prove that the text describes the right visual fact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm.compact_eval import (  # noqa: E402
    load_gt_stage_labels,
)
from flayr_core.alignment_scoring import (  # noqa: E402, F401
    GT_GAPS,
    QUALITY_FIELDS,
    ROLE_NAMES,
    S3_QUALITY_FIELDS,
    S4_QUALITY_FIELDS,
    SCORABLE_GAPS,
    SCORABLE_RELATIONS,
    STAGE_CODES,
    _empty_denominator,
    _event_matches_unit,
    _event_terms_match_unit,
    _human_stage_status,
    _overlaps,
    _quality_counts,
    _relation_gap_compatible,
    _stage_predictions,
    _validate_key_event,
    score_extraction,
    score_judgment,
)
from flayr_core.report_metadata import current_code_commit  # noqa: E402
from flayr_core.utils import write_json  # noqa: E402
from flayr_core.validation_cohort import SOURCE_CONTRACT_FILES  # noqa: E402


ALIGNMENT_SCHEMA_VERSION = 2
ALIGNMENT_PROTOCOL = "human_model_alignment_v2"
WHOLE_VIDEO_OBSERVATION_SCOPE = "whole_video_observation"

ALIGNMENT_METRIC_DEFINITIONS = {
    "gap_accuracy": "语义差距准确率；排除 legacy severity-only 无法表达 GT=none 的合同表达缺口",
    "contract_aware_gap_accuracy": "合同感知差距准确率；将 legacy severity-only 对 GT=none 的表达缺口计为不可表达错误",
    "contract_representation_gap_rate": "GT 有效格中，模型旧 severity-only 合同无法表达 none 的比例",
    "relation_accuracy": "仅在 GT relation 与模型 relation 均可解析时计算的方向准确率",
    "gap_coverage": "GT 可评分 gap 格中，模型给出可解析 gap 的比例；不把未回答强行算成语义错误",
    "relation_coverage": "GT 可评分 relation 格中，模型给出可解析 relation 的比例",
    "adjusted_gap_accuracy": "事实充分且应作答的 GT gap 格为分母；prediction_unavailable 计为错误，事实充分由 Stage1 双侧 clear/complete 审计确定",
    "adjusted_relation_accuracy": "事实充分且应作答的 GT relation 格为分母；prediction_unavailable 计为错误，事实充分由 Stage1 双侧 clear/complete 审计确定",
    "exact_direction_and_gap_accuracy": "方向和差距大小同时正确的准确率",
    "gt_large_recall": "GT=large 且模型产物可用于语义比较的格中，模型正确识别 large 的比例",
    "temporal_stage_recall_proxy": "人工 key_events 与模型 evidence_units 按角色、阶段和时间重叠匹配的召回代理",
    "temporal_stage_precision_proxy": "模型有效 evidence_units 中与人工 key_events 匹配的精确率代理；不代表语义真实",
}

def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value).strip())
    return cleaned.strip("._") or "unnamed"


def _safe_component_map(values: list[str], *, label: str) -> dict[str, str]:
    """Reject sanitized-name collisions before reading or writing artifacts."""
    safe_to_value: dict[str, str] = {}
    value_to_safe: dict[str, str] = {}
    for value in values:
        if value in value_to_safe:
            raise ValueError(f"{label} contains duplicate value: {value!r}")
        safe = _safe_component(value)
        previous = safe_to_value.get(safe)
        if previous is not None and previous != value:
            raise ValueError(
                f"{label} values {previous!r} and {value!r} share output component {safe!r}"
            )
        safe_to_value[safe] = value
        value_to_safe[value] = safe
    return value_to_safe


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_optional_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = _read_json(path)
    except (OSError, json.JSONDecodeError):
        return {"_sidecar_error": str(path)}
    return value if isinstance(value, dict) else {"_sidecar_error": str(path)}


def _manifest_row_id(row: dict[str, Any], index: int) -> str:
    """Read the frozen manifest's ``id`` or the runner's ``sample_id``.

    The two spellings are intentionally accepted only as aliases.  When both
    are present they must agree, so a hand-edited manifest cannot silently
    score one sample under another name.
    """
    raw_id = str(row.get("id") or "").strip()
    raw_sample_id = str(row.get("sample_id") or "").strip()
    if raw_id and raw_sample_id and raw_id != raw_sample_id:
        raise ValueError(f"manifest sample {index} has conflicting id/sample_id values")
    sample_id = raw_sample_id or raw_id
    if not sample_id:
        raise ValueError(f"manifest sample {index} must contain non-empty id or sample_id")
    return sample_id


def _manifest_row_is_stage_alignment_eligible(row: dict[str, Any]) -> bool:
    """Mirror the manifest scope rules used by the stage evaluator."""
    evaluation_scope = str(row.get("evaluation_scope") or "").strip()
    metric_scope = str(row.get("metric_scope") or "").strip()
    return not (
        metric_scope == "excluded"
        or evaluation_scope == WHOLE_VIDEO_OBSERVATION_SCOPE
        or metric_scope == WHOLE_VIDEO_OBSERVATION_SCOPE
    )


def _sample_ids(gt_path: Path, manifest_path: Path | None) -> list[str]:
    if manifest_path is not None:
        data = _read_json(manifest_path)
        rows = data.get("samples") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise ValueError("manifest must contain a samples list")
        sample_ids: list[str] = []
        seen: set[str] = set()
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(f"manifest sample {index} must be an object")
            sample_id = _manifest_row_id(row, index)
            if sample_id in seen:
                raise ValueError("manifest contains duplicate id/sample_id values")
            seen.add(sample_id)
            if _manifest_row_is_stage_alignment_eligible(row):
                sample_ids.append(sample_id)
        return sample_ids
    data = _read_json(gt_path)
    samples = data.get("samples") if isinstance(data, dict) else None
    if not isinstance(samples, dict) or not samples:
        raise ValueError("GT must contain a non-empty samples object")
    return sorted(
        str(sample_id)
        for sample_id, sample in samples.items()
        if isinstance(sample, dict)
        and (isinstance(sample.get("stages"), dict) or isinstance(sample.get("human_gap"), dict))
    )


def _artifact_source_durations(record: dict[str, Any]) -> dict[str, float]:
    roles = record.get("video_role_order")
    durations = record.get("video_source_duration_seconds")
    if not isinstance(roles, list) or not isinstance(durations, list) or len(roles) != len(durations):
        return {}
    result: dict[str, float] = {}
    for role, raw_duration in zip(roles, durations):
        try:
            duration = float(raw_duration)
        except (TypeError, ValueError):
            continue
        if role in ROLE_NAMES and duration > 0:
            result[str(role)] = duration
    return result


def _read_result_artifact(path: Path, result_key: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if not path.is_file():
        return None, {"status": "missing", "artifact": str(path)}
    try:
        record = _read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        return None, {"status": "invalid_artifact", "artifact": str(path), "error": str(exc)[:500]}
    if not isinstance(record, dict):
        return None, {"status": "invalid_artifact", "artifact": str(path), "error": "root is not an object"}
    result = record.get(result_key)
    if record.get("status") != "completed" or not isinstance(result, dict):
        return None, {
            "status": str(record.get("status") or "invalid"),
            "artifact": str(path),
            "failure_class": record.get("failure_class"),
            "contract_error_codes": record.get("contract_error_codes", []),
            "error": str(record.get("error") or record.get("errors") or "no completed result")[:1000],
        }
    request_metadata = _read_optional_json(path.parent / "compact_request_metadata.json")
    input_metadata = _read_optional_json(path.parent / "model_independent_input_metadata.json")
    metadata = {
        "status": "completed",
        "artifact": str(path),
        "artifact_sha256": _sha256(path),
        "schema_version": record.get("schema_version"),
        "source_commit": record.get("source_commit"),
        "source_digest": record.get("source_digest"),
        "paired_source_digest": record.get("source_digest"),
        "source_durations": _artifact_source_durations(record),
        "video_role_order": record.get("video_role_order"),
        "video_source_sha256": record.get("video_source_sha256"),
        "protocol_hash": request_metadata.get("protocol_hash"),
        "request_source_commit": request_metadata.get("source_commit"),
        "failure_class": None,
    }
    if result_key == "result" and path.name == "model_independent_evaluation.json":
        metadata["base_source_digest"] = input_metadata.get("base_source_digest")
        # This artifact's own source_digest is the derived fact-bundle digest.
        # The base bundle is a visual-facts input and may have a different digest
        # from the raw-video extraction input. Pair against the latter.
        source_extraction = input_metadata.get("source_extraction")
        metadata["source_extraction"] = source_extraction
        metadata["paired_source_digest"] = (
            source_extraction.get("source_digest")
            if isinstance(source_extraction, dict)
            else None
        )
        metadata["input_metadata_sidecar_error"] = input_metadata.get("_sidecar_error")
    metadata["request_metadata_sidecar_error"] = request_metadata.get("_sidecar_error")
    return result, metadata


def _canonical_source_digest(video_source_sha256: list[str]) -> str:
    payload = json.dumps(video_source_sha256, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _commit_matches(actual: Any, expected: Any, current: Any) -> bool:
    actual_text = str(actual or "").strip().lower()
    if not actual_text:
        return False
    candidates = {
        str(value or "").strip().lower()
        for value in (expected, current)
        if str(value or "").strip()
    }
    return any(
        actual_text == candidate
        or actual_text.startswith(candidate)
        or candidate.startswith(actual_text)
        for candidate in candidates
    )


def _production_surfaces_changed_between(recorded: Any, current: Any) -> bool:
    """Return whether production surfaces changed after a recorded artifact.

    The evaluator and its protocol may change after a production run without
    invalidating that run. A change under the frozen production surfaces is a
    semantic incompatibility and must remain excluded.
    """
    recorded_text = str(recorded or "").strip()
    current_text = str(current or "").strip()
    if not recorded_text or not current_text:
        return True
    try:
        recorded_commit = subprocess.run(
            ["git", "rev-parse", "--verify", f"{recorded_text}^{{commit}}"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        current_commit = subprocess.run(
            ["git", "rev-parse", "--verify", f"{current_text}^{{commit}}"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", recorded_commit, current_commit],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        surfaces = ("scripts/flayr.py", "scripts/flayr_core", *SOURCE_CONTRACT_FILES)
        changed = subprocess.run(
            ["git", "diff", "--name-only", f"{recorded_commit}..{current_commit}", "--", *surfaces],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        return any(str(path).strip() for path in changed)
    except (OSError, subprocess.CalledProcessError):
        return True


def _commit_is_compatible(actual: Any, expected: Any, current: Any) -> bool:
    """Accept frozen behavior, current source, or evaluator-only descendants."""
    if _commit_matches(actual, expected, current):
        return True
    return not _production_surfaces_changed_between(actual, current)


def _frozen_production_identity() -> dict[str, Any]:
    freeze_path = ROOT / "references/semantic-baseline-freeze.json"
    freeze = _read_json(freeze_path)
    route = freeze.get("model_route") if isinstance(freeze.get("model_route"), dict) else {}
    artifact_identity = (
        freeze.get("artifact_identity")
        if isinstance(freeze.get("artifact_identity"), dict)
        else {}
    )
    return {
        "vision_model": route.get("vision_model"),
        "judgment_model": route.get("judgment_model"),
        "production_behavior_commit": freeze.get("production_behavior_commit"),
        "analysis_schema_sha256": artifact_identity.get("analysis_schema_sha256"),
        "stage2_pipeline_version": artifact_identity.get("stage2_pipeline_version"),
    }


def _production_status_meta(
    run_dir: Path,
    *,
    status: str,
    reason: str,
    source_identity_status: str = "source_identity_incomplete",
) -> dict[str, Any]:
    return {
        "status": status,
        "artifact": str(run_dir),
        "source_identity_status": source_identity_status,
        "failure_class": "production_artifact_incompatible",
        "error": reason,
    }


def _stage_fact_sufficiency(facts: dict[str, dict[str, Any]]) -> dict[str, bool | None]:
    """Derive a conservative per-stage fact sufficiency mask from Stage1 audits.

    ``True`` requires both roles to have completed acquisition/qualification and
    an explicit Stage1 coverage audit with ``status=clear`` and
    ``coverage=complete``. Missing or unknown audits stay ``None``; they are not
    treated as proof that facts were insufficient or sufficient.
    """
    result: dict[str, bool | None] = {stage: None for stage in STAGE_CODES}
    for stage in STAGE_CODES:
        role_statuses: list[bool] = []
        for role in ROLE_NAMES:
            facts_for_role = facts.get(role)
            if not isinstance(facts_for_role, dict):
                role_statuses.append(False)
                continue
            acquisition = facts_for_role.get("stage1_acquisition")
            qualification = facts_for_role.get("stage1_qualification")
            audit = facts_for_role.get("stage1_coverage_audit")
            stage_audit = audit.get("stages", {}).get(stage) if isinstance(audit, dict) else None
            role_statuses.append(
                isinstance(acquisition, dict)
                and acquisition.get("status") in {"complete", "completed"}
                and isinstance(qualification, dict)
                and qualification.get("status") == "completed"
                and isinstance(stage_audit, dict)
                and stage_audit.get("status") == "clear"
                and stage_audit.get("coverage") == "complete"
            )
        if role_statuses and all(role_statuses):
            result[stage] = True
    return result


def _read_production_run(
    run_dir: Path,
    *,
    expected_model: str,
    expected_vision_model: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
    """Project one completed production run into the offline score shapes.

    This adapter is deliberately read-only. It validates the run manifest,
    route, source hashes, schema and pipeline version before exposing facts or
    stage judgments. A run that is merely readable but identity-incompatible is
    returned as an explicit non-scoring artifact, never as a best-effort score.
    """
    run_dir = run_dir.expanduser().resolve()
    identity = _frozen_production_identity()
    expected_vision = expected_vision_model or str(identity.get("vision_model") or "")
    success_path = run_dir / "_SUCCESS.json"
    analysis_path = run_dir / "analysis.json"
    if not success_path.is_file():
        meta = _production_status_meta(run_dir, status="missing_manifest", reason="missing _SUCCESS.json")
        return None, meta, None, meta.copy()
    try:
        success = _read_json(success_path)
    except (OSError, json.JSONDecodeError) as exc:
        meta = _production_status_meta(run_dir, status="invalid_manifest", reason=str(exc))
        return None, meta, None, meta.copy()
    if success.get("status") != "completed" or not analysis_path.is_file():
        meta = _production_status_meta(
            run_dir,
            status="incomplete_run",
            reason="_SUCCESS.json is not completed or analysis.json is missing",
        )
        return None, meta, None, meta.copy()
    try:
        analysis = _read_json(analysis_path)
    except (OSError, json.JSONDecodeError) as exc:
        meta = _production_status_meta(run_dir, status="invalid_artifact", reason=str(exc))
        return None, meta, None, meta.copy()

    provenance = success.get("provenance") if isinstance(success.get("provenance"), dict) else {}
    contract = analysis.get("analysis_result_contract") if isinstance(analysis.get("analysis_result_contract"), dict) else {}
    errors: list[str] = []
    if str(analysis.get("analysis_run_state") or "") != "completed":
        errors.append("analysis_run_state is not completed")
    if analysis.get("stage2_pipeline_version") != identity.get("stage2_pipeline_version"):
        errors.append("stage2_pipeline_version mismatch")
    if contract.get("schema_sha256") != identity.get("analysis_schema_sha256"):
        errors.append("analysis schema identity mismatch")
    if provenance.get("judgment_model") != expected_model:
        errors.append("judgment model mismatch")
    if provenance.get("vision_model") != expected_vision:
        errors.append("vision model mismatch")
    if not _commit_is_compatible(
        provenance.get("code_commit"),
        identity.get("production_behavior_commit"),
        current_code_commit(),
    ):
        errors.append("source commit is outside the frozen behavior/current source")

    inputs = success.get("inputs") if isinstance(success.get("inputs"), dict) else {}
    source_hashes: list[str] = []
    source_durations: dict[str, float] = {}
    for role in ("benchmark", "creator"):
        source = inputs.get(f"{role}_video") if isinstance(inputs.get(f"{role}_video"), dict) else {}
        declared = str(source.get("sha256") or "").strip()
        path_text = str(source.get("path") or "").strip()
        if not declared or not path_text:
            errors.append(f"{role} source identity is incomplete")
            source_hashes.append("")
            continue
        source_path = Path(path_text).expanduser()
        if not source_path.is_file():
            errors.append(f"{role} source file is missing")
            source_hashes.append(declared)
            continue
        actual = _sha256(source_path)
        source_hashes.append(declared)
        if actual != declared:
            errors.append(f"{role} source sha256 mismatch")
        duration = (
            analysis.get("dependencies", {}).get("source_durations", {}).get(role)
            if isinstance(analysis.get("dependencies"), dict)
            and isinstance(analysis.get("dependencies", {}).get("source_durations"), dict)
            else None
        )
        try:
            if duration is not None and float(duration) > 0:
                source_durations[role] = float(duration)
        except (TypeError, ValueError):
            pass

    required_artifacts = success.get("required_artifacts")
    if isinstance(required_artifacts, list):
        for name in required_artifacts:
            artifact_path = run_dir / str(name)
            if not artifact_path.is_file():
                errors.append(f"required artifact missing: {name}")
                continue
            declared = (success.get("artifacts", {}).get(str(name), {}) or {}).get("sha256")
            if declared and _sha256(artifact_path) != declared:
                errors.append(f"artifact sha256 mismatch: {name}")

    source_digest = _canonical_source_digest(source_hashes) if all(source_hashes) else None
    source_identity_status = "matched"
    if any("sha256 mismatch" in error for error in errors):
        source_identity_status = "blocked_source_identity_mismatch"
    elif any("source identity" in error or "source file" in error for error in errors):
        source_identity_status = "source_identity_incomplete"
    if errors:
        meta = _production_status_meta(
            run_dir,
            status="incompatible_artifact",
            reason="; ".join(errors[:20]),
            source_identity_status=source_identity_status,
        )
        return None, meta, None, meta.copy()

    facts: dict[str, Any] = {}
    for role in ("creator", "benchmark"):
        facts_path = run_dir / f"video_facts_{role}.json"
        if not facts_path.is_file():
            errors.append(f"missing video_facts_{role}.json")
            continue
        try:
            facts[role] = _read_json(facts_path)
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"invalid video_facts_{role}.json: {exc}")
            continue
        acquisition = facts[role].get("stage1_acquisition")
        if isinstance(acquisition, dict) and role not in source_durations:
            try:
                duration = float(acquisition.get("duration_seconds"))
                if duration > 0:
                    source_durations[role] = duration
            except (TypeError, ValueError):
                pass
    if errors:
        meta = _production_status_meta(run_dir, status="incomplete_artifact", reason="; ".join(errors[:20]))
        return None, meta, None, meta.copy()

    extraction_result = {
        "schema_version": facts["creator"].get("stage_evidence_contract_version"),
        "creator_evidence_units": facts["creator"].get("evidence_units", []),
        "benchmark_evidence_units": facts["benchmark"].get("evidence_units", []),
    }
    stage_judgments: list[dict[str, Any]] = []
    for stage in analysis.get("stage_analysis") if isinstance(analysis.get("stage_analysis"), list) else []:
        if not isinstance(stage, dict):
            continue
        stage_judgments.append(
            {
                "stage": stage.get("stage"),
                "gap_magnitude": stage.get("model_gap_magnitude") or stage.get("gap_magnitude") or stage.get("severity"),
                "relation": stage.get("relation"),
                "confidence": stage.get("confidence"),
            }
        )
    judgment_result = {"stage_judgments": stage_judgments}
    common_meta = {
        "status": "completed",
        "artifact": str(analysis_path),
        "artifact_sha256": _sha256(analysis_path),
        "source_digest": source_digest,
        "paired_source_digest": source_digest,
        "source_commit": provenance.get("code_commit"),
        "request_source_commit": provenance.get("code_commit"),
        "protocol_hash": contract.get("schema_sha256"),
        "video_role_order": ["benchmark", "creator"],
        "video_source_sha256": source_hashes,
        "source_durations": source_durations,
        "vision_model": provenance.get("vision_model"),
        "judgment_model": provenance.get("judgment_model"),
        "fact_sufficiency_by_stage": _stage_fact_sufficiency(facts),
        "run_dir": str(run_dir),
    }
    return extraction_result, common_meta, judgment_result, common_meta.copy()


def _source_digest(meta: dict[str, Any]) -> str | None:
    if "paired_source_digest" in meta:
        paired_digest = meta.get("paired_source_digest")
        return paired_digest.strip() if isinstance(paired_digest, str) and paired_digest.strip() else None
    base_digest = meta.get("base_source_digest")
    if isinstance(base_digest, str) and base_digest.strip():
        return base_digest.strip()
    source_digest = meta.get("source_digest")
    return source_digest.strip() if isinstance(source_digest, str) and source_digest.strip() else None


def _video_identity(meta: dict[str, Any]) -> dict[str, Any]:
    source_extraction = meta.get("source_extraction")
    source = source_extraction if isinstance(source_extraction, dict) else meta
    return {
        "video_role_order": source.get("video_role_order"),
        "video_source_sha256": source.get("video_source_sha256"),
    }


def _source_identity_audit(
    extraction_meta: dict[str, Any],
    judgment_meta: dict[str, Any],
) -> dict[str, Any]:
    """Make paired source provenance explicit before combining two artifacts."""
    override = extraction_meta.get("source_identity_status") or judgment_meta.get("source_identity_status")
    if override in {"blocked_source_identity_mismatch", "source_identity_incomplete"}:
        return {
            "status": override,
            "mismatches": [],
            "missing_fields": [],
            "reason": extraction_meta.get("error") or judgment_meta.get("error"),
        }
    if extraction_meta.get("status") == "not_requested" or judgment_meta.get("status") == "not_requested":
        return {"status": "not_comparable", "mismatches": [], "missing_fields": []}
    if extraction_meta.get("status") != "completed" or judgment_meta.get("status") != "completed":
        return {"status": "not_comparable", "mismatches": [], "missing_fields": []}

    mismatches: list[dict[str, Any]] = []
    missing_fields: list[str] = []
    extraction_digest = _source_digest(extraction_meta)
    judgment_digest = _source_digest(judgment_meta)
    if not extraction_digest or not judgment_digest:
        missing_fields.append("source_digest")
    elif extraction_digest != judgment_digest:
        mismatches.append(
            {
                "field": "source_digest",
                "extraction": extraction_digest,
                "judgment": judgment_digest,
            }
        )
    extraction_identity = _video_identity(extraction_meta)
    judgment_identity = _video_identity(judgment_meta)
    for field in ("video_role_order", "video_source_sha256"):
        left = extraction_identity.get(field)
        right = judgment_identity.get(field)
        if left in (None, []) or right in (None, []):
            missing_fields.append(field)
        elif left != right:
            mismatches.append({"field": field, "extraction": left, "judgment": right})

    status = "blocked_source_identity_mismatch" if mismatches else "matched" if not missing_fields else "source_identity_incomplete"
    return {
        "status": status,
        "mismatches": mismatches,
        "missing_fields": sorted(set(missing_fields)),
        "extraction_source_digest": extraction_digest,
        "judgment_source_digest": judgment_digest,
        "provenance": {
            "extraction_source_commit": extraction_meta.get("source_commit") or extraction_meta.get("request_source_commit"),
            "judgment_source_commit": judgment_meta.get("source_commit") or judgment_meta.get("request_source_commit"),
            "extraction_protocol_hash": extraction_meta.get("protocol_hash"),
            "judgment_protocol_hash": judgment_meta.get("protocol_hash"),
        },
    }


def _sample_record(
    sample_id: str,
    gt_labels: dict[str, dict[str, Any]],
    gt_sample: dict[str, Any],
    extraction_root: Path | None,
    judgment_root: Path | None,
    model: str,
    *,
    sample_component: str,
    model_component: str,
    production_root: Path | None = None,
    production_vision_model: str | None = None,
) -> dict[str, Any]:
    if production_root is not None:
        production_run = production_root / sample_component
        extraction_result, extraction_meta, judgment_result, judgment_meta = _read_production_run(
            production_run,
            expected_model=model,
            expected_vision_model=production_vision_model,
        )
        source_identity = _source_identity_audit(extraction_meta, judgment_meta)
        return {
            "sample_id": sample_id,
            "model": model,
            "source_identity": source_identity,
            "extraction": {
                "artifact": extraction_meta,
                "score": score_extraction(
                    extraction_result,
                    gt_sample,
                    artifact_status=extraction_meta["status"],
                    source_durations=extraction_meta.get("source_durations"),
                ),
            },
            "judgment": {
                "artifact": judgment_meta,
                "score": score_judgment(
                    judgment_result,
                    gt_labels,
                    artifact_status=judgment_meta["status"],
                    fact_sufficiency=extraction_meta.get("fact_sufficiency_by_stage"),
                ),
            },
        }
    extraction_path = (
        extraction_root / sample_component / model_component / "visual_extraction_evaluation.json"
        if extraction_root is not None
        else Path("__not_requested__")
    )
    judgment_path = (
        judgment_root / sample_component / model_component / "model_independent_evaluation.json"
        if judgment_root is not None
        else Path("__not_requested__")
    )
    extraction_result, extraction_meta = _read_result_artifact(extraction_path, "result") if extraction_root else (None, {"status": "not_requested"})
    judgment_result, judgment_meta = _read_result_artifact(judgment_path, "result") if judgment_root else (None, {"status": "not_requested"})
    source_identity = _source_identity_audit(extraction_meta, judgment_meta)
    return {
        "sample_id": sample_id,
        "model": model,
        "source_identity": source_identity,
        "extraction": {
            "artifact": extraction_meta,
            "score": score_extraction(
                extraction_result,
                gt_sample,
                artifact_status=extraction_meta["status"],
                source_durations=extraction_meta.get("source_durations"),
            ),
        },
        "judgment": {
            "artifact": judgment_meta,
            "score": score_judgment(
                judgment_result,
                gt_labels,
                artifact_status=judgment_meta["status"],
            ),
        },
    }


def _operational_summary(selected: list[dict[str, Any]], key: str) -> dict[str, Any]:
    """Summarize artifact availability without treating it as semantic quality."""
    metadata = [
        record.get(key, {}).get("artifact", {})
        for record in selected
        if isinstance(record.get(key), dict)
        and isinstance(record.get(key, {}).get("artifact"), dict)
    ]
    requested = [item for item in metadata if item.get("status") != "not_requested"]
    status_counts: dict[str, int] = {}
    failure_class_counts: dict[str, int] = {}
    contract_error_code_counts: dict[str, int] = {}
    for item in requested:
        status = str(item.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        if status != "completed":
            failure_class = str(item.get("failure_class") or "unspecified")
            failure_class_counts[failure_class] = failure_class_counts.get(failure_class, 0) + 1
        for code in item.get("contract_error_codes", []) if isinstance(item.get("contract_error_codes"), list) else []:
            normalized = str(code).strip()
            if normalized:
                contract_error_code_counts[normalized] = contract_error_code_counts.get(normalized, 0) + 1
    return {
        "requested_artifacts": len(requested),
        "completed_artifacts": sum(item.get("status") == "completed" for item in requested),
        "failed_or_missing_artifacts": sum(item.get("status") != "completed" for item in requested),
        "status_counts": dict(sorted(status_counts.items())),
        "failure_class_counts": dict(sorted(failure_class_counts.items())),
        "contract_error_code_counts": dict(sorted(contract_error_code_counts.items())),
    }


def _aggregate_judgment_stage_metrics(judgment_rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for stage_code in STAGE_CODES:
        rows = [
            row
            for score in judgment_rows
            for row in score.get("rows", [])
            if row.get("stage") == stage_code
        ]
        semantic_gap_rows = [row for row in rows if row.get("semantic_gap_comparable")]
        contract_aware_gap_rows = [
            row
            for row in rows
            if row.get("gt_gap_magnitude") in SCORABLE_GAPS
            and row.get("predicted_gap_magnitude") in SCORABLE_GAPS
        ]
        adjusted_gap_rows = [row for row in rows if row.get("adjusted_gap_eligible")]
        adjusted_relation_rows = [row for row in rows if row.get("adjusted_relation_eligible")]
        relation_rows = [row for row in rows if row.get("relation_correct") is not None]
        exact_rows = [row for row in semantic_gap_rows if row.get("relation_correct") is not None]
        large_rows = [row for row in rows if row.get("gt_gap_magnitude") == "large"]
        large_scored_rows = [row for row in large_rows if row.get("semantic_gap_comparable")]
        status_counts: dict[str, int] = {}
        error_counts: dict[str, int] = {}
        for row in rows:
            status = str(row.get("status") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
            error_class = str(row.get("error_class") or "unknown")
            error_counts[error_class] = error_counts.get(error_class, 0) + 1
        result[stage_code] = {
            "cell_count": len(rows),
            "status_counts": dict(sorted(status_counts.items())),
            "contract_aware_gap_cells": len(contract_aware_gap_rows),
            "contract_aware_gap_correct_cells": sum(row.get("gap_correct") is True for row in contract_aware_gap_rows),
            "contract_aware_gap_accuracy": (
                sum(row.get("gap_correct") is True for row in contract_aware_gap_rows)
                / len(contract_aware_gap_rows)
                if contract_aware_gap_rows
                else None
            ),
            "semantic_gap_cells": len(semantic_gap_rows),
            "semantic_gap_correct_cells": sum(row.get("gap_correct") is True for row in semantic_gap_rows),
            "semantic_gap_accuracy": (
                sum(row.get("gap_correct") is True for row in semantic_gap_rows)
                / len(semantic_gap_rows)
                if semantic_gap_rows
                else None
            ),
            "gap_coverage": (
                len(semantic_gap_rows) / sum(
                    row.get("gt_gap_magnitude") in SCORABLE_GAPS
                    and not row.get("contract_representation_gap")
                    for row in rows
                )
                if any(
                    row.get("gt_gap_magnitude") in SCORABLE_GAPS
                    and not row.get("contract_representation_gap")
                    for row in rows
                )
                else None
            ),
            "adjusted_gap_cells": len(adjusted_gap_rows),
            "adjusted_gap_correct_cells": sum(row.get("gap_correct") is True for row in adjusted_gap_rows),
            "adjusted_gap_accuracy": (
                sum(row.get("gap_correct") is True for row in adjusted_gap_rows) / len(adjusted_gap_rows)
                if adjusted_gap_rows
                else None
            ),
            "contract_representation_gap_cells": sum(
                row.get("error_class") == "contract_representation_gap" for row in rows
            ),
            "relation_cells": len(relation_rows),
            "relation_correct_cells": sum(row.get("relation_correct") is True for row in relation_rows),
            "relation_accuracy": (
                sum(row.get("relation_correct") is True for row in relation_rows) / len(relation_rows)
                if relation_rows
                else None
            ),
            "relation_coverage": (
                len(relation_rows) / sum(row.get("gt_relation") in SCORABLE_RELATIONS for row in rows)
                if any(row.get("gt_relation") in SCORABLE_RELATIONS for row in rows)
                else None
            ),
            "adjusted_relation_cells": len(adjusted_relation_rows),
            "adjusted_relation_correct_cells": sum(
                row.get("relation_correct") is True for row in adjusted_relation_rows
            ),
            "adjusted_relation_accuracy": (
                sum(row.get("relation_correct") is True for row in adjusted_relation_rows)
                / len(adjusted_relation_rows)
                if adjusted_relation_rows
                else None
            ),
            "exact_direction_and_gap_cells": len(exact_rows),
            "exact_direction_and_gap_correct_cells": sum(
                row.get("gap_correct") is True and row.get("relation_correct") is True
                for row in exact_rows
            ),
            "exact_direction_and_gap_accuracy": (
                sum(row.get("gap_correct") is True and row.get("relation_correct") is True for row in exact_rows)
                / len(exact_rows)
                if exact_rows
                else None
            ),
            "gt_large_cells": len(large_rows),
            "gt_large_scored_cells": len(large_scored_rows),
            "gt_large_unavailable_cells": len(large_rows) - len(large_scored_rows),
            "gt_large_correct_cells": sum(row.get("gap_correct") is True for row in large_scored_rows),
            "gt_large_missed_cells": sum(row.get("gap_correct") is not True for row in large_scored_rows),
            "gt_large_recall": (
                sum(row.get("gap_correct") is True for row in large_scored_rows) / len(large_scored_rows)
                if large_scored_rows
                else None
            ),
            "error_class_counts": dict(sorted(error_counts.items())),
        }
    return result


def _merge_stage_quality_counts(stage_scores: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    merged: dict[str, dict[str, int]] = {field: {} for field in QUALITY_FIELDS}
    for score in stage_scores:
        source = score.get("quality_counts") if isinstance(score, dict) else None
        if not isinstance(source, dict):
            continue
        for field, values in source.items():
            if field not in merged or not isinstance(values, dict):
                continue
            for value, count in values.items():
                merged[field][str(value)] = merged[field].get(str(value), 0) + int(count)
    return {field: dict(sorted(values.items())) for field, values in merged.items() if values}


def aggregate_model(records: list[dict[str, Any]], model: str) -> dict[str, Any]:
    all_selected = [record for record in records if record.get("model") == model]
    blocked = [
        record
        for record in all_selected
        if isinstance(record.get("source_identity"), dict)
        and record["source_identity"].get("status") == "blocked_source_identity_mismatch"
    ]
    incomplete = [
        record
        for record in all_selected
        if isinstance(record.get("source_identity"), dict)
        and record["source_identity"].get("status") == "source_identity_incomplete"
    ]
    not_comparable = [
        record
        for record in all_selected
        if isinstance(record.get("source_identity"), dict)
        and record["source_identity"].get("status") == "not_comparable"
    ]
    excluded = {id(record) for record in [*blocked, *incomplete, *not_comparable]}
    selected = [
        record
        for record in all_selected
        if id(record) not in excluded
    ]
    judgment_rows = [record["judgment"]["score"] for record in selected]
    extraction_rows = [record["extraction"]["score"] for record in selected]
    judgment_denominator = _empty_denominator()
    for score in judgment_rows:
        for key, value in score["denominator"].items():
            judgment_denominator[key] += int(value)
    gap_rows = [
        row
        for score in judgment_rows
        for row in score["rows"]
        if row.get("status") == "labeled"
        and row.get("gt_gap_magnitude") in SCORABLE_GAPS
        and row.get("predicted_gap_magnitude") in SCORABLE_GAPS
    ]
    semantic_gap_rows = [row for row in gap_rows if row.get("semantic_gap_comparable")]
    adjusted_gap_rows = [
        row
        for score in judgment_rows
        for row in score["rows"]
        if row.get("adjusted_gap_eligible")
    ]
    relation_rows = [
        row
        for score in judgment_rows
        for row in score["rows"]
        if row.get("relation_correct") is not None
    ]
    adjusted_relation_rows = [
        row
        for score in judgment_rows
        for row in score["rows"]
        if row.get("adjusted_relation_eligible")
    ]
    exact_rows = [row for row in gap_rows if row.get("relation_correct") is not None]
    extraction_denominator = {
        key: sum(int(score["denominator"][key]) for score in extraction_rows)
        for key in (
            "required_key_events",
            "present_key_events",
            "invalid_key_events",
            "absence_checks",
            "absence_respected",
            "absence_false_positive_units",
            "scored_key_events",
            "matched_key_events",
            "valid_model_units",
            "model_units_matching_key_events",
            "model_failure_or_missing",
        )
    }
    stage_metrics: dict[str, Any] = {}
    for stage_code in STAGE_CODES:
        stage_scores = [score["stage_metrics"][stage_code] for score in extraction_rows]
        events = sum(score["required_event_count"] for score in stage_scores)
        absence_checks = sum(score["absence_check_count"] for score in stage_scores)
        absence_respected = sum(score["absence_respected_count"] for score in stage_scores)
        scored_events = sum(score["scored_event_count"] for score in stage_scores)
        matched = sum(score["matched_event_count"] for score in stage_scores)
        units = sum(score["unit_count"] for score in stage_scores)
        quality_coverage_numerator = sum(
            score["quality_coverage"] * score["unit_count"] for score in stage_scores
        )
        stage_metrics[stage_code] = {
            "required_event_count": events,
            "scored_event_count": scored_events,
            "matched_event_count": matched,
            "recall_proxy": matched / scored_events if scored_events else None,
            "absence_check_count": absence_checks,
            "absence_respected_count": absence_respected,
            "absence_respected_rate": (
                absence_respected / absence_checks if absence_checks else None
            ),
            "unit_count": units,
            "quality_coverage": quality_coverage_numerator / units if units else 0.0,
            "quality_counts": _merge_stage_quality_counts(stage_scores),
        }
    judgment_stage_metrics = _aggregate_judgment_stage_metrics(judgment_rows)
    error_class_counts = {
        error_class: sum(
            row.get("error_class") == error_class
            for score in judgment_rows
            for row in score["rows"]
        )
        for error_class in sorted(
            {
                str(row.get("error_class"))
                for score in judgment_rows
                for row in score["rows"]
            }
        )
    }
    return {
        "model": model,
        "sample_count": len(all_selected),
        "scored_sample_count": len(selected),
        "source_identity_mismatch_sample_count": len(blocked),
        "source_identity_incomplete_sample_count": len(incomplete),
        "source_identity_not_comparable_sample_count": len(not_comparable),
        "judgment": {
            "denominator": judgment_denominator,
            "gap_accuracy": (
                sum(row.get("gap_correct") is True for row in semantic_gap_rows) / len(semantic_gap_rows)
                if semantic_gap_rows
                else None
            ),
            "contract_aware_gap_accuracy": (
                sum(row.get("gap_correct") is True for row in gap_rows) / len(gap_rows)
                if gap_rows
                else None
            ),
            "semantic_gap_accuracy_excluding_representation_gap": (
                sum(row.get("gap_correct") is True for row in semantic_gap_rows) / len(semantic_gap_rows)
                if semantic_gap_rows
                else None
            ),
            "contract_representation_gap_rate": (
                sum(row.get("contract_representation_gap") is True for score in judgment_rows for row in score["rows"])
                / judgment_denominator["gt_labeled_cells"]
                if judgment_denominator["gt_labeled_cells"]
                else None
            ),
            "gap_coverage": (
                judgment_denominator["scored_gap_cells"] / judgment_denominator["gt_scorable_gap_cells"]
                if judgment_denominator["gt_scorable_gap_cells"]
                else None
            ),
            "relation_coverage": (
                judgment_denominator["scored_relation_cells"] / judgment_denominator["gt_scorable_relation_cells"]
                if judgment_denominator["gt_scorable_relation_cells"]
                else None
            ),
            "adjusted_gap_accuracy": (
                sum(row.get("gap_correct") is True for row in adjusted_gap_rows) / len(adjusted_gap_rows)
                if adjusted_gap_rows
                else None
            ),
            "adjusted_relation_accuracy": (
                sum(row.get("relation_correct") is True for row in adjusted_relation_rows)
                / len(adjusted_relation_rows)
                if adjusted_relation_rows
                else None
            ),
            "relation_accuracy": sum(row.get("relation_correct") is True for row in relation_rows) / len(relation_rows) if relation_rows else None,
            "exact_direction_and_gap_accuracy": (
                sum(row.get("gap_correct") is True and row.get("relation_correct") is True for row in exact_rows if row.get("semantic_gap_comparable"))
                / sum(row.get("semantic_gap_comparable") is True for row in exact_rows)
                if any(row.get("semantic_gap_comparable") for row in exact_rows)
                else None
            ),
            "stage_metrics": judgment_stage_metrics,
            "error_class_counts": error_class_counts,
            "operational": _operational_summary(all_selected, "judgment"),
        },
        "extraction": {
            "denominator": extraction_denominator,
            "temporal_stage_recall_proxy": (
                extraction_denominator["matched_key_events"] / extraction_denominator["scored_key_events"]
                if extraction_denominator["scored_key_events"]
                else None
            ),
            "temporal_stage_precision_proxy": (
                extraction_denominator["model_units_matching_key_events"] / extraction_denominator["valid_model_units"]
                if extraction_denominator["scored_key_events"] and extraction_denominator["valid_model_units"]
                else None
            ),
            "absence_respected_rate": (
                extraction_denominator["absence_respected"] / extraction_denominator["absence_checks"]
                if extraction_denominator["absence_checks"]
                else None
            ),
            "stage_metrics": stage_metrics,
            "operational": _operational_summary(all_selected, "extraction"),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Score saved Flayr extraction/judgment artifacts against human GT without API calls.",
        allow_abbrev=False,
    )
    parser.add_argument("--gt-path", type=Path, required=True)
    parser.add_argument("--extraction-root", type=Path, default=None)
    parser.add_argument("--judgment-root", type=Path, default=None)
    parser.add_argument(
        "--production-root",
        type=Path,
        default=None,
        help="Root containing one completed production run per sample-id; read-only strict adapter.",
    )
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--models", nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--evaluation-role",
        choices=("model_calibration", "mechanism_regression", "blind_validation"),
        default="model_calibration",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.extraction_root is None and args.judgment_root is None and args.production_root is None:
        raise SystemExit("at least one artifact root is required")
    if args.production_root is not None and (args.extraction_root is not None or args.judgment_root is not None):
        raise SystemExit("--production-root cannot be combined with compact artifact roots")
    gt_path = args.gt_path.expanduser().resolve()
    gt_data = _read_json(gt_path)
    samples = gt_data.get("samples") if isinstance(gt_data, dict) else None
    if not isinstance(samples, dict):
        raise SystemExit("GT must contain a samples object")
    sample_ids = _sample_ids(gt_path, args.manifest.expanduser().resolve() if args.manifest else None)
    try:
        sample_components = _safe_component_map(sample_ids, label="sample_id")
        model_components = _safe_component_map(args.models, label="model")
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    extraction_root = args.extraction_root.expanduser().resolve() if args.extraction_root else None
    judgment_root = args.judgment_root.expanduser().resolve() if args.judgment_root else None
    production_root = args.production_root.expanduser().resolve() if args.production_root else None
    records: list[dict[str, Any]] = []
    for sample_id in sample_ids:
        sample = samples.get(sample_id)
        if not isinstance(sample, dict):
            raise SystemExit(f"GT sample is missing or invalid: {sample_id}")
        labels = load_gt_stage_labels(gt_path, sample_id)
        for model in args.models:
            records.append(
                _sample_record(
                    sample_id,
                    labels,
                    sample,
                    extraction_root,
                    judgment_root,
                    model,
                    sample_component=sample_components[sample_id],
                    model_component=model_components[model],
                    production_root=production_root,
                )
            )
    output = {
        "schema_version": ALIGNMENT_SCHEMA_VERSION,
        "protocol": ALIGNMENT_PROTOCOL,
        "evaluation_role": args.evaluation_role,
        "promotion_eligible": False,
        "gt_loaded": True,
        "model_results_are_prompt_gt_free": True,
        "source_commit": current_code_commit(),
        "gt_path": str(gt_path),
        "gt_sha256": _sha256(gt_path),
        "sample_ids": sample_ids,
        "population": {
            "sample_count": len(sample_ids),
            "stage_count_per_sample": len(STAGE_CODES),
            "stage_cell_count": len(sample_ids) * len(STAGE_CODES),
            "denominator_rule": "exclude not_applicable, uncertain, missing, and invalid GT cells from semantic accuracy; count model failures separately",
            "extraction_matching_rule": "role + stage function + positive time overlap; semantic truth is not proven by this proxy",
            "source_identity_rule": "paired extraction/judgment records with mismatched or incomplete source identity are explicitly excluded from aggregate semantic metrics; missing artifacts remain operational failures",
        },
        "metric_definitions": ALIGNMENT_METRIC_DEFINITIONS,
        "models": list(args.models),
        "records": records,
        "aggregate": [aggregate_model(records, model) for model in args.models],
    }
    output_path = args.output.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(output_path, output)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
