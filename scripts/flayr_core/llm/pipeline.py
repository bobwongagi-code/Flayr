"""flayr_core.llm.pipeline：LLM 分析主入口。

三个 public 函数：
  - run_comparison_scope_preflight  只建立锁定事实与双视频产品比较资格
  - run_large_model_analysis        从 analysis_input.md 跑完整分析，写出 analysis_result.json
  - merge_analysis_result           把外部提供的 analysis_result.json 合并进 analysis dict

所有入口通过 finalize_analysis_result 走同一条完整处理链，避免外部 JSON 和实时 LLM
因校验顺序不同而产生不同报告。
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from ..artifacts import format_seconds, get_analysis_frame_entries, parse_time_range_seconds
from ..evidence_states import stage_flag_allows_empty_evidence
from ..multimodal import sanitize_audio_observations
from ..utils import write_json, write_text
from ..analysis_model import (
    ANALYSIS_RESULT_CONTRACT,
    AnalysisResult,
    CanonicalAnalysisResult,
    schema_sha256,
)
from ..stage_evidence_contracts import (
    STAGE_EVIDENCE_CONTRACT_VERSION,
    STAGE_EVIDENCE_SNAPSHOT_VERSION,
    STAGE1_COVERAGE_AUDIT_VERSION,
    STAGE1_COVERAGE_AUDIT_INDEPENDENCE,
    build_stage1_acquisition_manifest,
    freeze_stage_evidence,
    normalize_stage_evidence_checks,
    normalize_stage1_coverage_audit,
    stage_evidence_check_map,
    stage_evidence_contract,
    qualified_stage_evidence_ids,
    stage_codes,
    stage_evidence_contract_issues,
    stage_evidence_immutability_issues,
    stage_evidence_link_issues,
    stage_evidence_recovery_targets,
    stage_evidence_runtime_issues,
    stage_evidence_snapshot_issues,
    stage_evidence_readiness,
    stage_analysis_evidence_view,
    stage1_ledger_manifest,
    stage1_qualification_projection,
    STAGE1_QUALIFICATION_GROUPS,
    normalize_stage_code,
    stage1_coverage_audit_issues,
    stage1_acquisition_issues,
    stage1_forbidden_field_issues,
    stage1_pipeline_owned_field_issues,
    merge_stage_signal_bindings,
    reconcile_stage_evidence_links,
    required_stage_signals_satisfied,
)
from ..structure_modules import canonical_module_id
from .api import (
    call_llm_api,
    extract_chat_completion_text,
    read_llm_api_key,
    can_analyze_native_audio,
)
from .analysis_contract import AnalysisContractError, validate_normalized_analysis_contract
from .artifact_identity import identity_value
from .json_codec import ResponseParseError
from .parse import (
    normalize_analysis_result,
    normalize_category_profile,
    normalize_comparison_contract,
    normalize_absolute_execution_shadow,
    normalize_product_profile,
    normalize_video_product_identity,
    normalize_video_fact_result,
    normalized_fact_id,
    parse_json_text,
)
from .payload import (
    build_improvement_reconciliation_payload,
    build_comparison_eligibility_payload,
    build_absolute_execution_shadow_payload,
    build_stage_group_judgment_payload,
    build_stage_synthesis_payload,
    STAGE_JUDGMENT_GROUPS,
    build_product_foundation_payload,
    build_product_foundation_repair_payload,
    build_stage_review_payload,
    build_video_fact_recovery_payload,
    build_stage_evidence_qualification_payload,
    build_video_identity_payload,
    build_video_fact_payload,
    load_brand_proposition,
    stage1_recovery_media_windows,
    stage_review_media_windows,
)
from .stage_review_contract import (
    PHASE_C_PATCH_SNAPSHOT_SCHEMA,
    PHASE_C_REVIEW_MODE,
    PHASE_C_REVIEW_SCHEMA_VERSION,
    patch_fields_for_stage,
)
from .stage_group_artifacts import (
    StageGroupArtifactError,
    completed_stage_group_artifact,
    failed_stage_group_artifact,
    read_stage_group_artifact,
    revalidatable_failed_stage_group_response,
    reusable_stage_group_response,
    stage_group_artifact_path,
)
from .stage_fact_artifacts import (
    StageFactArtifactError,
    completed_stage_fact_artifact,
    failed_stage_fact_artifact,
    read_stage_fact_artifact,
    reusable_stage_fact_response,
    stage_fact_artifact_path,
)
from .provider_artifacts import (
    ProviderCallError,
    ProviderReplayError,
    provider_call_with_artifact,
)
from .media import select_role_visual_inputs, select_stage_recovery_visual_inputs
from ..finalization import facade as finalization_facade
from ..postprocess import apply_postprocess_chain, apply_segmented_postprocess_chain
from ..postprocess.audit import MAX_CHANGE_ENTRIES, PostprocessAudit, build_field_sources
from ..postprocess.derive import critical_severity_stages
from ..postprocess.global_diagnosis import materialize_global_diagnosis
from ..postprocess.utils import evidence_overlaps_range
from ..postprocess.health_rewrite import (
    sanitize_child_toothpaste_recommendations,
    sanitize_health_recommendations,
    validate_creator_script_language,
    validate_recommendation_safety,
)
from ..postprocess.repair import (
    clamp_result_time_ranges,
    ground_improvement_evidence,
    remove_unverified_brand_models,
    stabilize_improvement_priorities,
)
from ..postprocess.repair_stages import apply_fact_scoped_s5_comparison_contract
from ..postprocess.validate import (
    validate_analysis_dimensions,
    validate_evidence_alignment,
    validate_quality_contract,
    validate_stage_ownership,
)


def judgment_model(args: argparse.Namespace) -> str:
    """Return the single semantic-judgment model selected for this run."""
    return str(
        getattr(args, "judgment_model", "")
        or getattr(args, "llm_model", "")
        or ""
    ).strip()


def vision_model(args: argparse.Namespace) -> str:
    """Return the visual-evidence model, preserving the legacy single-model route."""
    return str(
        getattr(args, "vision_model", "")
        or getattr(args, "llm_model", "")
        or ""
    ).strip()


def _stage1_model(args: argparse.Namespace, phase: str) -> str:
    return judgment_model(args) if str(phase).upper() in {"B", "D"} else vision_model(args)


class AnalysisPipelineError(RuntimeError):
    """One typed failure at a named analysis-pipeline boundary."""

    def __init__(self, phase: str, failure_kind: str, cause: BaseException) -> None:
        self.phase = str(phase)
        self.failure_kind = str(failure_kind)
        self.cause_type = cause.__class__.__name__
        detail = str(cause).strip() or self.cause_type
        super().__init__(detail)


def _run_pipeline_phase(phase: str, failure_kind: str, operation: Any) -> Any:
    """Preserve the phase that failed instead of calling every failure an LLM error."""
    try:
        return operation()
    except AnalysisPipelineError:
        raise
    except ProviderReplayError as exc:
        raise AnalysisPipelineError(phase, "provider_replay", exc) from exc
    except ProviderCallError as exc:
        raise AnalysisPipelineError(phase, "provider_call", exc) from exc
    except (StageFactArtifactError, StageGroupArtifactError) as exc:
        raise AnalysisPipelineError(phase, "provider_replay", exc) from exc
    except ResponseParseError as exc:
        raise AnalysisPipelineError(phase, "response_parse", exc) from exc
    except (Exception, SystemExit) as exc:
        raise AnalysisPipelineError(phase, failure_kind, exc) from exc


def _localized_failure_kind(
    exc: BaseException,
    *,
    execution_source: str,
    default: str,
) -> str:
    """Classify an isolated failure without turning it into a global model error."""
    if execution_source == "replay" or isinstance(
        exc, (ProviderReplayError, StageFactArtifactError, StageGroupArtifactError)
    ):
        return "provider_replay"
    if isinstance(exc, ResponseParseError):
        return "response_parse"
    if isinstance(exc, ProviderCallError):
        return "provider_call"
    return default


def _is_strict_replay_failure(args: argparse.Namespace, exc: BaseException) -> bool:
    """Return whether a replay-integrity failure must escape a degradable lane."""
    if isinstance(exc, ProviderReplayError):
        return any(
            getattr(args, name, None) is not None
            for name in ("provider_replay_from", "stage2_replay_from")
        )
    if isinstance(exc, StageFactArtifactError):
        return getattr(args, "stage1_replay_from", None) is not None
    if isinstance(exc, StageGroupArtifactError):
        return getattr(args, "stage2_replay_from", None) is not None
    return False


# 修改 build_video_fact_payload 的语义合同后必须递增，避免旧 facts 与新判断规则混用。
VIDEO_FACT_CACHE_SCHEMA_VERSION = 31
PRODUCT_FOUNDATION_CACHE_SCHEMA_VERSION = 3
CACHE_RECORD_SCHEMA_VERSION = 1
STAGE1_A_REQUEST_TIMEOUT_SECONDS = 300
STAGE1_A_REQUEST_RETRIES = 1
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _analysis_artifact_dir(analysis: dict[str, Any]) -> Path | None:
    """Return the trusted per-run directory used for provenance artifacts."""
    value = analysis.get("run_dir") if isinstance(analysis, dict) else None
    if not value:
        return None
    try:
        return Path(str(value)).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _full_provider_replay_requested(args: argparse.Namespace) -> bool:
    """Return whether every LLM provider boundary has a strict replay source."""
    return all(
        getattr(args, name, None)
        for name in ("provider_replay_from", "stage1_replay_from", "stage2_replay_from")
    )


def _clamp_result_time_ranges(result: dict[str, Any], analysis: dict[str, Any]) -> None:
    """Clamp rounded fact timestamps against a matching rounded video duration.

    Facts and stage ranges are serialized to one decimal place, while ffprobe
    durations can retain sub-frame precision (for example 45.666667s).  Use a
    shallow analysis copy for this boundary check so a legitimate final 45.7s
    fact is not erased, without changing the exact duration retained elsewhere.
    """
    videos = analysis.get("videos") if isinstance(analysis, dict) else None
    if not isinstance(videos, dict):
        clamp_result_time_ranges(result, analysis)
        return
    rounded_videos: dict[str, Any] = {}
    for role, info in videos.items():
        if not isinstance(info, dict):
            rounded_videos[role] = info
            continue
        rounded_info = dict(info)
        duration = rounded_info.get("duration_seconds")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            rounded_info["duration_seconds"] = round(float(duration), 1)
        rounded_videos[role] = rounded_info
    clamp_analysis = dict(analysis)
    clamp_analysis["videos"] = rounded_videos
    clamp_result_time_ranges(result, clamp_analysis)


def _write_raw_model_response(
    run_dir: Path,
    *,
    result: dict[str, Any] | None = None,
    raw_text: str | None = None,
    source_format: str | None = None,
    overwrite: bool = False,
) -> None:
    """Preserve the provider payload or an explicitly named provider bundle."""
    path = run_dir / "raw_model_response.json"
    if path.exists() and not overwrite:
        return
    if raw_text is not None:
        record: dict[str, Any] = {"source_format": "raw_text", "raw_text": str(raw_text)}
        if isinstance(result, dict):
            record["parsed_result"] = result
        write_json(path, record)
    elif isinstance(result, dict):
        record = copy.deepcopy(result)
        record["source_format"] = str(source_format or "provider_response")
        write_json(path, record)
    else:
        write_json(path, {"source_format": "raw_text", "raw_text": ""})


def _refresh_final_derived_artifact(
    analysis: dict[str, Any],
    normalized: dict[str, Any],
    metadata_fields: tuple[str, ...] = (),
) -> None:
    """Keep final_derived_result aligned with metadata attached after finalization."""
    _merge_postprocess_audit(analysis, normalized, metadata_fields=metadata_fields)


def _merge_postprocess_audit(
    analysis: dict[str, Any],
    normalized: dict[str, Any],
    audit: PostprocessAudit | None = None,
    metadata_fields: tuple[str, ...] = (),
) -> None:
    """Append post-finalization changes without losing the canonical final artifact."""
    artifact_dir = _analysis_artifact_dir(analysis)
    if artifact_dir is None or not (artifact_dir / "final_derived_result.json").is_file():
        return
    log_path = artifact_dir / "postprocess_change_log.json"
    try:
        log = json.loads(log_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log = {"schema_version": 1, "change_count": 0, "truncated": False, "changes": []}
    if not isinstance(log, dict):
        log = {"schema_version": 1, "change_count": 0, "truncated": False, "changes": []}
    changes = list(log.get("changes")) if isinstance(log, dict) and isinstance(log.get("changes"), list) else []
    if audit is not None:
        room = max(0, MAX_CHANGE_ENTRIES - len(changes))
        changes.extend(audit.changes[:room])
        audit_truncated = audit.truncated or len(audit.changes) > room
    else:
        audit_truncated = False
    existing_metadata_paths = {
        str(change.get("path"))
        for change in changes
        if isinstance(change, dict) and change.get("rule") == "pipeline.attach_result_metadata"
    }
    metadata_truncated = False
    for field in metadata_fields:
        path = f"/{field}"
        if field not in normalized or path in existing_metadata_paths:
            continue
        if len(changes) >= MAX_CHANGE_ENTRIES:
            metadata_truncated = True
            continue
        changes.append(
            {
                "path": path,
                "old": {"present": False},
                "new": {"present": True},
                "rule": "pipeline.attach_result_metadata",
                "kind": "deterministic_derivation",
                "evidence": [],
            }
        )
    raw_result: dict[str, Any] = {}
    normalized_result: dict[str, Any] = {}
    try:
        raw_artifact = json.loads((artifact_dir / "raw_model_response.json").read_text(encoding="utf-8"))
        if isinstance(raw_artifact, dict):
            raw_result = raw_artifact.get("parsed_result") if isinstance(raw_artifact.get("parsed_result"), dict) else raw_artifact
        loaded_normalized = json.loads(
            (artifact_dir / "validated_normalized_result.json").read_text(encoding="utf-8")
        )
        if isinstance(loaded_normalized, dict):
            normalized_result = loaded_normalized
    except (OSError, json.JSONDecodeError):
        pass
    trace_result = copy.deepcopy(normalized)
    trace_result.pop("postprocess_provenance", None)
    log_was_truncated = bool(log.get("truncated")) if isinstance(log, dict) else False
    field_sources = build_field_sources(
        raw_result,
        normalized_result,
        trace_result,
        changes,
        truncated=log_was_truncated or audit_truncated or metadata_truncated,
    )
    if isinstance(log, dict):
        log["field_sources"] = field_sources
        log["changes"] = changes
        log["change_count"] = len(changes)
        log["truncated"] = bool(log.get("truncated")) or audit_truncated or metadata_truncated
        provenance = normalized.get("postprocess_provenance")
        if isinstance(provenance, dict):
            provenance["change_count"] = len(changes)
            provenance["change_log_truncated"] = log["truncated"]
            provenance["field_sources"] = field_sources
    write_json(log_path, log)
    write_json(artifact_dir / "final_derived_result.json", normalized)


# ---------------------------------------------------------------------------
# 外部 JSON 合并入口
# ---------------------------------------------------------------------------

def run_comparison_scope_preflight(
    args: argparse.Namespace,
    analysis: dict[str, Any],
    run_dir: Path,
) -> dict[str, Any]:
    """只跑事实抽取和产品级比较资格，供验证集入场审计使用。

    资格判断只依赖锁定的双侧产品身份，不应为它支付阶段对比、Phase C 或提升点的成本。
    事实与资格文件仍按完整链路同名落盘，因此后续可在同一目录继续运行完整分析。
    """
    api_key = read_llm_api_key(args).strip()
    if not api_key and not args.llm_dry_run and not getattr(args, "provider_replay_from", None):
        raise SystemExit("比较资格预检需要 LLM API key。")
    facts = run_video_identity_extraction(args, analysis, run_dir, api_key)
    if args.llm_dry_run:
        return normalize_comparison_contract({"reason": "dry run 未调用事实抽取和资格判定。"})
    eligibility = establish_comparison_eligibility(args, facts, run_dir, api_key)
    analysis["comparison_contract"] = eligibility
    analysis["comparison_eligibility"] = eligibility
    analysis["video_understanding"] = facts
    analysis["analysis_source"] = {
        "type": "comparison_scope_preflight",
        "merged_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    return eligibility

def apply_finalized_analysis_result(
    analysis: dict[str, Any],
    normalized: dict[str, Any],
    result_path: Path,
) -> None:
    """把已完成校验的结果写回主 analysis；此处不得再次做后处理。"""
    phase_c_review = normalized.get("phase_c_review")
    AnalysisResult.from_mapping(normalized).project_into(analysis)
    for metadata_key in ("analysis_import_mode", "legacy_import"):
        if metadata_key in normalized:
            analysis[metadata_key] = copy.deepcopy(normalized[metadata_key])
    if isinstance(phase_c_review, dict):
        analysis["phase_c_review"] = phase_c_review
    pipeline_status = str(normalized.get("stage2_pipeline_status") or "completed").strip().lower()
    if pipeline_status in {"degraded", "failed"} or str(
        analysis.get("product_foundation_status") or ""
    ).strip().lower() in {"degraded", "failed"}:
        analysis["improvements_status"] = "degraded"
        analysis["analysis_run_state"] = "degraded"
    else:
        analysis["improvements_status"] = "llm_completed"
        analysis["analysis_run_state"] = "completed"
    analysis["analysis_source"] = {
        "type": "legacy_import" if analysis.get("analysis_import_mode") == "legacy" else "large_model_json",
        "path": str(result_path),
        "merged_at": dt.datetime.now().isoformat(timespec="seconds"),
    }


def _refresh_segmented_pipeline_status(result: dict[str, Any]) -> tuple[str, list[str]]:
    """Recompute segmented run status from the persisted stage outcomes.

    The status written by the live runner is only an input to finalization.  A
    result can also enter through ``--analysis-result-json`` or an offline
    replay, so the finalizer must derive the publishability decision again from
    the four group records and the six projected stages.
    """
    stage_results = result.get("stage_analysis")
    unresolved = _segmented_stage_unresolved(stage_results if isinstance(stage_results, list) else [])
    pipeline = result.get("segmented_pipeline")
    if not isinstance(pipeline, dict):
        pipeline = {}
        result["segmented_pipeline"] = pipeline
    groups = pipeline.get("stage_groups")
    group_statuses = {
        str(item.get("status") or "").strip().lower()
        for item in groups
        if isinstance(item, dict)
    } if isinstance(groups, list) else set()
    required_groups = {"S1_S2", "S3_S4", "S5", "S6"}
    observed_groups = {
        "_".join(str(stage).strip().upper() for stage in item.get("group") or [])
        for item in groups
        if isinstance(item, dict)
    } if isinstance(groups, list) else set()
    metadata_incomplete = not required_groups.issubset(observed_groups)
    synthesis_failed = str(pipeline.get("synthesis_status") or "").strip().lower() not in {"", "completed"}
    current_status = str(
        result.get("stage2_candidate_status")
        or result.get("stage2_pipeline_status")
        or pipeline.get("candidate_status")
        or pipeline.get("status")
        or ""
    ).strip().lower()
    if current_status == "failed":
        status = "failed"
    elif unresolved or "failed" in group_statuses or metadata_incomplete or synthesis_failed:
        status = "degraded"
    else:
        status = "completed"
    pipeline["version"] = "segmented_stage_v1"
    pipeline["unresolved_stages"] = unresolved
    pipeline["status"] = status
    result["stage2_pipeline_status"] = status
    result["stage2_pipeline_version"] = "segmented_stage_v1"
    return status, unresolved


def _clear_segmented_unresolved_severity(
    result: dict[str, Any],
    unresolved_stages: list[str],
) -> None:
    """Prevent compatibility defaults from becoming conclusions for unknown stages."""
    unresolved = set(unresolved_stages)
    if not unresolved:
        return
    for stage in result.get("stage_analysis") or []:
        if not isinstance(stage, dict) or _segmented_stage_code(stage.get("stage")) not in unresolved:
            continue
        stage["severity"] = None
        stage["model_severity"] = None
        trace = stage.get("severity_derivation")
        if isinstance(trace, dict):
            trace["severity"] = None
            trace["model_severity"] = None
            trace["status"] = str(
                stage.get("analysis_status")
                or stage.get("stage_handoff_status")
                or "evidence_blocked"
            )


def _reproject_segmented_stage_results(
    result: dict[str, Any],
    facts: dict[str, Any],
    comparison_eligibility: dict[str, Any] | None,
) -> None:
    """Rebuild code-owned Stage2 projections from locked Stage1 facts.

    A segmented result can enter through the live runner or through
    ``--analysis-result-json``.  The latter may contain a pre-finalization
    snapshot produced by an older build, so relying on the snapshot's copied
    IDs/statuses would make finalization order-dependent.  Reproject only the
    mechanical boundary fields; semantic fields remain the model's input to
    ``_normalize_segmented_stage``.
    """
    if not isinstance(result, dict) or not isinstance(facts, dict):
        return
    if str(result.get("stage2_pipeline_version") or "").strip() != "segmented_stage_v1":
        return
    stages = result.get("stage_analysis")
    if not isinstance(stages, list):
        return
    projected: list[Any] = []
    for item in stages:
        if not isinstance(item, dict):
            projected.append(item)
            continue
        code = _segmented_stage_code(item.get("stage"))
        if code not in _SEGMENTED_STAGE_NAMES:
            projected.append(item)
            continue
        projected.append(
            _normalize_segmented_stage(
                item,
                code,
                facts,
                comparison_eligibility,
            )
        )
    result["stage_analysis"] = projected


def _authoritative_segmented_comparison_contract(
    analysis: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Return the current-run comparison scope before accepting an import.

    ``analysis`` is produced by the code-owned preflight.  ``result`` may be
    a model response or an externally supplied snapshot and is only a
    compatibility fallback for legacy callers that do not carry preflight
    metadata.
    """
    for source in (
        analysis.get("comparison_eligibility") or analysis.get("comparison_contract"),
    ):
        if isinstance(source, dict) and source:
            return copy.deepcopy(source)
    # A segmented provider result is only a compatibility fallback. Normalize
    # it as untrusted input so a model cannot smuggle a code-owned S5 closure
    # into the authoritative handoff by copying ``status_source``.
    for source in (
        result.get("comparison_eligibility") or result.get("comparison_contract"),
    ):
        if isinstance(source, dict) and source:
            return normalize_comparison_contract(source)
    return {}


def finalize_canonical_analysis_result(
    canonical_result: CanonicalAnalysisResult,
    analysis: dict[str, Any],
    analysis_input: str,
    *,
    expected_stage1_hashes: dict[str, str] | None = None,
    raw_snapshot: dict[str, Any] | None = None,
    audit: PostprocessAudit | None = None,
    persist_artifacts: bool = True,
) -> dict[str, Any]:
    """Apply deterministic finalization to one immutable canonical snapshot.

    This boundary never parses provider aliases and never calls a model.  It
    is therefore safe for code-only replay after a gate, resolver, validation,
    or report change.
    """
    artifact_dir = _analysis_artifact_dir(analysis) if persist_artifacts else None
    if artifact_dir is not None:
        artifact_dir.mkdir(parents=True, exist_ok=True)
    if audit is None and artifact_dir is not None:
        audit = PostprocessAudit()
    validated_snapshot = canonical_result.to_dict()
    if artifact_dir is not None:
        write_json(artifact_dir / "validated_normalized_result.json", validated_snapshot)
    normalized = canonical_result.to_dict()
    segmented = str(normalized.get("stage2_pipeline_version") or "").strip() == "segmented_stage_v1"

    if segmented:
        apply_segmented_postprocess_chain(normalized, analysis, audit=audit)
    else:
        apply_postprocess_chain(normalized, analysis, audit=audit)

    if segmented:
        if audit is None:
            _status, unresolved = _refresh_segmented_pipeline_status(normalized)
            _clear_segmented_unresolved_severity(normalized, unresolved)
        else:
            _status, unresolved = audit.run(
                normalized,
                "postprocess.segmented.refresh_pipeline_status",
                _refresh_segmented_pipeline_status,
                normalized,
            )
            audit.run(
                normalized,
                "postprocess.segmented.clear_unresolved_severity",
                _clear_segmented_unresolved_severity,
                normalized,
                unresolved,
            )

    def audited_step(rule: str, function: Any, *args: Any) -> None:
        if audit is None:
            function(*args)
        else:
            audit.run(normalized, rule, function, *args)

    audited_step(
        "postprocess.reconcile_stage_evidence_links",
        reconcile_stage_evidence_links,
        normalized,
    )
    validate_evidence_alignment(normalized)
    validate_stage_ownership(normalized)
    audited_step("postprocess.sanitize_health_recommendations", sanitize_health_recommendations, normalized, analysis_input)
    audited_step(
        "postprocess.sanitize_child_toothpaste_recommendations",
        sanitize_child_toothpaste_recommendations,
        normalized,
        analysis_input,
    )
    audited_step("postprocess.stabilize_improvement_priorities.tail_1", stabilize_improvement_priorities, normalized)
    audited_step("postprocess.ground_improvement_evidence", ground_improvement_evidence, normalized)
    audited_step("postprocess.stabilize_improvement_priorities.tail_2", stabilize_improvement_priorities, normalized)
    validate_analysis_dimensions(normalized)
    validate_recommendation_safety(normalized, analysis_input)
    validate_creator_script_language(normalized, analysis_input)
    audited_step("postprocess.remove_unverified_brand_models", remove_unverified_brand_models, normalized, analysis)
    audited_step("postprocess.clamp_result_time_ranges", _clamp_result_time_ranges, normalized, analysis)
    audited_step("postprocess.materialize_global_diagnosis", materialize_global_diagnosis, normalized, analysis)
    validate_quality_contract(normalized, analysis)
    final_link_issues = stage_evidence_link_issues(normalized)
    if final_link_issues:
        raise SystemExit("最终阶段证据链接合同无效：" + "；".join(final_link_issues))
    stage1_hashes = expected_stage1_hashes or {}
    immutability_issues = stage_evidence_immutability_issues(
        normalized,
        stage1_hashes,
        require_snapshot=bool(stage1_hashes),
    )
    if immutability_issues:
        raise SystemExit("Stage1 证据在下游流程中发生未授权变化：" + "；".join(immutability_issues))

    if artifact_dir is not None and audit is not None:
        source_snapshot = copy.deepcopy(raw_snapshot) if isinstance(raw_snapshot, dict) else validated_snapshot
        field_sources = build_field_sources(
            source_snapshot,
            validated_snapshot,
            normalized,
            audit.changes,
            truncated=audit.truncated,
        )
        replay_context_path = artifact_dir / "analysis_replay_context.json"
        write_json(replay_context_path, analysis)
        provenance = {
            "schema_version": 2,
            "result_contract": ANALYSIS_RESULT_CONTRACT.metadata(),
            "raw_model_response": "raw_model_response.json" if raw_snapshot is not None else None,
            "raw_model_response_format": (
                str(raw_snapshot.get("source_format") or "provider_response")
                if isinstance(raw_snapshot, dict)
                else None
            ),
            "validated_normalized_result": "validated_normalized_result.json",
            # The replay gate validates the bytes on disk, not the compact
            # in-memory canonical digest.  write_json intentionally uses
            # pretty-printed JSON, so these two serializations have different
            # byte hashes even when they carry the same object.
            "validated_normalized_sha256": _sha256_file(
                artifact_dir / "validated_normalized_result.json"
            ),
            "final_derived_result": "final_derived_result.json",
            "field_change_log": "postprocess_change_log.json",
            "change_count": len(audit.changes),
            "change_log_truncated": audit.truncated,
            "field_sources": field_sources,
            "replay_context": replay_context_path.name,
            "replay_context_sha256": _sha256_file(replay_context_path),
            "analysis_input_sha256": hashlib.sha256(analysis_input.encode("utf-8")).hexdigest(),
        }
        normalized["postprocess_provenance"] = provenance
        change_log = audit.as_dict()
        change_log["field_sources"] = field_sources
        write_json(artifact_dir / "postprocess_change_log.json", change_log)
        write_json(artifact_dir / "final_derived_result.json", normalized)
    return normalized


def finalize_analysis_result(
    result: dict[str, Any],
    analysis: dict[str, Any],
    analysis_input: str,
    locked_video_understanding: dict[str, Any] | None = None,
    *,
    persist_artifacts: bool = True,
) -> dict[str, Any]:
    """所有 LLM 结果入口共用的规范化、修补和校验链。

    ``persist_artifacts=False`` is an in-memory preflight used only to select
    optional Phase C work. It cannot create or overwrite lifecycle artifacts;
    the publish pass remains the sole durable finalization boundary.
    """
    artifact_dir = _analysis_artifact_dir(analysis) if persist_artifacts else None
    if artifact_dir is not None:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        _write_raw_model_response(artifact_dir, result=result)
    audit = PostprocessAudit() if artifact_dir is not None else None
    raw_snapshot = copy.deepcopy(result)

    segmented_pipeline = (
        str(analysis.get("stage2_pipeline_version") or "").strip() == "segmented_stage_v1"
        or str(result.get("stage2_pipeline_version") or "").strip() == "segmented_stage_v1"
    )
    analysis_comparison_contract = _authoritative_segmented_comparison_contract(analysis, result)
    if (
        segmented_pipeline
        and isinstance(analysis_comparison_contract, dict)
        and analysis_comparison_contract
    ):
        # The preflight contract belongs to the current run, not to a model
        # response or an imported result file. Keep the result fields as a
        # compatibility view, but replace them before reprojection and
        # normalization so an external artifact cannot close or reopen a
        # comparison scope.
        result["comparison_contract"] = copy.deepcopy(analysis_comparison_contract)
        result["comparison_eligibility"] = copy.deepcopy(analysis_comparison_contract)

    trusted_stage1_acquisition: dict[str, dict[str, Any]] = {}
    trusted_stage1_recovery: dict[str, dict[str, Any]] = {}
    trusted_stage1_qualification: dict[str, dict[str, Any]] = {}
    trusted_stage1_coverage_audit: dict[str, dict[str, Any]] = {}
    if locked_video_understanding:
        before = copy.deepcopy(result) if audit is not None else None
        result["video_understanding"] = locked_video_understanding
        for role in ("benchmark", "creator"):
            side = locked_video_understanding.get(role)
            acquisition_metadata = side.get("stage1_acquisition") if isinstance(side, dict) else None
            if (
                isinstance(acquisition_metadata, dict)
                and acquisition_metadata.get("source") == "pipeline"
            ):
                trusted_stage1_acquisition[role] = acquisition_metadata
            metadata = side.get("stage1_recovery") if isinstance(side, dict) else None
            if isinstance(metadata, dict) and metadata.get("source") == "pipeline":
                trusted_stage1_recovery[role] = metadata
            qualification_metadata = side.get("stage1_qualification") if isinstance(side, dict) else None
            if (
                isinstance(qualification_metadata, dict)
                and qualification_metadata.get("source") == "pipeline"
            ):
                trusted_stage1_qualification[role] = qualification_metadata
            audit_metadata = side.get("stage1_coverage_audit") if isinstance(side, dict) else None
            if (
                isinstance(audit_metadata, dict)
                and audit_metadata.get("source") == "pipeline"
            ):
                trusted_stage1_coverage_audit[role] = audit_metadata
        if audit is not None:
            audit.record(before, result, "pipeline.lock_video_understanding")

        # The same deterministic projection must run for live results and
        # imported/offline snapshots.  Otherwise a snapshot created before a
        # comparison-scope fix can lose code-owned IDs and fail in finalization
        # even though the locked Stage1 ledger is valid.
        _reproject_segmented_stage_results(
            result,
            locked_video_understanding,
            analysis_comparison_contract
            or result.get("comparison_eligibility")
            or result.get("comparison_contract")
            or {},
        )

    before_normalize = copy.deepcopy(result) if audit is not None else None
    normalized = normalize_analysis_result(
        result,
        trusted_stage1_acquisition=trusted_stage1_acquisition,
        trusted_stage1_recovery=trusted_stage1_recovery,
        trusted_stage1_qualification=trusted_stage1_qualification,
        trusted_stage1_coverage_audit=trusted_stage1_coverage_audit,
        allow_trusted_pipeline_metadata=bool(locked_video_understanding),
    )
    if locked_video_understanding:
        normalized_sides = normalized.get("video_understanding")
        if isinstance(normalized_sides, dict):
            for role in ("benchmark", "creator"):
                locked_side = locked_video_understanding.get(role)
                normalized_side = normalized_sides.get(role)
                if isinstance(locked_side, dict) and isinstance(normalized_side, dict):
                    # normalize_video_understanding intentionally ignores
                    # model-shaped pipeline flags; restore the trusted runtime
                    # value at the same handoff boundary as the other Stage1
                    # provenance fields.
                    normalized_side["evidence_budget_exceeded"] = bool(
                        locked_side.get("evidence_budget_exceeded") is True
                    )
    segmented = segmented_pipeline or str(normalized.get("stage2_pipeline_version") or "").strip() == "segmented_stage_v1"
    if segmented:
        normalized["stage2_pipeline_version"] = "segmented_stage_v1"
        unresolved = _segmented_stage_unresolved(normalized.get("stage_analysis") or [])
        _clear_segmented_unresolved_severity(normalized, unresolved)
    if audit is not None:
        audit.record(before_normalize, normalized, "pipeline.normalize_analysis_result")
    audio_assessment = analysis.get("audio_assessment") if isinstance(analysis, dict) else {}
    native_audio = bool((audio_assessment or {}).get("native_audio_analysis", True))
    if audit is None:
        sanitize_audio_observations(
            normalized,
            native_audio,
            preserve_stage1_facts=bool(locked_video_understanding),
        )
    else:
        audit.run(
            normalized,
            "pipeline.sanitize_audio_observations",
            sanitize_audio_observations,
            normalized,
            native_audio,
            preserve_stage1_facts=bool(locked_video_understanding),
        )
    try:
        validate_normalized_analysis_contract(normalized)
    except AnalysisContractError as exc:
        raise SystemExit(str(exc)) from exc
    expected_stage1_hashes: dict[str, str] = {}
    if analysis.get("stage_evidence_contract_required") is True:
        for role in ("benchmark", "creator"):
            side = normalized.get("video_understanding", {}).get(role)
            issues = stage_evidence_contract_issues(side, require_version=True)
            if issues:
                raise SystemExit(f"{role} Stage1 阶段证据合同无效：" + "；".join(issues))
            snapshot_issues = stage_evidence_snapshot_issues(side, require_snapshot=True)
            if snapshot_issues:
                raise SystemExit(f"{role} Stage1 证据集未冻结或已损坏：" + "；".join(snapshot_issues))
            expected_stage1_hashes[role] = str(side.get("evidence_set_sha256") or "")

    canonical_result = CanonicalAnalysisResult.from_mapping(normalized)
    return finalize_canonical_analysis_result(
        canonical_result,
        analysis,
        analysis_input,
        expected_stage1_hashes=expected_stage1_hashes,
        raw_snapshot=raw_snapshot,
        audit=audit,
        persist_artifacts=persist_artifacts,
    )


def _mark_legacy_import_result(
    normalized: dict[str, Any],
    *,
    source_path: Path,
) -> dict[str, Any]:
    """Keep imported historical output visible without treating it as grounded."""
    source_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    normalized["analysis_import_mode"] = "legacy"
    normalized["legacy_import"] = {
        "source_path": str(source_path),
        "source_sha256": source_digest,
        "status": "audit_only",
        "reason": "外部旧结果未经过当前 Stage1/Stage2 事实合同，只能用于历史审计。",
    }
    normalized["stage2_pipeline_status"] = "degraded"
    normalized["stage2_candidate_status"] = "degraded"
    normalized["analysis_run_state"] = "degraded"
    for stage in normalized.get("stage_analysis", []):
        if not isinstance(stage, dict):
            continue
        stage["severity"] = None
        stage["model_severity"] = None
        stage["stage_evidence_gate"] = {
            "status": "legacy",
            "analysis_allowed": False,
            "reason_code": "legacy_import",
            "reason": "历史导入结果不属于当前 grounded 分析。",
            "source": "legacy_import",
        }
        stage["analysis_status"] = "legacy_evidence_contract"
        stage["analysis_reason"] = "历史导入结果仅供审计，未发布当前 severity。"
        stage["severity_derivation"] = {
            "status": "legacy",
            "severity": None,
            "model_severity": None,
            "resolver": "legacy_import_block",
            "phase_c_candidate": False,
            "constraints": [],
            "reason": "历史导入结果不产生当前 severity。",
        }
    return normalized


def _legacy_import_envelope(raw: dict[str, Any]) -> dict[str, Any]:
    """Project an old result into an audit-only envelope without revalidating it.

    Historical results deliberately do not have to satisfy the current
    normalized contract. Keep only fields the runtime projection understands,
    provide harmless empty shapes for report consumers, and let the legacy
    marker clear all publishable severity before projection.
    """
    envelope = {
        field: copy.deepcopy(raw[field])
        for field in ANALYSIS_RESULT_CONTRACT.projection_fields
        if field in raw
    }
    defaults: dict[str, Any] = {
        "one_line_summary": "历史结果，仅供审计。",
        "executive_summary": "",
        "holistic_assessment": {},
        "product_visibility": {},
        "loop_closure": {},
        "video_understanding": {},
        "stage_analysis": [],
        "improvements": [],
    }
    for field, default in defaults.items():
        envelope.setdefault(field, default)
    if not isinstance(envelope.get("stage_analysis"), list):
        envelope["stage_analysis"] = []
    if not isinstance(envelope.get("improvements"), list):
        envelope["improvements"] = []
    if not isinstance(envelope.get("video_understanding"), dict):
        envelope["video_understanding"] = {}
    return envelope


def merge_analysis_result(
    analysis: dict[str, Any],
    result_path: Path,
    analysis_input: str,
    *,
    legacy_import: bool = False,
) -> None:
    """把外部 analysis_result.json 经唯一处理链后合并入 analysis。"""
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"analysis result JSON 无法读取或解析：{result_path}") from exc
    if not isinstance(result, dict):
        raise SystemExit("analysis result JSON 必须是对象，不能是数组或标量。")
    if legacy_import:
        artifact_dir = _analysis_artifact_dir(analysis)
        if artifact_dir is not None:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            _write_raw_model_response(
                artifact_dir,
                result=result,
                source_format="legacy_import_result",
                overwrite=True,
            )
        normalized = _mark_legacy_import_result(
            _legacy_import_envelope(result),
            source_path=result_path,
        )
        apply_finalized_analysis_result(analysis, normalized, result_path)
        return
    segmented = str(result.get("stage2_pipeline_version") or "").strip() == "segmented_stage_v1"
    if segmented:
        analysis["stage2_pipeline_version"] = "segmented_stage_v1"
        analysis["stage2_candidate_status"] = str(
            result.get("stage2_candidate_status")
            or result.get("stage2_pipeline_status")
            or "degraded"
        ).strip().lower()
        analysis["stage_evidence_contract_required"] = True
    stages = result.get("stage_analysis") if isinstance(result.get("stage_analysis"), list) else []
    if segmented:
        # Older segmented artifacts may predate the Stage3 deterministic
        # fallback and contain an empty improvements list.  Replays must not
        # fail before the new finalizer can classify unresolved stages.
        improvements = result.get("improvements")
        if not isinstance(improvements, list) or not improvements:
            result["improvements"] = _deterministic_improvement(stages)
    has_structured_s5 = any(
        isinstance(stage, dict)
        and str(stage.get("stage") or "").upper().startswith("S5")
        and any(isinstance(stage.get(f"{role}_s5"), dict) for role in ("creator", "benchmark"))
        for stage in stages
    )
    if has_structured_s5:
        # 外部导入也必须使用和主链相同的 S5 来源门禁，避免入口不同导致背书结论漂移。
        analysis["s5_source_signals_required"] = True
    phase_c_review = result.get("phase_c_review")
    artifact_dir = _analysis_artifact_dir(analysis)
    if artifact_dir is not None:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        _write_raw_model_response(
            artifact_dir,
            result=result,
            source_format="external_result",
            overwrite=True,
        )
    if segmented:
        normalized = finalize_analysis_result(
            result,
            analysis,
            analysis_input,
            locked_video_understanding=result.get("video_understanding"),
        )
    else:
        normalized = finalize_analysis_result(result, analysis, analysis_input)
    if isinstance(phase_c_review, dict):
        normalized["phase_c_review"] = phase_c_review
        _refresh_final_derived_artifact(analysis, normalized, ("phase_c_review",))
    apply_finalized_analysis_result(analysis, normalized, result_path)


# ---------------------------------------------------------------------------
# LLM 调用 + 校验主入口
# ---------------------------------------------------------------------------

def fetch_json_completion(
    args: argparse.Namespace,
    api_key: str,
    payload_path: Path,
    raw_path: Path,
    max_attempts: int = 1,
    request_max_time_seconds: int | None = None,
    request_retries: int | None = None,
    response_meta: dict[str, Any] | None = None,
) -> str:
    """调用 LLM 并校验 JSON；默认不重复已完成但格式错误的语义请求。

    连接中断、缺少 ``[DONE]`` 等传输故障由 ``call_llm_api`` 在同一逻辑请求内
    有界重试。调用方只有在明确需要独立的新 completion 时才提高 ``max_attempts``；
    默认一次，避免对 temperature=0 的同一合同盲目重复付费。
    """
    last_text = ""
    logical_request_id = uuid.uuid4().hex
    outer_retry_reason = ""
    retry_reasons: list[str] = []
    try:
        payload_text = payload_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"LLM request payload is unavailable: {payload_path}") from exc
    # The caller may have created this file in the run directory for debugging,
    # but the transport must only receive a short-lived copy.
    payload_path.unlink(missing_ok=True)
    for attempt in range(max_attempts):
        request_options = {}
        if request_max_time_seconds is not None:
            request_options["max_time_seconds"] = request_max_time_seconds
        if request_retries is not None:
            request_options["retries"] = request_retries
        with tempfile.TemporaryDirectory(prefix=".llm-request.", dir=payload_path.parent) as request_dir:
            ephemeral_payload = Path(request_dir) / "request.json"
            write_text(ephemeral_payload, payload_text)
            try:
                raw_text = call_llm_api(
                    args.llm_api_url,
                    api_key,
                    ephemeral_payload,
                    raw_path,
                    budget=getattr(args, "_resource_budget", None),
                    request_id=logical_request_id,
                    initial_retry_reason=outer_retry_reason,
                    response_meta=response_meta,
                    **request_options,
                )
            except SystemExit as exc:
                error_text = str(exc)
                if _is_permanent_llm_error(error_text):
                    # HTTP 4xx responses are configuration, authorization, or
                    # request-contract failures. Re-entering the outer retry
                    # loop would repeat the same rejected request and spend
                    # model quota without changing the outcome.
                    raise
                if any(
                    marker in error_text
                    for marker in (
                        "total wall time budget exceeded",
                        "LLM call budget exceeded",
                        "total upload budget exceeded",
                        "download budget exceeded",
                        "cost estimate budget exceeded",
                        "insufficient wall time for another LLM transport attempt",
                    )
                ):
                    # Shared run-budget exhaustion is a hard stop. Re-entering
                    # the outer retry loop would only spend more time while
                    # the same budget remains unavailable.
                    raise
                # 底层已做同一 SSE 请求的传输重试；仍失败时重取完整响应，不能让单次网络中断终止整条 pipeline。
                if attempt + 1 >= max_attempts:
                    if response_meta is not None:
                        response_meta.update(
                            {
                                "logical_request_id": logical_request_id,
                                "completion_attempts": attempt + 1,
                                "retry_reasons": [*retry_reasons, error_text[:200]],
                                "status": "failed",
                            }
                        )
                    raise
                outer_retry_reason = error_text[:200]
                retry_reasons.append(outer_retry_reason)
                time.sleep(5 * (attempt + 1))
                continue
        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError:
            reason = "invalid JSON in provider envelope"
            retry_reasons.append(reason)
            if response_meta is not None:
                response_meta.update(
                    {
                        "status": "invalid_json",
                        "logical_request_id": logical_request_id,
                        "completion_attempts": attempt + 1,
                        "retry_reasons": list(retry_reasons),
                        "json_valid": False,
                    }
                )
            if attempt + 1 >= max_attempts:
                break
            outer_retry_reason = reason
            time.sleep(5 * (attempt + 1))
            continue
        try:
            last_text = extract_chat_completion_text(raw)
        except SystemExit as exc:
            reason = f"provider response missing text output: {exc}"
            retry_reasons.append(reason)
            if response_meta is not None:
                response_meta.update(
                    {
                        "status": "invalid_response",
                        "logical_request_id": logical_request_id,
                        "completion_attempts": attempt + 1,
                        "retry_reasons": list(retry_reasons),
                        "json_valid": True,
                    }
                )
            if attempt + 1 >= max_attempts:
                raise SystemExit(reason) from exc
            outer_retry_reason = reason
            time.sleep(5 * (attempt + 1))
            continue
        if response_meta is not None:
            choices = raw.get("choices") if isinstance(raw.get("choices"), list) else []
            choice = choices[0] if choices and isinstance(choices[0], dict) else {}
            response_meta.update(
                {
                    "status": "completed",
                    "logical_request_id": logical_request_id,
                    "completion_attempts": attempt + 1,
                    "retry_reasons": list(retry_reasons),
                    "finish_reason": str(choice.get("finish_reason") or "").strip().lower(),
                    "usage": copy.deepcopy(raw.get("usage")) if isinstance(raw.get("usage"), dict) else {},
                    "json_valid": True,
                }
            )
        try:
            parse_json_text(last_text)
            return last_text
        except SystemExit as exc:
            reason = "invalid JSON in completed model response"
            retry_reasons.append(reason)
            if response_meta is not None:
                response_meta.update(
                    {
                        "status": "invalid_json",
                        "json_valid": False,
                        "retry_reasons": list(retry_reasons),
                        "invalid_content_sha256": hashlib.sha256(
                            last_text.encode("utf-8", errors="replace")
                        ).hexdigest(),
                        "invalid_content_chars": len(last_text),
                        "invalid_json_error": str(exc)[:500],
                    }
                )
            # 输出预算截断（finish_reason=length）重发也会在同处截断，直接交给 repair，不徒劳重取。
            if str(raw.get("choices", [{}])[0].get("finish_reason")) == "length":
                break
            if attempt + 1 >= max_attempts:
                break
            outer_retry_reason = reason
    return last_text


def _stable_digest(value: Any) -> str:
    """生成跨运行稳定的内容摘要；缓存 key 只依赖可审计输入。"""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _preprocess_artifact_content_digest(value: Any) -> str:
    """Digest artifact content, excluding volatile filesystem metadata."""
    if not isinstance(value, dict):
        return _stable_digest(value)
    files_value = value.get("files")
    if isinstance(files_value, dict):
        files = [
            {
                "relative_path": relative_path,
                **(metadata if isinstance(metadata, dict) else {}),
            }
            for relative_path, metadata in files_value.items()
        ]
    elif isinstance(files_value, list):
        files = files_value
    else:
        files = []
    stable_files = []
    for item in files:
        if not isinstance(item, dict):
            continue
        stable_files.append(
            {
                "relative_path": str(item.get("relative_path") or ""),
                "sha256": str(item.get("sha256") or ""),
                "size": item.get("size", item.get("size_bytes")),
            }
        )
    return _stable_digest(
        {
            "version": value.get("version", value.get("schema_version")),
            "files": sorted(stable_files, key=lambda item: item["relative_path"]),
        }
    )


def _source_video_hash(analysis: dict[str, Any], role: str) -> str:
    videos = analysis.get("videos") if isinstance(analysis.get("videos"), dict) else {}
    info = videos.get(role) if isinstance(videos, dict) else None
    fingerprint = info.get("preprocess_fingerprint") if isinstance(info, dict) else None
    source = fingerprint.get("source_video") if isinstance(fingerprint, dict) else None
    return str(source.get("sha256") or "") if isinstance(source, dict) else ""


def _cache_path(run_dir: Path, namespace: str, key: dict[str, Any]) -> Path | None:
    """缓存归属输出目录父级，避免依赖本地 run 名称，也便于未来线上换存储实现。"""
    source_hash = str(key.get("source_video_sha256") or "")
    if not source_hash:
        return None
    return run_dir.parent / namespace / f"{_stable_digest(key)}.json"


def _read_cache_result(
    path: Path | None,
    result_key: str,
    expected_key: dict[str, Any] | None = None,
    validator: Any = None,
) -> dict[str, Any] | None:
    cached = _read_cache_record(path, result_key, expected_key, validator)
    return cached.get(result_key) if isinstance(cached, dict) else None


def _read_cache_record(
    path: Path | None,
    result_key: str,
    expected_key: dict[str, Any] | None = None,
    validator: Any = None,
) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(cached, dict):
        return None
    if cached.get("cache_record_schema_version") != CACHE_RECORD_SCHEMA_VERSION:
        return None
    if cached.get("completion_status") != "completed":
        return None
    if cached.get("result_schema_sha256") != schema_sha256():
        return None
    if expected_key is not None and any(cached.get(key) != value for key, value in expected_key.items()):
        return None
    result = cached.get(result_key)
    if not isinstance(result, dict):
        return None
    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    artifact = cached.get("artifact")
    if not isinstance(artifact, dict):
        return None
    if artifact.get("size_bytes") != len(serialized.encode("utf-8")):
        return None
    if artifact.get("sha256") != hashlib.sha256(serialized.encode("utf-8")).hexdigest():
        return None
    stage_fact_artifacts = cached.get("stage_fact_artifacts")
    if stage_fact_artifacts is not None:
        if not isinstance(stage_fact_artifacts, dict):
            return None
        expected_artifacts_digest = cached.get("stage_fact_artifacts_sha256")
        if expected_artifacts_digest != _stable_digest(stage_fact_artifacts):
            return None
    if validator is not None and not validator(result):
        return None
    return cached


def _stage_fact_artifacts_for_cache(
    run_dir: Path,
    role: str,
    fact_result: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Snapshot only provider artifacts referenced by the current ledger.

    A run directory may contain artifacts from an earlier failed or differently
    scoped attempt. Directory globbing would let those stale files hitchhike
    into a new cache record even though the canonical result never consumed
    them.
    """
    artifacts: dict[str, dict[str, Any]] = {}
    prefix = f"stage1_provider_{role}_"
    acquisition = (
        fact_result.get("stage1_acquisition")
        if isinstance(fact_result.get("stage1_acquisition"), dict)
        else {}
    )
    names = list(dict.fromkeys(
        str(item.get("artifact") or "").strip()
        for item in acquisition.get("provider_artifacts") or []
        if isinstance(item, dict) and str(item.get("artifact") or "").strip()
    ))
    for name in names:
        if not name.startswith(prefix) or Path(name).name != name or not name.endswith(".json"):
            raise ValueError(f"invalid Stage1 provider artifact name in manifest: {name}")
        path = run_dir / name
        if not path.is_file():
            raise ValueError(f"Stage1 provider artifact missing from current run: {name}")
        try:
            value = read_stage_fact_artifact(path)
        except StageFactArtifactError as exc:
            raise ValueError(f"Stage1 provider artifact invalid: {name}: {exc}") from exc
        artifacts[path.name] = value
    return artifacts


def _restore_stage_fact_artifacts_from_cache(
    cache_record: dict[str, Any],
    run_dir: Path,
    role: str,
) -> bool:
    value = cache_record.get("stage_fact_artifacts")
    if not isinstance(value, dict) or not value:
        return False
    prefix = f"stage1_provider_{role}_"
    restored = 0
    for name, artifact in value.items():
        safe_name = str(name or "")
        if (
            not safe_name.startswith(prefix)
            or Path(safe_name).name != safe_name
            or not safe_name.endswith(".json")
            or not isinstance(artifact, dict)
        ):
            return False
        write_json(run_dir / safe_name, artifact)
        restored += 1
    return restored > 0


def _current_stage1_provider_artifacts(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one ordered manifest entry for every artifact used by this ledger."""
    acquisition = facts.get("stage1_acquisition") if isinstance(facts.get("stage1_acquisition"), dict) else {}
    by_name: dict[str, dict[str, Any]] = {
        str(item.get("artifact")): copy.deepcopy(item)
        for item in acquisition.get("provider_artifacts") or []
        if isinstance(item, dict) and str(item.get("artifact") or "").strip()
    }
    qualification = facts.get("stage1_qualification") if isinstance(facts.get("stage1_qualification"), dict) else {}
    for item in qualification.get("group_records") or []:
        if not isinstance(item, dict) or not str(item.get("provider_artifact") or "").strip():
            continue
        name = str(item["provider_artifact"])
        by_name[name] = {
            "phase": str(item.get("phase") or "B").strip().upper(),
            "artifact": name,
            "status": item.get("status", "completed"),
            "execution_source": item.get("execution_source", "provider"),
            "request_identity_sha256": item.get("request_identity_sha256", ""),
            "response_sha256": item.get("response_sha256", ""),
            "completion_attempts": item.get("completion_attempts", 0),
            "failure_kind": item.get("failure_kind", ""),
            "cause_type": item.get("cause_type", ""),
            "failure_reason": item.get("failure_reason", ""),
        }
    recovery = facts.get("stage1_recovery") if isinstance(facts.get("stage1_recovery"), dict) else {}
    if str(recovery.get("provider_artifact") or "").strip():
        name = str(recovery["provider_artifact"])
        by_name[name] = {
            "phase": "C",
            "artifact": name,
            "status": recovery.get("provider_status", "completed"),
            "execution_source": recovery.get("execution_source", "provider"),
            "request_identity_sha256": recovery.get("request_identity_sha256", ""),
            "response_sha256": recovery.get("response_sha256", ""),
            "completion_attempts": recovery.get("completion_attempts", 0),
            "failure_reason": recovery.get("failure_reason", ""),
        }
    phase_order = {"A": 0, "B": 1, "C": 2, "D": 3}
    return sorted(
        by_name.values(),
        key=lambda item: (
            phase_order.get(str(item.get("phase") or "").upper(), 99),
            str(item.get("artifact") or ""),
        ),
    )


def _write_cache_result(path: Path | None, record: dict[str, Any]) -> None:
    if path is None:
        return
    result_key = next((key for key in ("foundation", "fact_result") if key in record), None)
    if result_key is None or not isinstance(record.get(result_key), dict):
        raise ValueError("cache record must contain a structured result")
    result = record[result_key]
    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    record = {
        **record,
        "cache_record_schema_version": CACHE_RECORD_SCHEMA_VERSION,
        "completion_status": "completed",
        "result_schema_sha256": schema_sha256(),
        "artifact": {
            "size_bytes": len(serialized.encode("utf-8")),
            "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        },
    }
    if isinstance(record.get("stage_fact_artifacts"), dict):
        record["stage_fact_artifacts_sha256"] = _stable_digest(record["stage_fact_artifacts"])
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, record)


def _git_commit_sha() -> str:
    """Return the current code identity without making cache reuse depend on git availability."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def _sha256_file(path: Path) -> str:
    if not path.is_file():
        return "missing"
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cache_reference_digests() -> dict[str, str]:
    """Hash code and reference material that changes the meaning of an LLM request."""
    candidates = [
        _REPO_ROOT / "scripts" / "flayr_core" / "llm" / "pipeline.py",
        _REPO_ROOT / "scripts" / "flayr_core" / "llm" / "payload.py",
        _REPO_ROOT / "scripts" / "flayr_core" / "market.py",
        _REPO_ROOT / "scripts" / "flayr_core" / "structure_modules.py",
        _REPO_ROOT / "structure_library_full.md",
        _REPO_ROOT / "QA-RULES.md",
    ]
    references_dir = _REPO_ROOT / "references"
    if references_dir.is_dir():
        candidates.extend(sorted(path for path in references_dir.iterdir() if path.is_file()))
    return {
        str(path.relative_to(_REPO_ROOT)): _sha256_file(path)
        for path in candidates
        if path.exists()
    }


def _product_context_digest(analysis: dict[str, Any]) -> str:
    product = analysis.get("product") if isinstance(analysis.get("product"), dict) else {}
    brand = analysis.get("brand_proposition") if isinstance(analysis.get("brand_proposition"), dict) else {}
    return _stable_digest({"product": product, "brand_proposition": brand})


def _product_foundation_cache_key(args: argparse.Namespace, analysis: dict[str, Any]) -> dict[str, Any]:
    model = judgment_model(args)
    payload = build_product_foundation_payload(model, analysis)
    return {
        "cache_schema_version": PRODUCT_FOUNDATION_CACHE_SCHEMA_VERSION,
        "llm_model": model,
        "llm_api_url": str(args.llm_api_url or ""),
        "temperature": 0.0,
        "product_context_digest": _product_context_digest(analysis),
        "request_payload_sha256": _stable_digest(identity_value(payload)),
    }


def _video_fact_cache_key(args: argparse.Namespace, analysis: dict[str, Any], role: str) -> dict[str, Any]:
    foundation = analysis.get("product_foundation") if isinstance(analysis.get("product_foundation"), dict) else {}
    video_info = analysis.get("videos", {}).get(role, {}) if isinstance(analysis.get("videos"), dict) else {}
    preprocess_fingerprint = video_info.get("preprocess_fingerprint") if isinstance(video_info, dict) else {}
    preprocess_artifacts = video_info.get("preprocess_artifacts") if isinstance(video_info, dict) else {}
    return {
        "cache_schema_version": VIDEO_FACT_CACHE_SCHEMA_VERSION,
        "source_video_sha256": _source_video_hash(analysis, role),
        "preprocess_fingerprint_sha256": _stable_digest(preprocess_fingerprint),
        "preprocess_artifacts_sha256": _preprocess_artifact_content_digest(preprocess_artifacts),
        "role": role,
        "judgment_model": judgment_model(args),
        "vision_model": vision_model(args),
        "llm_api_url": str(args.llm_api_url or ""),
        "foundation_digest": _stable_digest(foundation),
        "product_context_digest": _product_context_digest(analysis),
        "code_commit": _git_commit_sha(),
        "reference_digests": _cache_reference_digests(),
        "llm_image_limit": int(getattr(args, "llm_image_limit", 0) or 0),
        "target_market": str(((analysis.get("product") or {}).get("target_market") or "auto")),
        "temperature": 0.0,
        "seed": None,
    }


def _is_valid_foundation_cache(value: dict[str, Any]) -> bool:
    return bool(
        isinstance(value.get("category_profile"), dict)
        or isinstance(value.get("product_profile"), dict)
    )


def _video_fact_cache_stage1_coverage_issues(value: dict[str, Any]) -> list[str]:
    """Validate a cached Stage1-C result without requiring global coverage.

    Stage1-C is intentionally targeted.  A cache containing only the stages
    selected for one bounded recovery is complete for that recovery, even
    though the legacy ``stage1_coverage_audit_issues(side)`` helper quite
    correctly rejects it when asked to prove all six stages.  Cache reuse must
    also accept a typed unresolved recovery: retrying it would violate the
    one-recovery budget and turn an honest limitation into repeated LLM calls.
    """
    if value.get("stage_evidence_contract_version") != STAGE_EVIDENCE_CONTRACT_VERSION:
        return []
    recovery = value.get("stage1_recovery")
    recovery = recovery if isinstance(recovery, dict) else {}
    status = str(recovery.get("status") or "").strip().lower()
    if status == "not_needed":
        return []
    if status not in {"focused_recovery", "focused_recovery_with_unresolved"}:
        return stage1_coverage_audit_issues(value)

    valid_stages = set(stage_codes())
    targets = list(dict.fromkeys(
        code
        for code in (normalize_stage_code(item) for item in recovery.get("target_stages") or [])
        if code in valid_stages
    ))
    unresolved = set(
        code
        for code in (normalize_stage_code(item) for item in recovery.get("unresolved_stages") or [])
        if code in valid_stages
    )
    issues: list[str] = []
    if not targets:
        issues.append("stage1_recovery_targets_missing")
        return issues
    if status == "focused_recovery" and unresolved:
        issues.append("stage1_recovery_unresolved_metadata_mismatch")
    if status == "focused_recovery_with_unresolved" and not unresolved:
        issues.append("stage1_recovery_unresolved_stages_missing")

    audit = normalize_stage1_coverage_audit(
        value.get("stage1_coverage_audit"),
        {
            str(item.get("id") or "").strip()
            for item in value.get("evidence_units") or []
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        },
    )
    audit_targets = set(audit.get("target_stages") or [])
    if audit_targets != set(targets):
        issues.append("stage1_recovery_audit_scope_mismatch")

    for stage in targets:
        stage_issues = stage1_coverage_audit_issues(value, stage)
        if stage in unresolved:
            # The unresolved stage must remain visibly unresolved.  A cache
            # claiming unresolved while the audit is actually closed is
            # metadata corruption, not a reason to silently promote it.
            if not stage_issues:
                issues.append(f"{stage}:stage1_recovery_unresolved_not_observed")
        elif stage_issues:
            issues.extend(stage_issues)
    return list(dict.fromkeys(issues))


def _is_valid_video_fact_cache(role: str, value: dict[str, Any], analysis: dict[str, Any]) -> bool:
    try:
        normalized = normalize_video_fact_result(
            role,
            copy.deepcopy(value),
            analysis,
            allow_trusted_pipeline_metadata=True,
        )
    except (Exception, SystemExit):
        return False
    if normalized.get("stage_evidence_contract_version") == STAGE_EVIDENCE_CONTRACT_VERSION:
        if stage_evidence_snapshot_issues(value, require_snapshot=True):
            return False
        qualification = value.get("stage1_qualification")
        if not isinstance(qualification, dict) or qualification.get("status") != "completed":
            return False
        # Stage1-C is a bounded targeted recovery, so validate its declared
        # scope rather than requiring a second full-video audit.  A typed
        # unresolved result is reusable too; re-running it would exceed the
        # one-recovery budget and conceal the original limitation.
        if _video_fact_cache_stage1_coverage_issues(value):
            return False
    return True


_SEGMENTED_STAGE_NAMES = {
    "S1": "S1 Hook",
    "S2": "S2 产品引出",
    "S3": "S3 使用过程",
    "S4": "S4 效果呈现",
    "S5": "S5 信任放大",
    "S6": "S6 CTA",
}
_SEGMENTED_STAGE_QUESTIONS = {
    "S1": "用户凭什么停下来",
    "S2": "产品是否自然承接并成为解决方案",
    "S3": "使用过程是否把核心卖点演示出来",
    "S4": "目标效果是否被清楚且可信地证明",
    "S5": "信任材料是否真实、相关且可追溯",
    "S6": "用户是否知道下一步如何购买或行动",
}
_SEGMENTED_FLAG_REQUIRED_KEYS = {
    "S1": ("exists", "type", "dims", "landing_met", "landing_reason", "window_evidence", "hook_boundary_seconds", "hook_boundary_reason", "s2_start_signal", "landing_window_leak", "anchors_proposition"),
    "S2": ("exists", "merged_with_s3", "module_type", "handoff_met", "s1_s2_compatible", "product_identity_clear", "product_role_clear", "excluded_or_risky_module", "start_seconds", "end_seconds", "handoff_reason"),
    "S3": ("exists", "module_type", "usage_evidence_state", "usage_process_visible", "result_only_without_process", "mouth_only_or_static", "real_usage_met", "core_selling_point_visible", "process_framing_met", "action_proof_met", "action_target_contact_met", "action_application_change_visible", "critical_action_continuity_met", "scene_mode", "usage_context_fit", "continuity_met", "richness_met", "single_scene_continuity_met", "single_scene_variation_met", "multi_scene_logic_met", "multi_scene_transition_met", "multi_scene_role_adaptation_met", "role_design_met", "role_interaction_met", "distinct_personas_met", "steps_clear_met", "pov_immersive_met", "fake_or_staged", "start_seconds", "end_seconds", "usage_reason"),
    "S4": ("effect_type", "effect_evidence_state", "effect_visible", "effect_salience", "effect_proposition_matched", "comparison_control_met", "closeup_or_focus_met", "visual_difference_observed", "module_constraints_met", "effect_maximized", "requires_close_inspection", "effect_attribution_supported", "result_only_without_process", "process_linked_effect", "tamper_or_cut_risk", "effect_reason"),
    "S5": ("exists", "module_type", "trust_evidence_type", "trust_basis", "trust_source_visible", "trust_source_credible", "trust_claim_specific", "product_relevance_met", "independent_trust_purpose", "duplicates_other_stage", "voice_only", "risky_or_unsupported", "start_seconds", "end_seconds", "trust_reason"),
    "S6": ("exists", "module_type", "direct_order_met", "action_path_clear", "soft_purchase_invitation_met", "offer_or_incentive_clear", "price_anchor_met", "urgency_evidence_met", "gift_stack_met", "guarantee_clear_met", "urgency_met", "product_value_recalled", "module_fit_met", "ending_position_met", "depends_on_valid_s4", "compliance_risk", "start_seconds", "end_seconds", "cta_reason"),
}


def _segmented_stage_code(value: Any) -> str:
    match = re.search(r"S[1-6]", str(value or "").upper())
    return match.group(0) if match else ""


def _segmented_text_items(value: Any, limit: int = 5) -> list[str]:
    """Normalize bounded model text fields without iterating string characters."""
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
    elif str(value or "").strip():
        items = [str(value).strip()]
    else:
        items = []
    return items[:limit]


def _segmented_evidence_range(facts: dict[str, Any], role: str, stage: str, ids: list[str]) -> str:
    side = facts.get(role) if isinstance(facts.get(role), dict) else {}
    units = {
        str(unit.get("id") or ""): unit
        for unit in side.get("evidence_units") or []
        if isinstance(unit, dict)
    }
    parsed = [
        parse_time_range_seconds(units[item].get("time_range"), None)
        for item in ids
        if item in units
    ]
    parsed = [item for item in parsed if item is not None]
    if not parsed:
        return ""
    return f"{format_seconds(min(item[0] for item in parsed))} - {format_seconds(max(item[1] for item in parsed))}"


def _segmented_qualified_units(
    facts: dict[str, Any],
    role: str,
    stage: str,
    ids: list[str],
) -> list[dict[str, Any]]:
    side = facts.get(role) if isinstance(facts.get(role), dict) else {}
    units = {
        str(unit.get("id") or ""): unit
        for unit in side.get("evidence_units") or []
        if isinstance(unit, dict) and str(unit.get("id") or "").strip()
    }
    qualified = qualified_stage_evidence_ids(side, stage)
    return [
        units[evidence_id]
        for evidence_id in ids
        if evidence_id in qualified and evidence_id in units
    ]


def _segmented_side_summary(units: list[dict[str, Any]], role: str, readiness: str) -> str:
    if not units:
        if readiness == "absent":
            return f"{role}该阶段已完成观察，未发现合同要求的明确证据。"
        if readiness == "unknown":
            return f"{role}该阶段证据资格未知，暂不形成正式结论。"
        if readiness == "conflict":
            return f"{role}该阶段证据存在冲突，暂不形成正式结论。"
        return f"{role}该阶段没有可交接的资格化证据。"
    texts: list[str] = []
    for unit in units:
        text = next(
            (
                str(unit.get(field) or "").strip()
                for field in ("information", "visual_fact", "voiceover_zh", "subtitle_fact")
                if str(unit.get(field) or "").strip()
            ),
            "",
        )
        if text and text not in texts:
            texts.append(text)
    return "；".join(texts[:3]) or f"{role}该阶段已锁定证据，但缺少可展示的文字摘要。"


def _segmented_support_status(units: list[dict[str, Any]]) -> str:
    has_visual = any(str(unit.get("visual_fact") or "").strip() for unit in units)
    has_voice = any(
        str(unit.get(field) or "").strip()
        for unit in units
        for field in ("voiceover", "voiceover_zh", "subtitle_fact")
    )
    if has_visual and has_voice:
        return "supported"
    if has_voice:
        return "voice_only"
    if has_visual:
        return "visual_only"
    return "unknown"


def _sanitize_segmented_flag(value: Any, qualified_ids: set[str]) -> Any:
    """Keep semantic flags but strip any unqualified nested evidence IDs."""
    if isinstance(value, dict):
        return {
            key: (
                [str(item).strip() for item in nested if str(item).strip() in qualified_ids]
                if key == "evidence_ids" and isinstance(nested, list)
                else _sanitize_segmented_flag(nested, qualified_ids)
            )
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_segmented_flag(item, qualified_ids) for item in value]
    return value


def _segmented_model_evidence_ids(raw: dict[str, Any], role: str) -> list[str]:
    """Read only explicit Stage2 evidence-ID fields, including legacy nesting.

    Some compatible providers return the two role judgments as nested
    ``benchmark``/``creator`` objects even when the current compact contract
    asks for top-level ``*_evidence_ids``.  The nested alias is still an
    explicit structured field; free-text reasons are intentionally ignored.
    """
    candidates: list[Any] = [raw.get(f"{role}_evidence_ids")]
    nested = raw.get(role)
    if isinstance(nested, dict):
        candidates.append(nested.get("evidence_ids"))
    for key, value in raw.items():
        if str(key).startswith(f"{role}_") and isinstance(value, dict):
            candidates.append(value.get("evidence_ids"))
    ids: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, list):
            continue
        ids.extend(str(item).strip() for item in candidate if str(item).strip())
    return list(dict.fromkeys(ids))


def _segmented_complete_flag(value: Any, stage: str) -> bool:
    if not isinstance(value, dict):
        return False
    required = _SEGMENTED_FLAG_REQUIRED_KEYS.get(stage, ())
    if any(key not in value for key in required):
        return False
    if stage == "S1" and not isinstance(value.get("dims"), dict):
        return False
    return True


def _normalize_segmented_stage(
    raw: dict[str, Any],
    stage: str,
    facts: dict[str, Any],
    comparison_eligibility: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project a small Stage2 judgment into the legacy report shape.

    Only semantic judgment fields and complete stage-specific flags are read
    from the model. Summaries, quotes, support status, ranges, and references
    are rebuilt from the qualified Stage1 ledger so a prompt violation cannot
    reclaim a code-owned field.
    """
    stage_contract = (
        comparison_eligibility.get("stage_eligibility", {}).get(stage)
        if isinstance(comparison_eligibility, dict)
        and isinstance(comparison_eligibility.get("stage_eligibility"), dict)
        and isinstance(comparison_eligibility.get("stage_eligibility", {}).get(stage), dict)
        else {}
    )
    comparison_status = str(stage_contract.get("status") or "").strip().lower()
    scope_closed = comparison_status in {"not_comparable", "not_applicable"}
    output: dict[str, Any] = {
        "stage": _SEGMENTED_STAGE_NAMES[stage],
        "core_question": _SEGMENTED_STAGE_QUESTIONS[stage],
        "stage_state": "unknown",
        "relation": "uncertain",
        "model_gap_magnitude": "uncertain",
        "judgment_reason": str(raw.get("judgment_reason") or raw.get("reason") or "").strip(),
    }
    if scope_closed:
        output["comparison_status"] = (
            "not_applicable" if comparison_status == "not_applicable" else "not_directly_comparable"
        )
        output["model_comparison_status"] = comparison_status
        output["judgment_reason"] = (
            output["judgment_reason"]
            or str(stage_contract.get("basis") or "该阶段不在当前比较合同范围内。").strip()
        )
    role_ids: dict[str, list[str]] = {}
    readiness: dict[str, str] = {}
    for role in ("benchmark", "creator"):
        side = facts.get(role) if isinstance(facts.get(role), dict) else {}
        qualified = qualified_stage_evidence_ids(side, stage)
        key = f"{role}_evidence_ids"
        ids = [item for item in _segmented_model_evidence_ids(raw, role) if item in qualified]
        current_readiness = stage_evidence_readiness(side, stage)
        readiness[role] = current_readiness
        if current_readiness != "present":
            ids = []
        elif scope_closed:
            # Closed comparison scopes still expose the qualified Stage1
            # ledger for audit. They must not depend on the model repeating
            # IDs for a stage that is intentionally not being judged.
            ids = sorted(qualified)
        role_ids[role] = list(dict.fromkeys(ids))
        units = _segmented_qualified_units(facts, role, stage, role_ids[role])
        output[key] = role_ids[role]
        output[f"{role}_time_range"] = _segmented_evidence_range(facts, role, stage, role_ids[role])
        output[f"{role}_summary"] = _segmented_side_summary(units, role, current_readiness)
        output[f"{role}_key_message"] = output[f"{role}_summary"]
        output[f"{role}_visual_evidence"] = [
            str(unit.get("visual_fact") or "").strip()
            for unit in units
            if str(unit.get("visual_fact") or "").strip()
        ]
        output[f"{role}_support_status"] = _segmented_support_status(units)
        output[f"{role}_quote"] = next(
            (
                str(unit.get("voiceover") or "").strip()
                for unit in units
                if str(unit.get("voiceover") or "").strip()
            ),
            "",
        )
        output[f"{role}_quote_zh"] = next(
            (
                str(unit.get("voiceover_zh") or "").strip()
                for unit in units
                if str(unit.get("voiceover_zh") or "").strip()
            ),
            "",
        )

    missing_model_references = not scope_closed and any(
        readiness[role] == "present" and not role_ids[role]
        for role in ("benchmark", "creator")
    )
    if scope_closed:
        output["relation"] = "uncertain"
        output["model_gap_magnitude"] = "uncertain"
        output["stage_state"] = "unknown"
        output["stage_handoff_status"] = (
            "not_applicable" if comparison_status == "not_applicable" else "not_comparable"
        )
    elif missing_model_references:
        output["relation"] = "uncertain"
        output["model_gap_magnitude"] = "uncertain"
        output["stage_state"] = "unknown"
        output["judgment_reason"] = (
            str(raw.get("judgment_reason") or "").strip()
            or "Stage2 未返回可核验的阶段证据 ID，未将候选事实升级为正式引用。"
        )
        output["stage_handoff_status"] = "handoff_loss"
    elif any(value not in {"present", "absent"} for value in readiness.values()):
        output["relation"] = "uncertain"
        output["model_gap_magnitude"] = "uncertain"
        output["stage_state"] = "unknown" if "conflict" not in readiness.values() else "conflict"
        output["judgment_reason"] = (
            str(raw.get("judgment_reason") or "").strip()
            or f"Stage1 资格未闭合：benchmark={readiness['benchmark']}，creator={readiness['creator']}。"
        )
        output["stage_handoff_status"] = "evidence_blocked"
    else:
        # A complete evidence handoff is necessary but not sufficient.  The
        # group response must also close its own semantic state; otherwise a
        # model-supplied relation or magnitude would become a conclusion merely
        # because Stage1 happened to have evidence.
        stage_state = str(raw.get("stage_state") or "unknown").strip().lower()
        output["stage_state"] = stage_state if stage_state in {"completed", "unknown", "conflict", "blocked"} else "unknown"
        output["judgment_reason"] = str(raw.get("judgment_reason") or raw.get("reason") or "").strip()
        if output["stage_state"] != "completed":
            output["relation"] = "uncertain"
            output["model_gap_magnitude"] = "uncertain"
            output["stage_handoff_status"] = "unknown" if output["stage_state"] == "unknown" else "evidence_blocked"
        else:
            relation = str(raw.get("relation") or "").strip().lower()
            output["relation"] = relation if relation in {"creator_better", "benchmark_better", "equivalent", "uncertain"} else "uncertain"
            magnitude = str(raw.get("model_gap_magnitude") or "").strip().lower()
            output["model_gap_magnitude"] = magnitude if magnitude in {"none", "small", "medium", "large", "uncertain"} else "uncertain"
            output["stage_handoff_status"] = "grounded"

    # ``gap_type`` is a semantic Stage2 judgment, not a Stage3-owned field.
    # Preserve it only after the evidence handoff and stage state are closed;
    # an unknown/blocked stage must not inherit a diagnosis from raw prose.
    raw_gap_type = str(raw.get("gap_type") or "").strip().lower()
    output["gap_type"] = (
        raw_gap_type
        if output["stage_state"] == "completed" and raw_gap_type in {"structural", "execution", "resource", "unknown"}
        else "unknown"
    )
    output["gap_summary"] = [output["judgment_reason"] or "待基于阶段证据复核。"]
    output["evidence"] = [output["judgment_reason"] or "阶段证据由代码交接。"]
    output["gap"] = output["judgment_reason"] or "阶段差距待复核。"
    output["time_range"] = (
        f"标杆 {output.get('benchmark_time_range') or '待复核'} / "
        f"达人 {output.get('creator_time_range') or '待复核'}"
    )
    # Unknown, blocked, and handoff-loss stages must not acquire a synthetic
    # medium severity before the resolver/finalizer runs.  A temporary default
    # here would already be visible to Stage3 synthesis and could be mistaken
    # for a model conclusion.
    output["model_severity"] = (
        output["model_gap_magnitude"]
        if output["model_gap_magnitude"] in {"small", "medium", "large"}
        else None
    )
    output["severity"] = output["model_severity"]
    output["creator_module_id"] = "unknown"
    output["benchmark_module_id"] = "unknown"
    output["module_fit"] = "unknown"
    output["module_fit_reason"] = output["judgment_reason"]
    # Grounded evidence only proves that the handoff is usable; it does not
    # prove the creator completed the stage.  Keep the legacy field unknown
    # unless a separate semantic producer supplies it.
    output["task_completion"] = None
    output["voice_performance"] = {
        "pace": "unknown",
        "energy": "unknown",
        "key_pause": None,
        "note": "由阶段证据交接。",
    }
    output["benchmark_execution"] = None
    output["creator_execution"] = None
    output["painpoint_relevance"] = None
    output["stage_standard_delivery"] = "unknown"

    # A nested structured flag is accepted only as a complete object. Partial
    # semantic objects are worse than an explicit unknown because the existing
    # resolver/validators would otherwise mistake omitted booleans for facts.
    if output.get("stage_handoff_status") == "grounded":
        for role in ("benchmark", "creator"):
            key = f"{role}_{stage.lower() if stage != 'S1' else 'hook'}"
            value = raw.get(key)
            if _segmented_complete_flag(value, stage):
                output[key] = _sanitize_segmented_flag(value, set(role_ids[role]))
    return output


def _deterministic_product_visibility(facts: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    """Project product visibility from immutable creator facts, without LLM estimation."""
    side = facts.get("creator") if isinstance(facts.get("creator"), dict) else {}
    raw_duration = (analysis.get("videos", {}).get("creator", {}) or {}).get("duration_seconds")
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError):
        duration = 0.0
    if not math.isfinite(duration) or duration <= 0:
        return {
            "first_appearance_sec": None,
            "total_screen_time_sec": None,
            "video_duration_sec": None,
            "ratio": None,
            "estimation_note": "达人视频时长缺失或无效，无法从时间区间计算产品出镜统计，需人工复核。",
        }
    visibility_observed = any(
        isinstance(unit, dict)
        and (unit.get("product_visible") is True or unit.get("product_visible") is False)
        for unit in side.get("evidence_units") or []
    )
    if not visibility_observed:
        return {
            "first_appearance_sec": None,
            "total_screen_time_sec": None,
            "video_duration_sec": round(duration, 3),
            "ratio": None,
            "estimation_note": "Stage1 未提供明确的 product_visible 观察，不能把未知当作产品未出镜，需人工复核。",
        }
    intervals: list[tuple[float, float]] = []
    for unit in side.get("evidence_units") or []:
        if not isinstance(unit, dict) or unit.get("product_visible") is not True:
            continue
        parsed = parse_time_range_seconds(unit.get("time_range"), duration or None)
        if parsed is not None and parsed[1] > parsed[0]:
            intervals.append(parsed)
    intervals.sort()
    merged: list[list[float]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    total = sum(end - start for start, end in merged)
    first = merged[0][0] if merged else 0.0
    ratio = total / duration
    return {
        "first_appearance_sec": round(first, 3),
        "total_screen_time_sec": round(total, 3),
        "video_duration_sec": round(duration, 3),
        "ratio": round(min(max(ratio, 0.0), 1.0), 6),
        "estimation_note": "由代码从达人 Stage1 evidence_units 的明确 product_visible 标记合并区间计算。",
    }


def _deterministic_improvement(stage_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = [
        item for item in stage_results
        if isinstance(item, dict) and item.get("model_gap_magnitude") in {"large", "medium"}
    ]
    selected = candidates[0] if candidates else (stage_results[0] if stage_results else {})
    stage = _segmented_stage_code(selected.get("stage")) or "S1"
    return [{
        "title": f"优先复核{stage}阶段证据",
        "target_stage": stage,
        "gmv_impact": "待基于完整阶段证据确认",
        "gap_type": selected.get("gap_type") if selected.get("gap_type") in {"structural", "execution", "resource", "unknown"} else "unknown",
        "time_range": selected.get("creator_time_range") or "",
        "creator_time_range": selected.get("creator_time_range") or "",
        "benchmark_time_range": selected.get("benchmark_time_range") or "",
        "problem": selected.get("gap") or "阶段证据不足，暂不生成确定性建议。",
        "benchmark_reference": selected.get("benchmark_summary") or "暂无合格标杆证据。",
        "benchmark_evidence_ids": selected.get("benchmark_evidence_ids") or [],
        "suggestion": "待阶段证据完成后再生成具体改进动作。",
        "actions": ["补齐并复核该阶段关键证据"],
        "gmv_reason": "避免把证据未知误写成业务缺陷。",
        "evidence": selected.get("evidence") or [],
        "priority": 1,
    }]


def _project_synthesis_improvements(
    raw_improvements: Any,
    stage_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project model prose onto code-owned stage evidence and ranges.

    Stage3 may write prose and select a target stage. It cannot author IDs,
    time ranges, gap types, or priority because those fields already have a
    single authoritative source in the normalized Stage2 result.
    """
    by_code = {
        code: stage
        for stage in stage_results
        if isinstance(stage, dict)
        for code in [_segmented_stage_code(stage.get("stage"))]
        if code in _SEGMENTED_STAGE_NAMES
    }
    projected: list[dict[str, Any]] = []
    for item in raw_improvements if isinstance(raw_improvements, list) else []:
        if not isinstance(item, dict):
            continue
        code = _segmented_stage_code(item.get("target_stage"))
        stage = by_code.get(code)
        if stage is None:
            continue
        magnitude = str(stage.get("model_gap_magnitude") or "uncertain").strip().lower()
        priority = {"large": 1, "medium": 2, "small": 3, "none": 4}.get(magnitude, 4)
        raw_actions = item.get("actions")
        actions = [raw_actions] if isinstance(raw_actions, str) else raw_actions
        actions = actions if isinstance(actions, list) else []
        projected.append(
            {
                "title": str(item.get("title") or f"复核{code}阶段").strip(),
                "target_stage": code,
                "problem": str(item.get("problem") or stage.get("gap") or "阶段差距待复核").strip(),
                "suggestion": str(item.get("suggestion") or "待基于阶段证据复核").strip(),
                "actions": [str(value).strip() for value in actions if str(value).strip()][:5],
                "gmv_reason": str(item.get("gmv_reason") or "避免把证据未知误写成业务缺陷").strip(),
                "gmv_impact": str(item.get("gmv_impact") or "待基于完整阶段证据确认").strip(),
                # Stage3 can supply prose only.  gap_type is projected from
                # the already-closed Stage2 result.
                "gap_type": stage.get("gap_type") if stage.get("gap_type") in {"structural", "execution", "resource", "unknown"} else "unknown",
                "time_range": stage.get("time_range") or "",
                "creator_time_range": stage.get("creator_time_range") or "",
                "benchmark_time_range": stage.get("benchmark_time_range") or "",
                "benchmark_reference": stage.get("benchmark_summary") or "暂无合格标杆证据。",
                "benchmark_evidence_ids": list(stage.get("benchmark_evidence_ids") or []),
                "evidence": list(stage.get("evidence") or []),
                "priority": priority,
            }
        )
    return projected[:5]


def _prepare_segmented_synthesis(raw: dict[str, Any] | None, stage_results: list[dict[str, Any]]) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    result = copy.deepcopy(raw)
    result.setdefault("one_line_verdict", "基于分阶段证据完成分析，部分字段需按阶段状态复核。")
    result.setdefault("one_line_summary", result["one_line_verdict"])
    result.setdefault("executive_summary", result["one_line_summary"])
    result.setdefault("holistic_assessment", {})
    result.setdefault("key_conclusions", [])
    result.setdefault("loop_closure", {})
    result.setdefault("s3_s4_relationship", {})
    result.setdefault("promise_chain", {})
    result["improvements"] = _project_synthesis_improvements(
        result.get("improvements"),
        stage_results,
    ) or _deterministic_improvement(stage_results)
    return result


def _segmented_stage_unresolved(stage_results: list[dict[str, Any]]) -> list[str]:
    """Return core stages that cannot support a completed run marker."""
    unresolved: list[str] = []
    for stage in stage_results:
        if not isinstance(stage, dict):
            continue
        code = _segmented_stage_code(stage.get("stage"))
        comparison_status = str(stage.get("comparison_status") or "").strip().lower()
        if comparison_status in {"not_directly_comparable", "not_applicable"}:
            # A closed comparison scope is an intentional terminal state, not
            # a failed Stage2 handoff. It must remain explicit in the report,
            # but should not make an otherwise complete segmented run appear
            # degraded.
            continue
        status = str(
            stage.get("analysis_status")
            or stage.get("stage_handoff_status")
            or "unknown"
        ).strip().lower()
        stage_state = str(stage.get("stage_state") or "unknown").strip().lower()
        magnitude = str(stage.get("model_gap_magnitude") or "unknown").strip().lower()
        # ``stage_state`` is a required semantic output.  A grounded evidence
        # handoff is necessary but not sufficient: if the model did not close
        # the stage judgment itself, relation/magnitude must not become a
        # publishable conclusion through a compatibility default.
        if code and (
            status != "grounded"
            or stage_state != "completed"
            or magnitude == "uncertain"
        ):
            unresolved.append(code)
    return list(dict.fromkeys(unresolved))


def _build_stage1_to_stage2_handoff(
    facts: dict[str, Any],
    analysis: dict[str, Any],
) -> dict[str, Any]:
    """Build the immutable, code-owned Stage1-to-Stage2 handoff record."""
    handoff: dict[str, Any] = {
        "version": "stage1_to_stage2_handoff_v1",
        "pipeline": "segmented_stage_v1",
        "roles": {},
    }
    for role in ("benchmark", "creator"):
        side = facts.get(role) if isinstance(facts.get(role), dict) else {}
        view = stage_analysis_evidence_view(side)
        projection = stage1_qualification_projection(side)
        video_info = analysis.get("videos", {}).get(role, {}) if isinstance(analysis.get("videos"), dict) else {}
        preprocess_fingerprint = (
            video_info.get("preprocess_fingerprint")
            if isinstance(video_info.get("preprocess_fingerprint"), dict)
            else {}
        )
        handoff["roles"][role] = {
            "ledger_ref": {
                "version": side.get("evidence_set_version") or STAGE_EVIDENCE_SNAPSHOT_VERSION,
                "sha256": side.get("evidence_set_sha256") or "",
            },
            "ledger_manifest": stage1_ledger_manifest(side),
            "source_fingerprints": {
                "source_video_sha256": _source_video_hash(analysis, role),
                "preprocess_fingerprint_sha256": _stable_digest(preprocess_fingerprint),
            },
            "stage1_projection": projection,
            "ledger_hash": side.get("evidence_set_sha256") or "",
            "qualified_evidence_ids": view.get("qualified_stage_evidence_ids") or {},
            "candidate_evidence_ids": view.get("candidate_evidence_ids_by_stage") or {},
            "candidate_observations_by_stage": view.get("candidate_observations_by_stage") or {},
            "stage_readiness": view.get("stage_evidence_readiness") or {},
            "coverage_summary": {
                stage: {
                    "readiness": (view.get("stage_evidence_readiness") or {}).get(stage, "unknown"),
                    "qualified_count": len((view.get("qualified_stage_evidence_ids") or {}).get(stage) or []),
                    "candidate_count": len((view.get("candidate_evidence_ids_by_stage") or {}).get(stage) or []),
                }
                for stage in stage_codes()
            },
        }
    return handoff


def _stage1_to_stage2_handoff_issues(
    handoff: dict[str, Any],
    facts: dict[str, Any],
    analysis: dict[str, Any],
) -> list[str]:
    """Check that the persisted handoff is a lossless projection of Stage1."""
    if not isinstance(handoff, dict):
        return ["handoff_not_object"]
    roles = handoff.get("roles") if isinstance(handoff.get("roles"), dict) else {}
    issues: list[str] = []
    for role in ("benchmark", "creator"):
        side = facts.get(role) if isinstance(facts.get(role), dict) else {}
        actual = roles.get(role) if isinstance(roles.get(role), dict) else None
        if actual is None:
            issues.append(f"{role}:handoff_role_missing")
            continue
        view = stage_analysis_evidence_view(side)
        expected = {
            "ledger_ref": {
                "version": side.get("evidence_set_version") or STAGE_EVIDENCE_SNAPSHOT_VERSION,
                "sha256": side.get("evidence_set_sha256") or "",
            },
            "ledger_manifest": stage1_ledger_manifest(side),
            "stage1_projection": stage1_qualification_projection(side),
            "ledger_hash": side.get("evidence_set_sha256") or "",
            "qualified_evidence_ids": view.get("qualified_stage_evidence_ids") or {},
            "candidate_evidence_ids": view.get("candidate_evidence_ids_by_stage") or {},
            "candidate_observations_by_stage": view.get("candidate_observations_by_stage") or {},
            "stage_readiness": view.get("stage_evidence_readiness") or {},
            "coverage_summary": {
                stage: {
                    "readiness": (view.get("stage_evidence_readiness") or {}).get(stage, "unknown"),
                    "qualified_count": len((view.get("qualified_stage_evidence_ids") or {}).get(stage) or []),
                    "candidate_count": len((view.get("candidate_evidence_ids_by_stage") or {}).get(stage) or []),
                }
                for stage in stage_codes()
            },
        }
        for field, expected_value in expected.items():
            if actual.get(field) != expected_value:
                issues.append(f"{role}:handoff_field_mismatch:{field}")
        video_info = analysis.get("videos", {}).get(role, {}) if isinstance(analysis.get("videos"), dict) else {}
        fingerprint = video_info.get("preprocess_fingerprint") if isinstance(video_info.get("preprocess_fingerprint"), dict) else {}
        expected_fingerprints = {
            "source_video_sha256": _source_video_hash(analysis, role),
            "preprocess_fingerprint_sha256": _stable_digest(fingerprint),
        }
        if actual.get("source_fingerprints") != expected_fingerprints:
            issues.append(f"{role}:handoff_field_mismatch:source_fingerprints")
    return list(dict.fromkeys(issues))


def _stage2_replay_source(args: argparse.Namespace) -> tuple[Path | None, bool]:
    """Return the source directory and whether missing entries may call LLM."""
    replay = getattr(args, "stage2_replay_from", None)
    resume = getattr(args, "stage2_resume_from", None)
    if replay:
        return Path(replay).expanduser().resolve(), False
    if resume:
        return Path(resume).expanduser().resolve(), True
    return None, True


def _stage1_replay_source(args: argparse.Namespace) -> tuple[Path | None, bool]:
    """Return the frozen Stage1 artifact source and provider fallback policy."""
    replay = getattr(args, "stage1_replay_from", None)
    resume = getattr(args, "stage1_resume_from", None)
    if isinstance(replay, (str, Path)) and str(replay):
        return Path(replay).expanduser().resolve(), False
    if isinstance(resume, (str, Path)) and str(resume):
        return Path(resume).expanduser().resolve(), True
    return None, True


def _same_existing_artifact(left: Path | None, right: Path) -> bool:
    if left is None:
        return False
    try:
        return os.path.samefile(left, right)
    except OSError:
        return left.expanduser().resolve() == right.expanduser().resolve()


def _resume_failure_artifact_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.resume-failed{path.suffix}")


def _archive_stage_group_artifact(path: Path, label: str) -> Path:
    artifact = read_stage_group_artifact(path)
    digest = _stable_digest(artifact)[:12]
    archived = path.with_name(f"{path.stem}.{label}.{digest}{path.suffix}")
    if archived.exists():
        existing = read_stage_group_artifact(archived)
        if _stable_digest(existing) != _stable_digest(artifact):
            raise StageGroupArtifactError(
                f"stage group archive collision or corruption: {archived}"
            )
    else:
        write_json(archived, artifact)
    return archived


def _validated_stage_group_response(
    parsed: Any,
    target: list[str],
    *,
    label: str,
    facts: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    if not isinstance(parsed, dict):
        raise ValueError(f"阶段组 {label} 必须返回 JSON 对象")
    raw_stages = parsed.get("stages") if isinstance(parsed.get("stages"), list) else []
    raw_codes: list[str] = []
    for item in raw_stages:
        if not isinstance(item, dict):
            raise ValueError(f"阶段组 {label} 的 stage item 必须是 JSON 对象")
        code = _segmented_stage_code(item.get("stage"))
        if code is None:
            raise ValueError(f"阶段组 {label} 返回了无效阶段")
        raw_codes.append(code)
    by_code = {
        _segmented_stage_code(item.get("stage")): item
        for item in raw_stages
        if isinstance(item, dict) and _segmented_stage_code(item.get("stage")) in target
    }
    if len(raw_codes) != len(target) or set(raw_codes) != set(target):
        raise ValueError(
            f"阶段组 {label} 必须恰好覆盖目标阶段一次："
            f"期待 {target}，实际 {raw_codes}"
        )
    required_keys = {
        "stage_state",
        "relation",
        "model_gap_magnitude",
        "benchmark_evidence_ids",
        "creator_evidence_ids",
        "judgment_reason",
    }
    valid_states = {"completed", "unknown", "conflict", "blocked"}
    valid_relations = {"creator_better", "benchmark_better", "equivalent", "uncertain"}
    valid_magnitudes = {"none", "small", "medium", "large", "uncertain"}
    for code in target:
        item = by_code[code]
        missing = sorted(required_keys - set(item))
        if missing:
            raise ValueError(
                f"阶段组 {label} 的 {code} 缺少必填语义字段：{', '.join(missing)}"
            )
        stage_state = str(item.get("stage_state") or "").strip().lower()
        relation = str(item.get("relation") or "").strip().lower()
        magnitude = str(item.get("model_gap_magnitude") or "").strip().lower()
        if stage_state not in valid_states:
            raise ValueError(f"阶段组 {label} 的 {code} stage_state 非法")
        if relation not in valid_relations:
            raise ValueError(f"阶段组 {label} 的 {code} relation 非法")
        if magnitude not in valid_magnitudes:
            raise ValueError(f"阶段组 {label} 的 {code} model_gap_magnitude 非法")
        if stage_state != "completed" and (relation != "uncertain" or magnitude != "uncertain"):
            raise ValueError(
                f"阶段组 {label} 的 {code} 未完成状态必须保持 uncertain relation/magnitude"
            )
        if stage_state == "completed":
            consistent = (
                (relation == "equivalent" and magnitude == "none")
                or (
                    relation in {"creator_better", "benchmark_better"}
                    and magnitude in {"small", "medium", "large"}
                )
            )
            if not consistent:
                raise ValueError(
                    f"阶段组 {label} 的 {code} relation 与 model_gap_magnitude 自相矛盾"
                )
        for key in ("benchmark_evidence_ids", "creator_evidence_ids"):
            if not isinstance(item.get(key), list):
                raise ValueError(f"阶段组 {label} 的 {code} {key} 必须是数组")
            raw_ids = item[key]
            if any(not isinstance(value, str) or not value.strip() for value in raw_ids):
                raise ValueError(f"阶段组 {label} 的 {code} {key} 只能包含非空字符串")
            if facts is not None:
                role = key.removesuffix("_evidence_ids")
                side = facts.get(role) if isinstance(facts.get(role), dict) else {}
                valid_ids = qualified_stage_evidence_ids(side, code)
                invalid_ids = sorted(set(raw_ids) - valid_ids)
                if invalid_ids:
                    raise ValueError(
                        f"阶段组 {label} 的 {code} {key} 含非本阶段合格证据："
                        + ", ".join(invalid_ids)
                    )
        if not isinstance(item.get("judgment_reason"), str) or not item["judgment_reason"].strip():
            raise ValueError(f"阶段组 {label} 的 {code} judgment_reason 不能为空")
    return by_code


def _validated_stage_synthesis_response(parsed: Any) -> dict[str, Any]:
    if not isinstance(parsed, dict):
        raise ValueError("Stage3 synthesis must be a JSON object")
    parsed = copy.deepcopy(parsed)
    required_types: dict[str, type] = {
        "one_line_verdict": str,
        "one_line_summary": str,
        "executive_summary": str,
        "holistic_assessment": dict,
        "key_conclusions": list,
        "loop_closure": dict,
        "s3_s4_relationship": dict,
        "promise_chain": dict,
        "improvements": list,
    }
    missing = sorted(set(required_types) - set(parsed))
    if missing:
        raise ValueError("Stage3 synthesis 缺少必填字段：" + ", ".join(missing))
    for key, expected_type in required_types.items():
        if not isinstance(parsed.get(key), expected_type):
            raise ValueError(f"Stage3 synthesis 的 {key} 类型非法")
    for key in ("one_line_verdict", "one_line_summary", "executive_summary"):
        if not str(parsed.get(key) or "").strip():
            raise ValueError(f"Stage3 synthesis 的 {key} 不能为空")
    conclusions = parsed["key_conclusions"]
    if any(not isinstance(item, str) or not item.strip() for item in conclusions):
        raise ValueError("Stage3 synthesis 的 key_conclusions 只能包含非空字符串")
    improvement_keys = {
        "title",
        "target_stage",
        "problem",
        "suggestion",
        "actions",
        "gmv_reason",
        "gmv_impact",
    }
    for index, item in enumerate(parsed["improvements"]):
        if not isinstance(item, dict):
            raise ValueError(f"Stage3 synthesis 的 improvements[{index}] 必须是对象")
        missing = sorted(improvement_keys - set(item))
        if missing:
            raise ValueError(
                f"Stage3 synthesis 的 improvements[{index}] 缺少字段：" + ", ".join(missing)
            )
        if _segmented_stage_code(item.get("target_stage")) is None:
            raise ValueError(f"Stage3 synthesis 的 improvements[{index}] target_stage 非法")
        for key in improvement_keys - {"target_stage", "actions"}:
            if not isinstance(item.get(key), str) or not item[key].strip():
                raise ValueError(f"Stage3 synthesis 的 improvements[{index}].{key} 不能为空")
        actions = item.get("actions")
        valid_action_string = isinstance(actions, str) and bool(actions.strip())
        valid_action_list = isinstance(actions, list) and all(
            isinstance(action, str) and bool(action.strip()) for action in actions
        )
        if not valid_action_string and not valid_action_list:
            raise ValueError(f"Stage3 synthesis 的 improvements[{index}].actions 类型非法")
    return parsed


def _read_replayable_stage_fact(
    source_dir: Path,
    *,
    role: str,
    phase: str,
    group: list[str] | tuple[str, ...] | None,
    payload: dict[str, Any],
    args: argparse.Namespace,
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    path = stage_fact_artifact_path(source_dir, role, phase, group)
    artifact = read_stage_fact_artifact(path)
    response, response_meta = reusable_stage_fact_response(
        artifact,
        role=role,
        phase=phase,
        group=group,
        payload=payload,
        model=_stage1_model(args, phase),
        api_url=args.llm_api_url,
    )
    return response, response_meta, path


def _read_replayable_stage_group(
    source_dir: Path,
    *,
    group: list[str] | tuple[str, ...],
    payload: dict[str, Any],
    args: argparse.Namespace,
    allow_validation_failed: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    path = stage_group_artifact_path(source_dir, group)
    artifact = read_stage_group_artifact(path)
    if allow_validation_failed and artifact.get("status") == "failed":
        response = revalidatable_failed_stage_group_response(
            artifact,
            group=group,
            payload=payload,
            model=judgment_model(args),
            api_url=args.llm_api_url,
        )
    else:
        response = reusable_stage_group_response(
            artifact,
            group=group,
            payload=payload,
            model=judgment_model(args),
            api_url=args.llm_api_url,
        )
    response_meta = artifact.get("response_meta")
    meta = copy.deepcopy(response_meta) if isinstance(response_meta, dict) else {}
    if allow_validation_failed and artifact.get("status") == "failed":
        meta["execution_source"] = "revalidation"
        meta["revalidated_from_status"] = "failed"
    return response, meta


def run_segmented_stage_pipeline(
    args: argparse.Namespace,
    analysis: dict[str, Any],
    analysis_input: str,
    facts: dict[str, Any],
    run_dir: Path,
    api_key: str,
) -> dict[str, Any]:
    """Run the frozen Stage2/Stage3 path without whole-object model repair."""
    def materialize_handoff() -> dict[str, Any]:
        value = _build_stage1_to_stage2_handoff(facts, analysis)
        issues = _stage1_to_stage2_handoff_issues(value, facts, analysis)
        value["integrity"] = {
            "algorithm": "sha256",
            "sha256": _stable_digest(
                {
                    "version": value["version"],
                    "pipeline": value["pipeline"],
                    "roles": value["roles"],
                    "validation_status": "failed" if issues else "passed",
                    "validation_issues": issues,
                }
            ),
            "validation_status": "failed" if issues else "passed",
            "validation_issues": issues,
            "preservation_target": "100% of Stage1 ledger IDs and hashes are represented in the handoff",
        }
        write_json(run_dir / "stage1_to_stage2_handoff.json", value)
        if issues:
            raise SystemExit("Stage1 到 Stage2 交接校验失败：" + ", ".join(issues))
        return value

    handoff = _run_pipeline_phase("stage1_handoff", "handoff", materialize_handoff)
    stage_results: list[dict[str, Any]] = []
    group_records: list[dict[str, Any]] = []
    any_group_failed = False
    replay_source, provider_fallback_allowed = _stage2_replay_source(args)
    for group in STAGE_JUDGMENT_GROUPS:
        label = "_".join(group)
        target = list(group)
        record: dict[str, Any] = {"group": target, "status": "pending", "stages": []}
        comparison_eligibility = _authoritative_segmented_comparison_contract(analysis, {})
        stage_contracts = (
            comparison_eligibility.get("stage_eligibility")
            if isinstance(comparison_eligibility.get("stage_eligibility"), dict)
            else {}
        )
        closed_scope = bool(target) and all(
            isinstance(stage_contracts.get(code), dict)
            and str(stage_contracts[code].get("status") or "").strip().lower()
            in {"not_comparable", "not_applicable"}
            for code in target
        )
        if closed_scope:
            projected = [
                _normalize_segmented_stage({}, code, facts, comparison_eligibility)
                for code in target
            ]
            record.update(
                {
                    "status": "completed",
                    "stages": target,
                    "response_sha256": _stable_digest(projected),
                    "execution_source": "deterministic_scope",
                    "completion_attempts": 0,
                    "retry_reasons": [],
                    "usage": {},
                }
            )
            stage_results.extend(projected)
            group_records.append(record)
            write_json(run_dir / f"stage_group_{label}.json", record)
            continue
        provider_artifact_path = stage_group_artifact_path(run_dir, target)
        request_path = run_dir / f"llm_stage_group_{label}_request.json"
        response_path = run_dir / f"llm_stage_group_{label}_response.json"
        payload: dict[str, Any] = {}
        response_meta: dict[str, Any] = {}
        execution_source = "provider"
        replay_artifact_path = (
            stage_group_artifact_path(replay_source, target)
            if replay_source is not None
            else None
        )
        preserve_resume_source = (
            provider_fallback_allowed
            and _same_existing_artifact(replay_artifact_path, provider_artifact_path)
        )
        resume_failure_path = _resume_failure_artifact_path(provider_artifact_path)
        try:
            payload = build_stage_group_judgment_payload(
                judgment_model(args),
                analysis_input,
                facts,
                analysis,
                target,
                api_url=args.llm_api_url,
                budget=getattr(args, "_resource_budget", None),
            )
            parsed: dict[str, Any] | None = None
            if replay_source is not None:
                execution_source = "replay"
                try:
                    parsed, response_meta = _read_replayable_stage_group(
                        replay_source,
                        group=target,
                        payload=payload,
                        args=args,
                    )
                    _validated_stage_group_response(parsed, target, label=label, facts=facts)
                except (StageGroupArtifactError, ValueError) as exc:
                    if not provider_fallback_allowed:
                        raise SystemExit(f"阶段组 {label} 无法离线重放：{exc}") from exc
                    execution_source = "provider"
                    parsed = None
                    response_meta = {}
            if parsed is None:
                write_json(request_path, payload)
                response_text = fetch_json_completion(
                    args,
                    api_key,
                    request_path,
                    response_path,
                    response_meta=response_meta,
                )
                parsed = parse_json_text(response_text)
            by_code = _validated_stage_group_response(parsed, target, label=label, facts=facts)
            write_json(
                provider_artifact_path,
                completed_stage_group_artifact(
                    group=target,
                    payload=payload,
                    response=parsed,
                    model=judgment_model(args),
                    api_url=args.llm_api_url,
                    response_meta=response_meta,
                ),
            )
            resume_failure_path.unlink(missing_ok=True)
            projected = [
                _normalize_segmented_stage(by_code[code], code, facts, comparison_eligibility)
                for code in target
            ]
            record.update({
                "status": "completed",
                "stages": target,
                "response_sha256": _stable_digest(parsed),
                "provider_artifact": provider_artifact_path.name,
                "execution_source": execution_source,
                "logical_request_id": response_meta.get("logical_request_id"),
                "completion_attempts": response_meta.get("completion_attempts", 0),
                "retry_reasons": response_meta.get("retry_reasons", []),
                "usage": response_meta.get("usage", {}),
            })
            stage_results.extend(projected)
        except (OSError, ValueError, RuntimeError, SystemExit, json.JSONDecodeError) as exc:
            any_group_failed = True
            record.update(
                {
                    "status": "failed",
                    "failure_kind": _localized_failure_kind(
                        exc,
                        execution_source=execution_source,
                        default="provider_call_or_validation",
                    ),
                    "cause_type": exc.__class__.__name__,
                    "error": str(exc),
                }
            )
            failure_artifact = failed_stage_group_artifact(
                group=target,
                payload=payload,
                model=judgment_model(args),
                api_url=args.llm_api_url,
                error=str(exc),
                response_meta=response_meta,
                response=parsed,
            )
            failure_path = resume_failure_path if preserve_resume_source else provider_artifact_path
            write_json(
                failure_path,
                failure_artifact,
            )
            record["provider_artifact"] = failure_path.name
            projected = [
                _normalize_segmented_stage(
                    {"stage": _SEGMENTED_STAGE_NAMES[code], "relation": "uncertain", "model_gap_magnitude": "uncertain", "judgment_reason": f"阶段组调用失败：{exc}"},
                    code,
                    facts,
                    _authoritative_segmented_comparison_contract(analysis, {}),
                )
                for code in target
            ]
            stage_results.extend(projected)
        group_records.append(record)
        write_json(run_dir / f"stage_group_{label}.json", record)

    unresolved_before_synthesis = _segmented_stage_unresolved(stage_results)
    skip_synthesis = any_group_failed or bool(unresolved_before_synthesis)
    synthesis_status = "failed" if skip_synthesis else "completed"
    synthesis: dict[str, Any] = {}
    synthesis_request = run_dir / "llm_stage_synthesis_request.json"
    synthesis_response = run_dir / "llm_stage_synthesis_response.json"
    synthesis_provider_artifact = stage_group_artifact_path(run_dir, ("SYNTHESIS",))
    synthesis_payload: dict[str, Any] = {}
    synthesis_response_meta: dict[str, Any] = {}
    synthesis_execution_source = "not_run" if skip_synthesis else "provider"
    parsed_synthesis: dict[str, Any] | None = None
    synthesis_artifact_record = "" if skip_synthesis else synthesis_provider_artifact.name
    synthesis_replay_artifact = (
        stage_group_artifact_path(replay_source, ("SYNTHESIS",))
        if replay_source is not None
        else None
    )
    preserve_synthesis_resume_source = (
        provider_fallback_allowed
        and _same_existing_artifact(synthesis_replay_artifact, synthesis_provider_artifact)
    )
    synthesis_resume_failure = _resume_failure_artifact_path(synthesis_provider_artifact)
    if skip_synthesis:
        if preserve_synthesis_resume_source and synthesis_provider_artifact.is_file():
            _archive_stage_group_artifact(synthesis_provider_artifact, "superseded")
            synthesis_provider_artifact.unlink()
        synthesis = {
            "synthesis_error": "Stage3 synthesis 未执行：阶段组失败或存在 unresolved stage。",
            "failure_kind": "upstream_stage_unresolved",
            "cause_type": "UpstreamStageUnresolved",
        }
    else:
        try:
            synthesis_payload = build_stage_synthesis_payload(
                judgment_model(args), analysis_input, facts, stage_results, analysis
            )
            if replay_source is not None:
                synthesis_execution_source = "replay"
                try:
                    parsed_synthesis, synthesis_response_meta = _read_replayable_stage_group(
                        replay_source,
                        group=("SYNTHESIS",),
                        payload=synthesis_payload,
                        args=args,
                        allow_validation_failed=True,
                    )
                    _validated_stage_synthesis_response(parsed_synthesis)
                    if synthesis_response_meta.get("execution_source") == "revalidation":
                        synthesis_execution_source = "revalidation"
                except (StageGroupArtifactError, ValueError) as exc:
                    if not provider_fallback_allowed:
                        raise SystemExit(f"Stage3 synthesis 无法离线重放：{exc}") from exc
                    synthesis_execution_source = "provider"
                    parsed_synthesis = None
                    synthesis_response_meta = {}
            if parsed_synthesis is None:
                write_json(synthesis_request, synthesis_payload)
                response_text = fetch_json_completion(
                    args,
                    api_key,
                    synthesis_request,
                    synthesis_response,
                    response_meta=synthesis_response_meta,
                )
                parsed_synthesis = parse_json_text(response_text)
            parsed_synthesis = _validated_stage_synthesis_response(parsed_synthesis)
            if (
                preserve_synthesis_resume_source
                and synthesis_response_meta.get("revalidated_from_status") == "failed"
                and synthesis_provider_artifact.is_file()
            ):
                _archive_stage_group_artifact(
                    synthesis_provider_artifact,
                    "validation-failed",
                )
            write_json(
                synthesis_provider_artifact,
                completed_stage_group_artifact(
                    group=("SYNTHESIS",),
                    payload=synthesis_payload,
                    response=parsed_synthesis,
                    model=judgment_model(args),
                    api_url=args.llm_api_url,
                    response_meta=synthesis_response_meta,
                ),
            )
            synthesis_resume_failure.unlink(missing_ok=True)
            synthesis = parsed_synthesis
        except (OSError, ValueError, RuntimeError, SystemExit, json.JSONDecodeError) as exc:
            synthesis_status = "failed"
            synthesis = {
                "synthesis_error": str(exc),
                "failure_kind": _localized_failure_kind(
                    exc,
                    execution_source=synthesis_execution_source,
                    default="provider_call_or_validation",
                ),
                "cause_type": exc.__class__.__name__,
            }
            any_group_failed = True
            failure_artifact = failed_stage_group_artifact(
                group=("SYNTHESIS",),
                payload=synthesis_payload,
                model=judgment_model(args),
                api_url=args.llm_api_url,
                error=str(exc),
                response_meta=synthesis_response_meta,
                response=parsed_synthesis,
            )
            failure_path = (
                synthesis_resume_failure
                if preserve_synthesis_resume_source
                else synthesis_provider_artifact
            )
            write_json(failure_path, failure_artifact)
            synthesis_artifact_record = failure_path.name

    synthesis = _prepare_segmented_synthesis(synthesis, stage_results)
    unresolved_stages = _segmented_stage_unresolved(stage_results)
    raw_bundle = {
        "source_format": "segmented_provider_bundle",
        "pipeline": "segmented_stage_v1",
        "stage_groups": group_records,
        "synthesis_status": synthesis_status,
        "synthesis_provider_artifact": synthesis_artifact_record,
        "synthesis_execution_source": synthesis_execution_source,
        "unresolved_stages": unresolved_stages,
        "synthesis": synthesis,
    }
    _write_raw_model_response(
        run_dir,
        result=raw_bundle,
        source_format="segmented_provider_bundle",
        overwrite=True,
    )
    candidate_status = (
        "degraded" if any_group_failed or unresolved_stages else "completed"
    )
    analysis["stage2_candidate_status"] = candidate_status
    analysis["stage2_pipeline_version"] = "segmented_stage_v1"
    analysis["stage_evidence_contract_required"] = True
    analysis["evidence_state_required"] = False
    analysis["multimodal_assessment_required"] = False
    # Stage-specific flags are accepted only when the group returned a complete
    # object. This prevents a partial semantic object from activating the old
    # validator as a new source of truth.
    for key in ("s1_hook_flags_required", "s2_flags_required", "s3_flags_required", "s4_flags_required", "s5_flags_required", "s6_flags_required"):
        analysis[key] = False
    foundation = analysis.get("product_foundation") if isinstance(analysis.get("product_foundation"), dict) else {}
    result = {
        "one_line_verdict": synthesis.get("one_line_verdict"),
        "one_line_summary": synthesis.get("one_line_summary"),
        "executive_summary": synthesis.get("executive_summary"),
        "holistic_assessment": synthesis.get("holistic_assessment"),
        "key_conclusions": synthesis.get("key_conclusions"),
        "comparison_contract": analysis.get("comparison_contract") or {},
        "comparison_eligibility": analysis.get("comparison_eligibility") or analysis.get("comparison_contract") or {},
        "product_visibility": _deterministic_product_visibility(facts, analysis),
        "category_profile": foundation.get("category_profile") or {},
        "product_profile": foundation.get("product_profile") or {},
        "loop_closure": synthesis.get("loop_closure") or {},
        "s3_s4_relationship": synthesis.get("s3_s4_relationship") or {},
        "promise_chain": synthesis.get("promise_chain") or {},
        "video_understanding": facts,
        "stage_evidence_links": [],
        "stage_analysis": stage_results,
        "improvements": synthesis.get("improvements") or _deterministic_improvement(stage_results),
        "stage2_pipeline_version": "segmented_stage_v1",
        "stage2_candidate_status": candidate_status,
        "segmented_pipeline": {
            "version": "segmented_stage_v1",
            "stage_groups": group_records,
            "synthesis_status": synthesis_status,
            "synthesis_execution_source": synthesis_execution_source,
            "unresolved_stages": unresolved_stages,
            "candidate_status": candidate_status,
        },
    }
    return result


def _apply_live_postprocess_chain(
    *,
    args: argparse.Namespace,
    api_key: str,
    raw_result: dict[str, Any],
    analysis_input: str,
    run_dir: Path,
    analysis: dict[str, Any],
    locked_video_understanding: dict[str, Any] | None,
) -> dict[str, Any]:
    """Run the live-only Phase C/improvement pass before finalization.

    Stage2 owns bounded judgments and this function owns the single live
    post-judgment handoff.  The deterministic finalizer still runs afterwards
    and remains the only publisher of the final status.
    """
    result = _process_llm_result(
        raw_result,
        analysis,
        analysis_input,
        locked_video_understanding,
    )
    if (
        result.get("stage2_candidate_status") in {"degraded", "failed"}
        or result.get("stage2_pipeline_status") in {"degraded", "failed"}
    ):
        return result
    refined = maybe_refine_low_confidence_stages(
        args=args,
        api_key=api_key,
        raw_result=raw_result,
        result=result,
        analysis_input=analysis_input,
        run_dir=run_dir,
        analysis=analysis,
        locked_video_understanding=locked_video_understanding,
    )
    return maybe_reconcile_final_improvements(
        args=args,
        api_key=api_key,
        result=refined,
        analysis=analysis,
        analysis_input=analysis_input,
        locked_video_understanding=locked_video_understanding,
        run_dir=run_dir,
    )


def run_large_model_analysis(
    args: argparse.Namespace,
    analysis: dict[str, Any],
    analysis_input_path: Path,
    run_dir: Path,
) -> tuple[Path, dict[str, Any]] | None:
    """从 analysis_input.md 出发跑一次完整 LLM 分析，写出 analysis_result.json。"""
    if not args.llm_include_images:
        raise SystemExit(
            "旧的 text-only LLM 路径已移除；请删除 --no-llm-include-images，"
            "生产分析必须使用 Stage1 + segmented Stage2/Stage3 多模态链路。"
        )
    api_key = read_llm_api_key(args).strip()
    if not api_key and not args.llm_dry_run and not _full_provider_replay_requested(args):
        keychain_hint = ""
        if args.llm_api_key_keychain_service:
            keychain_hint = f" or Keychain service {args.llm_api_key_keychain_service}"
        raise SystemExit(f"Missing API key: set ${args.llm_api_key_env}{keychain_hint}, or use --llm-dry-run.")

    # 冻结 S1 命题尺子（人工策展）：先挂进 analysis，再跑 Step-0，避免品牌/型号空猜污染品地基。
    product = analysis.get("product") if isinstance(analysis.get("product"), dict) else {}
    brand_proposition = load_brand_proposition(
        run_dir,
        str(product.get("proposition_key") or ""),
    )
    if brand_proposition:
        analysis["brand_proposition"] = brand_proposition
    # Step-0：先确立品的商业地基（特征+命题），贯穿喂给阶段1 观察 + 阶段2 判断。
    foundation = _run_pipeline_phase(
        "product_foundation",
        "provider_or_normalization",
        lambda: establish_product_foundation(args, analysis, run_dir, api_key),
    )
    if foundation:
        analysis["product_foundation"] = foundation
    foundation_status = str(analysis.get("product_foundation_status") or "unknown").strip().lower()
    if foundation_status in {"failed", "degraded"} and not getattr(args, "allow_degraded", False):
        write_json(run_dir / "analysis.json", analysis)
        raise SystemExit(
            "Step-0 产品地基未完成；默认阻断后续分析，避免把未经验证的产品证明或内联猜测发布为完成结果。"
            " 如需审计性降级运行，请显式使用 --allow-degraded。"
        )
    facts = _run_pipeline_phase(
        "stage1_evidence",
        "evidence_supply_chain",
        lambda: run_video_fact_extraction(args, analysis, run_dir, api_key),
    )
    if args.llm_dry_run:
        print("LLM dry run: fact request payloads constructed in memory; no request artifacts retained")
        return None
    _run_pipeline_phase(
        "absolute_execution_shadow",
        "shadow_evaluation",
        lambda: maybe_run_absolute_execution_shadow(args, analysis, facts, run_dir, api_key),
    )
    comparison_contract = _run_pipeline_phase(
        "comparison_eligibility",
        "provider_or_normalization",
        lambda: establish_comparison_eligibility(args, facts, run_dir, api_key),
    )
    analysis["comparison_contract"] = comparison_contract
    analysis["comparison_eligibility"] = comparison_contract
    if comparison_contract.get("overall_status") in {"not_comparable", "uncertain"}:
        _run_pipeline_phase(
            "comparison_resolution",
            "deterministic_pipeline",
            lambda: _apply_non_comparable_result(analysis, facts, comparison_contract, run_dir),
        )
        return None
    analysis_input = _run_pipeline_phase(
        "analysis_input",
        "artifact_read",
        lambda: analysis_input_path.read_text(encoding="utf-8"),
    )
    segmented_result = _run_pipeline_phase(
        "stage2_judgment",
        "handoff_or_judgment",
        lambda: run_segmented_stage_pipeline(
            args,
            analysis,
            analysis_input,
            facts,
            run_dir,
            api_key,
        ),
    )
    segmented_result["analysis_run_metadata"] = {
        "llm_model": judgment_model(args),
        "judgment_model": judgment_model(args),
        "vision_model": vision_model(args),
        "llm_api_url": str(args.llm_api_url or ""),
        "multimodal_input": True,
        "pipeline": "segmented_stage_v1",
    }
    live_result = _run_pipeline_phase(
        "postprocess",
        "deterministic_pipeline",
        lambda: _apply_live_postprocess_chain(
            args=args,
            api_key=api_key,
            raw_result=segmented_result,
            analysis_input=analysis_input,
            run_dir=run_dir,
            analysis=analysis,
            locked_video_understanding=facts,
        ),
    )
    normalized = _run_pipeline_phase(
        "finalization",
        "finalizer",
        lambda: finalize_analysis_result(
            live_result,
            analysis,
            analysis_input,
            locked_video_understanding=facts,
        ),
    )
    # finalize_analysis_result is the single authority for the publish status.
    result_path = run_dir / "analysis_result.json"
    _run_pipeline_phase(
        "publish_artifact",
        "artifact_write",
        lambda: write_json(result_path, normalized),
    )
    return result_path, normalized


def maybe_run_absolute_execution_shadow(
    args: argparse.Namespace,
    analysis: dict[str, Any],
    facts: dict[str, Any],
    run_dir: Path,
    api_key: str,
) -> None:
    """按开关运行单侧执行审计；失败只记录结果，不得中断主分析。

    审计读取的仅是每侧 Stage1 锁定事实，因此不会接触另一侧视频或主对比的
    stage_analysis。当前是 shadow mode：结果不进入 severity，也不缓存评分结果；
    这样才能如实测量模型在相同 facts 下的方差，而不是把首次随机结果伪装成稳定。
    """
    if not getattr(args, "absolute_execution_shadow", False):
        return
    audit: dict[str, Any] = {"status": "pending", "roles": {}, "errors": [], "provider_meta": {}}
    for role in ("benchmark", "creator"):
        if not isinstance(facts.get(role), dict):
            audit["errors"].append(f"{role}: 缺少锁定单视频事实")
            continue
        try:
            request_path = run_dir / f"llm_absolute_execution_{role}_request.json"
            response_path = run_dir / f"llm_absolute_execution_{role}_response.json"
            payload = build_absolute_execution_shadow_payload(
                judgment_model(args), role, facts, analysis
            )
            write_json(request_path, payload)
            live_meta: dict[str, Any] = {}
            response, response_meta, execution_source = provider_call_with_artifact(
                artifact_path=run_dir / f"provider_absolute_execution_{role}.json",
                replay_root=getattr(args, "provider_replay_from", None),
                call_kind=f"absolute_execution_shadow:{role}",
                payload=payload,
                model=judgment_model(args),
                api_url=args.llm_api_url,
                response_meta=live_meta,
                call=lambda: (
                    parse_json_text(
                        fetch_json_completion(
                            args,
                            api_key,
                            request_path,
                            response_path,
                            response_meta=live_meta,
                        )
                    ),
                    live_meta,
                ),
            )
            parsed = normalize_absolute_execution_shadow(role, response)
            if parsed is None:
                raise SystemExit("单侧审计缺少完整 S1-S4 枚举输出")
            audit["roles"][role] = parsed
            audit["provider_meta"][role] = response_meta
            audit.setdefault("provider_artifacts", {})[role] = {
                "path": f"provider_absolute_execution_{role}.json",
                "execution_source": execution_source,
            }
            write_json(run_dir / f"absolute_execution_{role}.json", parsed)
        except (OSError, ValueError, RuntimeError, SystemExit, json.JSONDecodeError) as exc:
            if _is_strict_replay_failure(args, exc):
                raise
            audit["errors"].append(f"{role}: {exc}")
    audit["status"] = "completed" if len(audit["roles"]) == 2 else "partial" if audit["roles"] else "failed"
    analysis["absolute_execution_shadow"] = audit


def _is_permanent_llm_error(error_text: str) -> bool:
    """Return whether the transport error is a non-retryable HTTP 4xx failure."""
    return bool(re.search(r"\bHTTP\s+(?:400|401|403|404|405|422)\b", str(error_text or "")))


def preserve_valid_repair_sections(
    original: dict[str, Any] | None,
    repaired: dict[str, Any],
) -> dict[str, Any]:
    """Repair 只覆盖它实际输出的字段，避免清空完整阶段或嵌套 flag。"""
    if not isinstance(original, dict):
        return repaired

    def merge_value(original_value: Any, repaired_value: Any) -> Any:
        """Preserve omitted/null nested repair fields without inventing values."""
        if repaired_value is None or (isinstance(repaired_value, str) and not repaired_value.strip()):
            return json.loads(json.dumps(original_value, ensure_ascii=False))
        if isinstance(original_value, dict) and isinstance(repaired_value, dict):
            merged_value = dict(repaired_value)
            for nested_key, nested_original in original_value.items():
                if nested_key not in merged_value:
                    merged_value[nested_key] = json.loads(json.dumps(nested_original, ensure_ascii=False))
                else:
                    merged_value[nested_key] = merge_value(nested_original, merged_value[nested_key])
            return merged_value
        return repaired_value

    repaired = dict(repaired)
    original_stages = original.get("stage_analysis")
    repaired_stages = repaired.get("stage_analysis")
    if isinstance(original_stages, list) and isinstance(repaired_stages, list):
        merged_stages: list[Any] = []
        flag_names = {1: "hook", 2: "s2", 3: "s3", 4: "s4", 5: "s5", 6: "s6"}
        for index, repaired_stage in enumerate(repaired_stages, start=1):
            original_stage = original_stages[index - 1] if index <= len(original_stages) else None
            if not isinstance(original_stage, dict) or not isinstance(repaired_stage, dict):
                merged_stages.append(repaired_stage)
                continue
            merged_stage = dict(repaired_stage)
            for key, value in original_stage.items():
                if key in {"creator_module_id", "benchmark_module_id"} and (
                    key not in merged_stage
                    or merged_stage.get(key) is None
                    or (isinstance(merged_stage.get(key), str) and not merged_stage[key].strip())
                ):
                    merged_stage[key] = canonical_module_id(value, index)
                else:
                    merged_stage[key] = merge_value(value, merged_stage.get(key))

            # A failed first pass has already validated the stage-level evidence
            # context before repair is requested.  Do not let a full JSON repair
            # move an otherwise valid stage to another fact unit/time window.
            # Missing original context remains repairable; only valid, non-empty
            # original context is protected here.
            flag_name = flag_names.get(index)
            if flag_name:
                for role in ("benchmark", "creator"):
                    evidence_key = f"{role}_evidence_ids"
                    time_key = f"{role}_time_range"
                    flag_key = f"{role}_{flag_name}"
                    merged_flag = merged_stage.get(flag_key)
                    if isinstance(merged_flag, dict) and str(merged_flag.get("trust_basis") or "") in {
                        "product_claim",
                        "offer_or_spec",
                        "none",
                        "unknown",
                    }:
                        # These are already non-independent bases in the S5
                        # contract.  Keep their dependent booleans coherent
                        # even when repair omitted them and merge restored the
                        # original values.
                        merged_flag["exists"] = False
                        merged_flag["independent_trust_purpose"] = False
                    original_ids = [
                        str(value).strip()
                        for value in original_stage.get(evidence_key, [])
                        if str(value).strip()
                    ]
                    original_time = original_stage.get(time_key)
                    has_valid_context = bool(original_ids) and parse_time_range_seconds(original_time, None) is not None
                    if not has_valid_context:
                        continue
                    merged_stage[evidence_key] = json.loads(json.dumps(original_ids, ensure_ascii=False))
                    merged_stage[time_key] = json.loads(json.dumps(original_time, ensure_ascii=False))
                    original_flag = original_stage.get(flag_key)
                    original_flag_ids = (
                        [str(value).strip() for value in original_flag.get("evidence_ids", []) if str(value).strip()]
                        if isinstance(original_flag, dict)
                        else []
                    )
                    if (
                        isinstance(merged_flag, dict)
                        and original_flag_ids
                        and set(original_flag_ids).issubset(set(original_ids))
                    ):
                        merged_flag["evidence_ids"] = json.loads(
                            json.dumps(original_flag_ids, ensure_ascii=False)
                        )
            merged_stages.append(merged_stage)
        repaired["stage_analysis"] = merged_stages
    repaired_improvements = repaired.get("improvements")
    original_improvements = original.get("improvements")
    if (
        (not isinstance(repaired_improvements, list) or not repaired_improvements)
        and isinstance(original_improvements, list)
        and 1 <= len(original_improvements) <= 5
    ):
        repaired["improvements"] = json.loads(json.dumps(original_improvements, ensure_ascii=False))
    return repaired


def uncovered_large_stage_codes(result: dict[str, Any]) -> list[str]:
    """返回最终为 large、但 Top 提升点尚未覆盖的阶段。"""
    covered = {
        stage_code(item.get("target_stage"))
        for item in result.get("improvements", [])
        if isinstance(item, dict) and stage_code(item.get("target_stage"))
    }
    return [
        code
        for stage in result.get("stage_analysis", [])
        if isinstance(stage, dict)
        and (code := stage_code(stage.get("stage")))
        and str(stage.get("comparison_status") or "") not in {"not_directly_comparable", "not_applicable"}
        and str(stage.get("severity") or "").strip().lower() == "large"
        and code not in covered
    ]


def merge_reconciled_improvements(
    result: dict[str, Any],
    additions: list[dict[str, Any]],
    missing_stage_codes: list[str],
) -> dict[str, Any]:
    """把缺失阶段建议并入现有 Top 列表；最多五项，优先覆盖最终大差距。"""
    wanted = set(missing_stage_codes)
    valid_additions = [
        item for item in additions
        if isinstance(item, dict) and stage_code(item.get("target_stage")) in wanted
    ]
    additions_by_stage: dict[str, dict[str, Any]] = {}
    for item in valid_additions:
        additions_by_stage.setdefault(stage_code(item.get("target_stage")), item)

    merged = json.loads(json.dumps(result, ensure_ascii=False))
    stages_by_code = {
        stage_code(stage.get("stage")): stage
        for stage in merged.get("stage_analysis", [])
        if isinstance(stage, dict)
    }
    existing = [
        item
        for item in merged.get("improvements", [])
        if isinstance(item, dict)
        and str(stages_by_code.get(stage_code(item.get("target_stage")), {}).get("comparison_status") or "")
        not in {"not_directly_comparable", "not_applicable"}
    ]
    stage_severity = {
        stage_code(stage.get("stage")): str(stage.get("severity") or "uncertain").strip().lower()
        for stage in merged.get("stage_analysis", [])
        if isinstance(stage, dict)
    }
    projected_additions = []
    for code, item in additions_by_stage.items():
        stage = stages_by_code.get(code)
        if not isinstance(stage, dict):
            continue
        stage_for_projection = dict(stage)
        if stage_for_projection.get("model_gap_magnitude") not in {"none", "small", "medium", "large", "uncertain"}:
            stage_for_projection["model_gap_magnitude"] = stage_for_projection.get("severity") or "uncertain"
        projected = _project_synthesis_improvements([item], [stage_for_projection])
        if projected:
            projected_additions.append(projected[0])
    projected_codes = {
        stage_code(item.get("target_stage"))
        for item in projected_additions
        if isinstance(item, dict)
    }
    combined = [*projected_additions, *existing]
    severity_rank = {"large": 0, "medium": 1, "small": 2}
    combined.sort(
        key=lambda item: (
            severity_rank.get(stage_severity.get(stage_code(item.get("target_stage")), "uncertain"), 3),
            0 if stage_code(item.get("target_stage")) in projected_codes else 1,
            _safe_priority(item.get("priority")),
        )
    )
    merged["improvements"] = combined[:5]
    return merged


def _safe_priority(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 99


def maybe_reconcile_final_improvements(
    *,
    args: argparse.Namespace,
    api_key: str,
    result: dict[str, Any],
    analysis: dict[str, Any],
    analysis_input: str,
    locked_video_understanding: dict[str, Any] | None,
    run_dir: Path,
) -> dict[str, Any]:
    """最终 severity 与建议脱节时做一次纯文本补全；失败不得阻断主分析。"""
    missing = uncovered_large_stage_codes(result)
    if not missing or args.llm_dry_run:
        return result
    payload = build_improvement_reconciliation_payload(
        judgment_model(args), result, missing, analysis
    )
    request_path = run_dir / "llm_improvement_reconciliation_request.json"
    response_path = run_dir / "llm_improvement_reconciliation_response.json"
    write_json(request_path, payload)
    preserved = {
        key: result[key]
        for key in ("phase_c_review", "s4_visual_verifier")
        if key in result
    }
    response_meta: dict[str, Any] = {}
    live_meta: dict[str, Any] = {}
    try:
        provider_response, response_meta, execution_source = provider_call_with_artifact(
            artifact_path=run_dir / "provider_improvement_reconciliation.json",
            replay_root=getattr(args, "provider_replay_from", None),
            call_kind="improvement_reconciliation",
            payload=payload,
            model=judgment_model(args),
            api_url=args.llm_api_url,
            response_meta=live_meta,
            call=lambda: (
                json.loads(
                    call_llm_api(
                        args.llm_api_url,
                        api_key,
                        request_path,
                        response_path,
                        budget=getattr(args, "_resource_budget", None),
                        response_meta=live_meta,
                    )
                ),
                live_meta,
            ),
        )
        parsed = parse_json_text(extract_chat_completion_text(provider_response))
        additions = parsed.get("improvements") if isinstance(parsed.get("improvements"), list) else []
        merged = merge_reconciled_improvements(result, additions, missing)
        reconciled = _process_llm_result(
            merged,
            analysis,
            analysis_input,
            locked_video_understanding,
        )
        remaining = uncovered_large_stage_codes(reconciled)
        if any(code in remaining for code in missing):
            raise ValueError("补全结果未覆盖全部缺失的大差距阶段")
    except (Exception, SystemExit) as exc:  # 可选补全失败时保留主分析结果
        if _is_strict_replay_failure(args, exc):
            raise
        result["improvement_reconciliation"] = {
            "applied": False,
            "requested_stages": missing,
            "reason": f"最终提升点补全失败：{exc}",
            "provider_meta": response_meta,
            "provider_artifact": "provider_improvement_reconciliation.json",
        }
        _refresh_final_derived_artifact(analysis, result, ("improvement_reconciliation",))
        return result
    reconciled.update(preserved)
    reconciled["improvement_reconciliation"] = {
        "applied": True,
        "requested_stages": missing,
        "response_retention": "durable",
        "provider_meta": response_meta,
        "provider_artifact": "provider_improvement_reconciliation.json",
        "execution_source": execution_source,
    }
    _refresh_final_derived_artifact(
        analysis,
        reconciled,
        tuple([*preserved.keys(), "improvement_reconciliation"]),
    )
    return reconciled


def maybe_refine_low_confidence_stages(
    args: argparse.Namespace,
    api_key: str,
    raw_result: dict[str, Any],
    result: dict[str, Any],
    analysis_input: str,
    run_dir: Path,
    analysis: dict[str, Any],
    locked_video_understanding: dict[str, Any] | None,
) -> dict[str, Any]:
    """Phase C：模型主动声明低置信阶段后，只回看一次原生视频切片并重判。

    硬约束：
      - 只接受第一遍输出里的 low_confidence_stages；
      - 最多 2 个阶段；
      - 最多 1 次回看，不做循环；
      - facts 仍是唯一事实源，回看只修 stage_analysis。
    """
    if not locked_video_understanding:
        return result
    # Model uncertainty alone cannot spend the native-video budget. A stage
    # must also have an independent coverage, qualification, or resolver signal.
    legacy_critical_candidates = finalization_facade.legacy_phase_c_candidate_set(
        critical_severity_stages(result),
    )
    model_candidates = extract_low_confidence_stages(raw_result)
    coverage_candidates = detect_low_confidence_stages(result)
    temporal_candidates = [
        *detect_visual_coverage_gap_stages(result, analysis),
        *detect_unreferenced_visual_event_stages(result, analysis),
    ]
    conflict_candidates = [candidate.stage_id for candidate in legacy_critical_candidates.candidates]
    reason_map: dict[str, list[str]] = {}
    for code in coverage_candidates:
        reason_map.setdefault(code, []).append("stage_coverage_incomplete")
    for code in temporal_candidates:
        reason_map.setdefault(code, []).append("temporal_continuity_uncertain")
    for code in conflict_candidates:
        reason_map.setdefault(code, []).append("evidence_qualification_conflict")
    candidates: list[str] = []
    for code in [
        *coverage_candidates,
        *temporal_candidates,
        *conflict_candidates,
    ]:
        if code not in candidates:
            candidates.append(code)
    for code in model_candidates:
        if code in candidates:
            reason_map.setdefault(code, []).append("model_uncertainty_correlated")
    _priority = {"S1": 0, "S6": 0, "S4": 1}
    stage_codes = sorted(candidates, key=lambda c: (_priority.get(c, 2), candidates.index(c)))[:2]
    if not stage_codes:
        return result
    review_payload = build_stage_review_payload(
        vision_model(args),
        analysis,
        locked_video_understanding,
        result,
        stage_codes,
        budget=getattr(args, "_resource_budget", None),
        api_url=args.llm_api_url,
    )
    review_stages = [
        stage
        for stage in result.get("stage_analysis", [])
        if isinstance(stage, dict) and stage_code(stage.get("stage")) in stage_codes
    ]
    review_windows = stage_review_media_windows(
        analysis,
        review_stages,
        locked_video_understanding,
    )
    review_reasons = {
        code: list(dict.fromkeys(reason_map.get(code, [])))
        for code in stage_codes
    }
    request_bytes = _payload_size_bytes(review_payload)
    review_started_at = time.monotonic()
    if not payload_has_video(review_payload):
        result["phase_c_review"] = {
            "schema_version": PHASE_C_REVIEW_SCHEMA_VERSION,
            "mode": PHASE_C_REVIEW_MODE,
            "snapshot_schema": PHASE_C_PATCH_SNAPSHOT_SCHEMA,
            "requested_stages": stage_codes,
            "applied": False,
            "reason": "low_confidence_stages 已声明，但本地视频切片构造失败。",
            "patches": [],
            "trigger_reasons": review_reasons,
            "media_windows": review_windows,
            "request_bytes": request_bytes,
            "elapsed_seconds": round(max(0.0, time.monotonic() - review_started_at), 3),
            "effective_patch": {"changed_stage_count": 0},
        }
        _refresh_final_derived_artifact(analysis, result, ("phase_c_review",))
        return result

    review_request_path = run_dir / "llm_stage_review_request.json"
    review_response_path = run_dir / "llm_stage_review_response.json"
    write_json(review_request_path, review_payload)
    response_meta: dict[str, Any] = {}
    live_meta: dict[str, Any] = {}
    try:
        provider_response, response_meta, execution_source = provider_call_with_artifact(
            artifact_path=run_dir / "provider_phase_c.json",
            replay_root=getattr(args, "provider_replay_from", None),
            call_kind="phase_c_review",
            payload=review_payload,
            model=vision_model(args),
            api_url=args.llm_api_url,
            response_meta=live_meta,
            call=lambda: (
                json.loads(
                    call_llm_api(
                        args.llm_api_url,
                        api_key,
                        review_request_path,
                        review_response_path,
                        budget=getattr(args, "_resource_budget", None),
                        response_meta=live_meta,
                    )
                ),
                live_meta,
            ),
        )
        review_text = extract_chat_completion_text(provider_response)
        review_result = parse_json_text(review_text)
        refined = apply_stage_review_updates(
            result,
            review_result,
            analysis,
            analysis_input,
            locked_video_understanding,
            allowed_stage_codes=stage_codes,
            fallback_improvements=raw_result.get("improvements"),
        )
    except (OSError, ValueError, RuntimeError, SystemExit, json.JSONDecodeError) as exc:
        if _is_strict_replay_failure(args, exc):
            raise
        result["phase_c_review"] = {
            "schema_version": PHASE_C_REVIEW_SCHEMA_VERSION,
            "mode": PHASE_C_REVIEW_MODE,
            "snapshot_schema": PHASE_C_PATCH_SNAPSHOT_SCHEMA,
            "requested_stages": stage_codes,
            "applied": False,
            "reason": f"低置信阶段回看失败：{exc}",
            "patches": [],
            "provider_meta": response_meta,
            "provider_artifact": "provider_phase_c.json",
            "trigger_reasons": review_reasons,
            "media_windows": review_windows,
            "request_bytes": request_bytes,
            "elapsed_seconds": round(max(0.0, time.monotonic() - review_started_at), 3),
            "effective_patch": {"changed_stage_count": 0},
        }
        _refresh_final_derived_artifact(analysis, result, ("phase_c_review",))
        return result

    patches = _phase_c_patch_snapshots(result, refined, review_result)
    refined["phase_c_review"] = {
        "schema_version": PHASE_C_REVIEW_SCHEMA_VERSION,
        "mode": PHASE_C_REVIEW_MODE,
        "snapshot_schema": PHASE_C_PATCH_SNAPSHOT_SCHEMA,
        "requested_stages": stage_codes,
        "applied": True,
        "response_retention": "durable",
        "provider_meta": response_meta,
        "provider_artifact": "provider_phase_c.json",
        "execution_source": execution_source,
        "notes": review_result.get("review_notes", []),
        "patches": patches,
        "trigger_reasons": review_reasons,
        "media_windows": review_windows,
        "request_bytes": request_bytes,
        "elapsed_seconds": round(max(0.0, time.monotonic() - review_started_at), 3),
        "effective_patch": {"changed_stage_count": len(patches)},
    }
    _refresh_final_derived_artifact(analysis, refined, ("phase_c_review",))
    return refined


def extract_low_confidence_stages(raw_result: dict[str, Any]) -> list[str]:
    """从第一遍 LLM 输出中提取 S1-S6 低置信阶段代码。"""
    values = raw_result.get("low_confidence_stages")
    if values is None and isinstance(raw_result.get("quality_control"), dict):
        values = raw_result["quality_control"].get("low_confidence_stages")
    if not isinstance(values, list):
        return []
    codes: list[str] = []
    for value in values:
        text = str(value or "").strip().upper()
        if text.startswith("S") and len(text) >= 2:
            code = text[:2]
            if code in {"S1", "S2", "S3", "S4", "S5", "S6"} and code not in codes:
                codes.append(code)
    return codes[:2]


# 占位证据单元（_NO_STAGE_/_NO_USAGE/_NO_CTA 等）和"证据不足"提示，是后处理写入的
# 客观"素材不足"标记，不依赖模型自觉，可作为确定性回看触发信号。
_PLACEHOLDER_EVIDENCE_RE = re.compile(r"_NO_|NO_STAGE|NO_USAGE|NO_CTA")
_EVIDENCE_CAUTION_RE = re.compile(r"证据不足|待复核|需人工复核|未识别|未发现可|未验证|画面证据不足")


def detect_low_confidence_stages(result: dict[str, Any]) -> list[str]:
    """代码侧确定性兜底：用客观素材不足信号识别该回看的阶段，补模型自报的漏报。

    判据（针对达人侧——分析主体）：
    - 引用的是占位 evidence_unit（_NO_*），或 support_status=visual_only 且无有效口播/带待复核提示；
    - 且 severity ∈ {large, medium}：只有"薄证据上的高后果判断"才值得花一次回看。
    large 优先，最多 2 个；resolver 冲突与其他候选共享同一视频级预算。
    """
    creator_units = {
        str(unit.get("id")): unit
        for unit in result.get("video_understanding", {}).get("creator", {}).get("evidence_units", [])
        if isinstance(unit, dict)
    }
    large: list[str] = []
    medium: list[str] = []
    for stage in result.get("stage_analysis", []):
        if not isinstance(stage, dict):
            continue
        code = stage_code(stage.get("stage"))
        severity = str(stage.get("severity") or "").strip().lower()
        if not code or severity not in {"large", "medium"}:
            continue
        creator_side = result.get("video_understanding", {}).get("creator", {})
        ids = [str(value) for value in stage.get("creator_evidence_ids", [])]
        if (
            isinstance(creator_side, dict)
            and creator_side.get("stage_evidence_contract_version") == STAGE_EVIDENCE_CONTRACT_VERSION
        ):
            ids = [value for value in ids if value in qualified_stage_evidence_ids(creator_side, code)]
        has_placeholder = any(_PLACEHOLDER_EVIDENCE_RE.search(item) for item in ids)
        unit_visual = " ".join(str(creator_units.get(item, {}).get("visual_fact", "")) for item in ids)
        stage_visual = " ".join(str(value) for value in stage.get("creator_visual_evidence", []))
        has_caution = bool(_EVIDENCE_CAUTION_RE.search(unit_visual + " " + stage_visual))
        visual_only = str(stage.get("creator_support_status") or "") == "visual_only"
        no_voice = not str(stage.get("creator_quote") or "").strip()
        if has_placeholder or (visual_only and (has_caution or no_voice)):
            (large if severity == "large" else medium).append(code)
    ordered: list[str] = []
    for code in [*large, *medium]:
        if code not in ordered:
            ordered.append(code)
    return ordered[:2]


def detect_visual_coverage_gap_stages(
    result: dict[str, Any],
    analysis: dict[str, Any],
) -> list[str]:
    """Find high-consequence stage windows with no canonical frame coverage.

    This is deliberately independent of model evidence IDs.  A confident
    Stage 1 omission cannot trigger a review through an evidence reference it
    never produced, so the Phase C candidate set also inspects the actual
    selected-frame manifest for both roles.
    """
    videos = analysis.get("videos") if isinstance(analysis.get("videos"), dict) else {}
    if not videos:
        return []
    gaps: list[str] = []
    for stage in result.get("stage_analysis", []):
        if not isinstance(stage, dict):
            continue
        code = stage_code(stage.get("stage"))
        severity = str(stage.get("severity") or "").strip().lower()
        if not code or severity not in {"large", "medium"}:
            continue
        missing_role = False
        for role in ("creator", "benchmark"):
            info = videos.get(role) if isinstance(videos.get(role), dict) else {}
            parsed = parse_time_range_seconds(
                stage.get(f"{role}_time_range"),
                info.get("duration_seconds"),
            )
            if parsed is None:
                continue
            start, end = parsed
            entries = get_analysis_frame_entries(info)
            covered = any(
                (timestamp := _finite_timestamp(entry.get("timestamp_seconds"))) is not None
                and start <= timestamp <= end
                for entry in entries
                if isinstance(entry, dict)
            )
            if not covered:
                missing_role = True
                break
        if missing_role and code not in gaps:
            gaps.append(code)
    return gaps


def detect_unreferenced_visual_event_stages(
    result: dict[str, Any],
    analysis: dict[str, Any],
) -> list[str]:
    """Find high-consequence stages whose visual events are not cited.

    This catches a confident omission even when Stage 1 produced no matching
    evidence ID.  It only creates a Phase C candidate; the locked evidence
    contract still prevents Phase C from inventing a new fact.
    """
    videos = analysis.get("videos") if isinstance(analysis.get("videos"), dict) else {}
    video_understanding = result.get("video_understanding")
    if not isinstance(video_understanding, dict):
        video_understanding = {}
    units_by_role = {
        role: {
            str(unit.get("id")): unit
            for unit in (video_understanding.get(role, {}).get("evidence_units", []) or [])
            if isinstance(unit, dict) and unit.get("id")
        }
        for role in ("creator", "benchmark")
    }
    event_reasons = {
        "scene_boundary",
        "subtitle_boundary",
        "speech_boundary",
        "local_change",
        "action_change",
        "global_change",
        "focus_hook",
        "focus_cta",
    }
    candidates: list[str] = []
    for stage in result.get("stage_analysis", []):
        if not isinstance(stage, dict):
            continue
        code = stage_code(stage.get("stage"))
        severity = str(stage.get("severity") or "").strip().lower()
        if not code or severity not in {"large", "medium"}:
            continue
        stage_needs_review = False
        for role in ("creator", "benchmark"):
            info = videos.get(role) if isinstance(videos.get(role), dict) else {}
            parsed = parse_time_range_seconds(stage.get(f"{role}_time_range"), info.get("duration_seconds"))
            if parsed is None:
                continue
            start, end = parsed
            entries = [
                entry
                for entry in get_analysis_frame_entries(info)
                if isinstance(entry, dict)
                and (timestamp := _finite_timestamp(entry.get("timestamp_seconds"))) is not None
                and start <= timestamp <= end
                and event_reasons.intersection({str(item) for item in entry.get("selection_reasons", [])})
            ]
            if not entries:
                continue
            referenced_ids = {str(value) for value in stage.get(f"{role}_evidence_ids", [])}
            side = video_understanding.get(role) if isinstance(video_understanding.get(role), dict) else {}
            if side.get("stage_evidence_contract_version") == STAGE_EVIDENCE_CONTRACT_VERSION:
                referenced_ids &= qualified_stage_evidence_ids(side, code)
            referenced_units = [
                unit for unit_id, unit in units_by_role[role].items() if unit_id in referenced_ids
            ]
            for entry in entries:
                timestamp = _finite_timestamp(entry.get("timestamp_seconds"))
                if timestamp is None:
                    continue
                point = f"{timestamp:.3f}s - {timestamp + 0.001:.3f}s"
                if not any(evidence_overlaps_range(unit, point) for unit in referenced_units):
                    stage_needs_review = True
                    break
            if stage_needs_review:
                break
        if stage_needs_review and code not in candidates:
            candidates.append(code)
    return candidates


def _finite_timestamp(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def payload_has_video(payload: dict[str, Any]) -> bool:
    """判断回看 payload 是否真正挂了 video_url。"""
    for message in payload.get("messages", []):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        if any(isinstance(item, dict) and item.get("type") == "video_url" for item in content):
            return True
    return False


def payload_has_audio(payload: dict[str, Any]) -> bool:
    """Return whether the exact request contains a standalone audio block."""
    for message in payload.get("messages", []):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        if any(isinstance(item, dict) and item.get("type") == "input_audio" for item in content):
            return True
    return False


def payload_has_direct_audio(
    payload: dict[str, Any],
    *,
    api_url: str,
    model: str,
) -> bool:
    """Return whether this provider can perceive audio in the exact request."""
    if not can_analyze_native_audio(api_url, model):
        return False
    return payload_has_video(payload) or payload_has_audio(payload)


def _payload_size_bytes(payload: dict[str, Any]) -> int:
    """Count the serialized request without materializing a second media copy."""
    total = 0

    class _CountingWriter:
        def write(self, value: str) -> int:
            nonlocal total
            size = len(value.encode("utf-8"))
            total += size
            return len(value)

    # Match api._write_request_json so request_bytes is the actual wire body,
    # not an optimistic compact-JSON estimate.
    json.dump(payload, _CountingWriter(), ensure_ascii=False)
    return total


def _visual_input_timestamps(visual_inputs: list[dict[str, Any]]) -> list[float]:
    """Return timestamps for the exact sampled images sent in this request.

    A timeline overview has no single timestamp, but its code-owned provenance
    records every canonical source frame visibly embedded in that image. Only
    those verified source timestamps may qualify a time-bounded visual fact.
    """
    values: list[float] = []
    for item in visual_inputs:
        if not isinstance(item, dict):
            continue
        timestamp = _finite_timestamp(item.get("timestamp_seconds"))
        if timestamp is not None and timestamp not in values:
            values.append(timestamp)
        source_timestamps = item.get("source_frame_timestamps")
        if isinstance(source_timestamps, list):
            for raw_timestamp in source_timestamps:
                source_timestamp = _finite_timestamp(raw_timestamp)
                if source_timestamp is not None and source_timestamp not in values:
                    values.append(source_timestamp)
    return sorted(values)


def apply_stage_review_updates(
    current_result: dict[str, Any],
    review_result: dict[str, Any],
    analysis: dict[str, Any],
    analysis_input: str,
    locked_video_understanding: dict[str, Any],
    *,
    allowed_stage_codes: list[str],
    fallback_improvements: Any = None,
) -> dict[str, Any]:
    """Apply only Phase C's closed evidence-and-fact patch, then re-finalize.

    Phase C cannot replace a stage or directly write conclusions. Every legal
    patch returns to the shared finalizer, which repairs, validates and invokes
    the resolver again.
    """
    patches_by_code = _validate_stage_review_patches(
        review_result,
        locked_video_understanding,
        allowed_stage_codes,
        current_result=current_result,
    )

    merged = json.loads(json.dumps(current_result, ensure_ascii=False))
    # Phase C 只更新阶段。若初轮后处理已过滤掉所有提升点，恢复初轮已通过 schema 的原始条目，
    # 使重走统一收口时满足 raw envelope；最终仍由既有过滤/重排逻辑决定是否保留。
    if not merged.get("improvements") and isinstance(fallback_improvements, list) and fallback_improvements:
        merged["improvements"] = json.loads(json.dumps(fallback_improvements, ensure_ascii=False))
    merged_stages = []
    for stage in merged.get("stage_analysis", []):
        code = stage_code(stage.get("stage"))
        if code in patches_by_code:
            base_stage = dict(stage)
            # Net multimodal assessment is not an allowed Phase C patch. Drop
            # the stale aggregate instead of letting a fresh fact patch inherit
            # an earlier conclusion.
            base_stage.pop("creator_multimodal", None)
            base_stage.pop("benchmark_multimodal", None)
            # Postprocess markers are derived from prior facts and must be
            # rebuilt after any patch.
            base_stage.pop("_postprocess_state", None)
            base_stage.update(patches_by_code[code])
            merged_stages.append(base_stage)
        else:
            merged_stages.append(stage)
    merged["stage_analysis"] = merged_stages
    return _process_llm_result(merged, analysis, analysis_input, locked_video_understanding)


def _validate_stage_review_patches(
    review_result: dict[str, Any],
    locked_video_understanding: dict[str, Any],
    allowed_stage_codes: list[str],
    *,
    current_result: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Reject Phase C output outside the closed patch contract before merging."""
    if not isinstance(review_result, dict):
        raise SystemExit("Phase C review must be a JSON object.")
    unexpected_top_level = set(review_result) - {"stage_patches", "review_notes"}
    if unexpected_top_level:
        raise SystemExit(
            "Phase C review contains unsupported top-level fields: "
            + ", ".join(sorted(unexpected_top_level))
            + "."
        )
    patches = review_result.get("stage_patches")
    if not isinstance(patches, list) or not patches:
        raise SystemExit("Phase C review returned no stage_patches.")

    allowed = {stage_code(code) for code in allowed_stage_codes}
    available_units = _phase_c_available_evidence_units(locked_video_understanding)
    current_stages = {
        stage_code(stage.get("stage")): stage
        for stage in (current_result or {}).get("stage_analysis", [])
        if isinstance(stage, dict) and stage_code(stage.get("stage"))
    }
    patches_by_code: dict[str, dict[str, Any]] = {}
    for patch in patches:
        if not isinstance(patch, dict):
            raise SystemExit("Each Phase C stage patch must be an object.")
        if set(patch) != {"stage", "fields"}:
            raise SystemExit("Each Phase C stage patch may contain only stage and fields.")
        code = stage_code(patch.get("stage"))
        if not code or code not in allowed:
            raise SystemExit(f"Phase C patch targets a stage that was not requested: {patch.get('stage')!r}.")
        if code in patches_by_code:
            raise SystemExit(f"Phase C review contains duplicate patch for {code}.")
        fields = patch.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise SystemExit(f"Phase C patch for {code} must contain non-empty fields.")
        allowed_fields = set(patch_fields_for_stage(code))
        illegal_fields = set(fields) - allowed_fields
        if illegal_fields:
            raise SystemExit(
                f"Phase C patch for {code} attempts to modify protected fields: "
                + ", ".join(sorted(illegal_fields))
                + "."
            )
        required_fields = set(patch_fields_for_stage(code))
        if set(fields) != required_fields:
            missing = required_fields - set(fields)
            raise SystemExit(
                f"Phase C patch for {code} must replace its complete fact pair; missing: "
                + ", ".join(sorted(missing))
                + "."
            )
        _validate_phase_c_patch_evidence_ids(
            code,
            fields,
            _phase_c_available_evidence_ids(locked_video_understanding, code),
            available_units,
            target_stage=current_stages.get(code),
        )
        patches_by_code[code] = json.loads(json.dumps(fields, ensure_ascii=False))
    missing_requested = allowed - set(patches_by_code)
    if missing_requested:
        raise SystemExit(
            "Phase C review omitted requested stage patches: " + ", ".join(sorted(missing_requested)) + "."
        )
    return patches_by_code


def _phase_c_available_evidence_ids(facts: dict[str, Any], stage_code_value: str | None = None) -> dict[str, set[str]]:
    return {
        role: {
            str(unit.get("id"))
            for unit in ((facts.get(role) or {}).get("evidence_units") or [])
            if isinstance(unit, dict) and str(unit.get("id") or "").strip()
            and (
                (facts.get(role) or {}).get("stage_evidence_contract_version") != STAGE_EVIDENCE_CONTRACT_VERSION
                or stage_code_value is None
                or str(unit.get("id")) in qualified_stage_evidence_ids(facts.get(role), stage_code_value)
            )
        }
        for role in ("creator", "benchmark")
    }


def _phase_c_available_evidence_units(
    facts: dict[str, Any],
) -> dict[str, dict[str, dict[str, Any]]]:
    return {
        role: {
            str(unit.get("id")): unit
            for unit in ((facts.get(role) or {}).get("evidence_units") or [])
            if isinstance(unit, dict) and str(unit.get("id") or "").strip()
        }
        for role in ("creator", "benchmark")
    }


def _validate_phase_c_patch_evidence_ids(
    code: str,
    fields: dict[str, Any],
    available_ids: dict[str, set[str]],
    available_units: dict[str, dict[str, dict[str, Any]]],
    *,
    target_stage: dict[str, Any] | None = None,
) -> None:
    for role in ("creator", "benchmark"):
        evidence_key = f"{role}_evidence_ids"
        evidence_ids = fields.get(evidence_key)
        fact_key = f"{role}_{code.lower()}" if code != "S1" else f"{role}_hook"
        fact = fields.get(fact_key)
        if not isinstance(fact, dict):
            raise SystemExit(f"Phase C patch for {code} requires object field {fact_key}.")
        allows_empty_evidence = _phase_c_allows_empty_evidence(code, fact)
        if not isinstance(evidence_ids, list):
            raise SystemExit(f"Phase C patch for {code} requires {evidence_key} to be an array.")
        normalized_ids = [str(item).strip() for item in evidence_ids]
        if not normalized_ids and not allows_empty_evidence:
            raise SystemExit(f"Phase C patch for {code} requires non-empty {evidence_key}.")
        if any(not item for item in normalized_ids):
            raise SystemExit(f"Phase C patch for {code} has blank {evidence_key}.")
        if len(normalized_ids) != len(set(normalized_ids)):
            raise SystemExit(f"Phase C patch for {code} has duplicate {evidence_key}.")
        missing = sorted(set(normalized_ids) - available_ids[role])
        if missing:
            raise SystemExit(
                f"Phase C patch for {code} references unknown {role} evidence: {', '.join(missing)}."
            )
        for nested_key in ("evidence_ids", "trust_source_evidence_ids"):
            nested_ids = fact.get(nested_key)
            if nested_ids is None:
                continue
            if not isinstance(nested_ids, list):
                raise SystemExit(f"Phase C patch for {code} has invalid {fact_key}.{nested_key}.")
            normalized_nested = [str(item).strip() for item in nested_ids]
            if (
                nested_key == "evidence_ids"
                and not normalized_nested
                and not allows_empty_evidence
            ):
                raise SystemExit(
                    f"Phase C patch for {code} requires non-empty {fact_key}.{nested_key}."
                )
            if any(not item for item in normalized_nested):
                raise SystemExit(f"Phase C patch for {code} has blank {fact_key}.{nested_key}.")
            if len(normalized_nested) != len(set(normalized_nested)):
                raise SystemExit(f"Phase C patch for {code} has duplicate {fact_key}.{nested_key}.")
            nested_missing = sorted(set(normalized_nested) - available_ids[role])
            if nested_missing:
                raise SystemExit(
                    f"Phase C patch for {code} references unknown {role} evidence in {fact_key}.{nested_key}: "
                    + ", ".join(nested_missing)
                    + "."
                )
            outside_stage = sorted(set(normalized_nested) - set(normalized_ids))
            if outside_stage:
                raise SystemExit(
                    f"Phase C patch for {code} {fact_key}.{nested_key} must be a subset of "
                    f"{evidence_key}: "
                    + ", ".join(outside_stage)
                    + "."
                )
            stage_range = (target_stage or {}).get(f"{role}_time_range")
            if stage_range is not None:
                outside_time = [
                    evidence_id
                    for evidence_id in normalized_nested
                    if not evidence_overlaps_range(
                        available_units[role][evidence_id],
                        stage_range,
                    )
                ]
                if outside_time:
                    raise SystemExit(
                        f"Phase C patch for {code} {fact_key}.{nested_key} must overlap the target stage time range: "
                        + ", ".join(outside_time)
                        + "."
                    )


def _phase_c_allows_empty_evidence(code: str, fact: dict[str, Any]) -> bool:
    """Delegate the closed empty-evidence policy to the shared stage contract."""
    return stage_flag_allows_empty_evidence(code, fact)


def _phase_c_patch_snapshots(
    before_result: dict[str, Any],
    after_result: dict[str, Any],
    review_result: dict[str, Any],
) -> list[dict[str, Any]]:
    """Record versioned patch-before/patch-after snapshots, never full stages."""
    before_stages = {
        stage_code(stage.get("stage")): stage
        for stage in before_result.get("stage_analysis") or []
        if isinstance(stage, dict)
    }
    after_stages = {
        stage_code(stage.get("stage")): stage
        for stage in after_result.get("stage_analysis") or []
        if isinstance(stage, dict)
    }
    snapshots: list[dict[str, Any]] = []
    for patch in review_result.get("stage_patches") or []:
        if not isinstance(patch, dict):
            continue
        code = stage_code(patch.get("stage"))
        fields = patch.get("fields")
        if not code or not isinstance(fields, dict):
            continue
        snapshots.append(
            {
                "stage": code,
                "applied_fields": sorted(fields),
                "before": _phase_c_stage_snapshot(before_stages.get(code) or {}, code),
                "after": _phase_c_stage_snapshot(after_stages.get(code) or {}, code),
                "finalization": {
                    "repair_validation_resolver": "completed",
                    "resolver_status": str(
                        ((after_stages.get(code) or {}).get("severity_derivation") or {}).get("status") or "unknown"
                    ),
                },
            }
        )
    return snapshots


def _phase_c_stage_snapshot(stage: dict[str, Any], code: str) -> dict[str, Any]:
    fields = {
        field: copy.deepcopy(stage[field])
        for field in patch_fields_for_stage(code)
        if field in stage
    }
    trace = stage.get("severity_derivation") if isinstance(stage.get("severity_derivation"), dict) else {}
    return {
        "patchable_fields": fields,
        "resolution": {
            "model_severity": stage.get("model_severity"),
            "severity": stage.get("severity"),
            "resolver_status": trace.get("status"),
            "constraints": copy.deepcopy(trace.get("constraints") or []),
        },
    }


def stage_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if len(text) >= 2 and text[:2] in {"S1", "S2", "S3", "S4", "S5", "S6"}:
        return text[:2]
    return ""


def _process_llm_result(
    result: dict[str, Any],
    analysis: dict[str, Any],
    analysis_input: str,
    locked_video_understanding: dict[str, Any] | None,
) -> dict[str, Any]:
    """Run the in-memory preflight needed to choose optional review work."""
    # Preflight is allowed to derive a candidate for Phase C selection, but it
    # must not mutate the provider-owned response that the publish pass later
    # stores as raw_model_response.json.
    candidate = copy.deepcopy(result)
    analysis_candidate = copy.deepcopy(analysis)
    return finalize_analysis_result(
        candidate,
        analysis_candidate,
        analysis_input,
        locked_video_understanding,
        persist_artifacts=False,
    )


# ---------------------------------------------------------------------------
# 单视频事实抽取
# ---------------------------------------------------------------------------

def establish_product_foundation(
    args: argparse.Namespace,
    analysis: dict[str, Any],
    run_dir: Path,
    api_key: str,
) -> dict[str, Any] | None:
    """Step-0：看视频前先据产品事实 + 品类世界知识确立品的商业地基（category_profile 特征 +
    product_profile 命题），存 product_foundation.json 并返回，供阶段1 观察、阶段2 判断、4d 政策消费。
    失败返回 None，并写入 failed 状态；调用方必须显式选择是否允许 degraded。"""
    if not has_product_foundation_anchor(analysis):
        analysis["product_foundation_status"] = "not_applicable"
        print(
            "Step-0 跳过：缺少品类/卖点/目标用户/人工命题等可靠锚点；状态记为 not_applicable。",
            flush=True,
        )
        return None
    cache_path = run_dir / "product_foundation.json"
    cache_key = _product_foundation_cache_key(args, analysis)
    if getattr(args, "reuse_preprocessing", False) and cache_path.is_file():
        foundation = _read_cache_result(
            cache_path,
            "foundation",
            cache_key,
            validator=_is_valid_foundation_cache,
        )
        if foundation is not None:
            _stamp_proof_contract_source(foundation, analysis)
            cached_reason = product_foundation_validation_reason(foundation.get("product_profile"))
            analysis["product_foundation_status"] = "degraded" if cached_reason else "completed"
            if cached_reason:
                analysis["product_foundation_error"] = f"缓存的 Step-0 产品证明合同未闭合：{cached_reason}"
            return foundation
    payload = build_product_foundation_payload(judgment_model(args), analysis)
    request_path = run_dir / "llm_product_foundation_request.json"
    response_path = run_dir / "llm_product_foundation_response.json"
    provider_meta: dict[str, Any] = {"initial": {}, "repair": {}}
    write_json(request_path, payload)
    if args.llm_dry_run:
        request_path.unlink(missing_ok=True)
        analysis["product_foundation_status"] = "deferred_dry_run"
        return None
    try:
        response, initial_meta, _ = provider_call_with_artifact(
            artifact_path=run_dir / "provider_product_foundation.json",
            replay_root=getattr(args, "provider_replay_from", None),
            call_kind="product_foundation",
            payload=payload,
            model=judgment_model(args),
            api_url=args.llm_api_url,
            response_meta=provider_meta["initial"],
            call=lambda: (
                parse_json_text(
                    fetch_json_completion(
                        args,
                        api_key,
                        request_path,
                        response_path,
                        request_max_time_seconds=240,
                        response_meta=provider_meta["initial"],
                    )
                ),
                provider_meta["initial"],
            ),
        )
        provider_meta["initial"] = initial_meta
        raw = response
        foundation = {
            "category_profile": normalize_category_profile(raw.get("category_profile")),
            "product_profile": normalize_product_profile(raw.get("product_profile")),
        }
        _stamp_proof_contract_source(foundation, analysis)
        validation_reason = product_foundation_validation_reason(foundation.get("product_profile"))
        repaired_reason = ""
        if validation_reason:
            repair_payload = build_product_foundation_repair_payload(
                judgment_model(args),
                analysis,
                raw.get("product_profile") if isinstance(raw.get("product_profile"), dict) else {},
                validation_reason,
            )
            repair_request_path = run_dir / "llm_product_foundation_repair_request.json"
            repair_response_path = run_dir / "llm_product_foundation_repair_response.json"
            write_json(repair_request_path, repair_payload)
            repaired_response, repair_meta, _ = provider_call_with_artifact(
                artifact_path=run_dir / "provider_product_foundation_repair.json",
                replay_root=getattr(args, "provider_replay_from", None),
                call_kind="product_foundation_repair",
                payload=repair_payload,
                model=judgment_model(args),
                api_url=args.llm_api_url,
                response_meta=provider_meta["repair"],
                call=lambda: (
                    parse_json_text(
                        fetch_json_completion(
                            args,
                            api_key,
                            repair_request_path,
                            repair_response_path,
                            request_max_time_seconds=240,
                            response_meta=provider_meta["repair"],
                        )
                    ),
                    provider_meta["repair"],
                ),
            )
            provider_meta["repair"] = repair_meta
            repaired_raw = repaired_response
            foundation = {
                "category_profile": normalize_category_profile(repaired_raw.get("category_profile")),
                "product_profile": normalize_product_profile(repaired_raw.get("product_profile")),
            }
            _stamp_proof_contract_source(foundation, analysis)
            repaired_reason = product_foundation_validation_reason(foundation.get("product_profile"))
            if repaired_reason:
                # 二次回答仍不合格时保留地基，但显式降级，禁止下游把旧视觉字段当强证据。
                if foundation["product_profile"] is not None:
                    foundation["product_profile"]["short_video_proof_plan"] = {
                        "candidates": [],
                        "s4_anchor_candidate_id": "",
                        "selection_source": "model_category_default",
                        "anchor_confidence": "low",
                        "valid": False,
                        "validation_reason": f"Step-0 重答后仍无有效 short_video_proof_plan：{repaired_reason}",
                    }
                    foundation["product_profile"]["proof_contract"] = {
                        "anchor_candidate_id": "",
                        "mode": "trust_substituted",
                        "consumer_outcome": "",
                        "signal_type": "",
                        "observable_dimension": "",
                        "observable_signal": "",
                        "before_state": "",
                        "after_state": "",
                        "proof_condition": "",
                        "valid": False,
                        "validation_reason": f"Step-0 重答后仍无有效产品证明合同：{repaired_reason}",
                    }
                    foundation["product_profile"]["visual_proof_points"] = []
        foundation_status = "completed"
        if repaired_reason:
            foundation_status = "degraded"
            analysis["product_foundation_error"] = f"Step-0 产品证明合同未闭合：{repaired_reason}"
        if not foundation["category_profile"] and not foundation["product_profile"]:
            raise ValueError("category_profile 与 product_profile 均为空")
    except (Exception, SystemExit) as exc:  # noqa: BLE001
        if _is_strict_replay_failure(args, exc):
            raise
        analysis["product_foundation_status"] = "failed"
        analysis["product_foundation_error"] = str(exc)[:500]
        print(f"Step-0 品地基确立失败，状态记为 failed：{exc}", flush=True)
        write_json(run_dir / "product_foundation_provider_meta.json", provider_meta)
        return None
    write_json(run_dir / "product_foundation_provider_meta.json", provider_meta)
    _write_cache_result(cache_path, {**cache_key, "foundation": foundation})
    analysis["product_foundation_status"] = foundation_status
    return foundation


def establish_comparison_eligibility(
    args: argparse.Namespace,
    facts: dict[str, Any],
    run_dir: Path,
    api_key: str,
) -> dict[str, Any]:
    """独立判定双视频能否做产品级比较；失败时保守退回 uncertain。"""
    payload = build_comparison_eligibility_payload(judgment_model(args), facts)
    request_path = run_dir / "llm_comparison_eligibility_request.json"
    response_path = run_dir / "llm_comparison_eligibility_response.json"
    response_meta: dict[str, Any] = {}
    execution_source = "live"
    write_json(request_path, payload)
    try:
        response, response_meta, execution_source = provider_call_with_artifact(
            artifact_path=run_dir / "provider_comparison_eligibility.json",
            replay_root=(
                getattr(args, "provider_replay_from", None)
                or getattr(args, "stage2_replay_from", None)
            ),
            resume_root=getattr(args, "stage2_resume_from", None),
            call_kind="comparison_eligibility",
            payload=payload,
            model=judgment_model(args),
            api_url=args.llm_api_url,
            response_meta=response_meta,
            call=lambda: (
                parse_json_text(
                    fetch_json_completion(
                        args,
                        api_key,
                        request_path,
                        response_path,
                        response_meta=response_meta,
                    )
                ),
                response_meta,
            ),
        )
        eligibility = normalize_comparison_contract(response)
        if eligibility["overall_status"] == "uncertain" and not eligibility["reason"]:
            eligibility["reason"] = "双侧产品身份不足以确认产品级比较资格。"
    except (Exception, SystemExit) as exc:  # 资格层不允许阻断主分析；uncertain 会阻止后续误用为直接产品比较。
        if _is_strict_replay_failure(args, exc):
            raise
        eligibility = normalize_comparison_contract(
            {"reason": f"产品级比较资格判定失败，保守按 uncertain 处理：{exc}"}
        )
    eligibility = _stamp_facts_eligibility(eligibility)
    eligibility = _apply_operator_scope_override(
        eligibility,
        getattr(args, "comparison_scope_override", None),
    )
    eligibility = apply_fact_scoped_s5_comparison_contract(eligibility, facts)
    write_json(run_dir / "comparison_contract.json", eligibility)
    write_json(run_dir / "comparison_eligibility.json", eligibility)
    write_json(run_dir / "comparison_provider_meta.json", response_meta)
    write_json(
        run_dir / "comparison_provider_artifact_ref.json",
        {"path": "provider_comparison_eligibility.json", "execution_source": execution_source},
    )
    return eligibility


def _stamp_facts_eligibility(eligibility: dict[str, Any]) -> dict[str, Any]:
    """让事实预检的审计字段与其判定结论保持一致。

    资格预检的唯一输入是锁定的双侧产品身份事实，因此不能保留主比较模型或
    normalize 默认值带来的 ``facts_scope=uncertain``。人工结构对标覆盖在此之后
    单独处理，并把这份事实结论作为原始审计记录保留下来。
    """
    normalized = normalize_comparison_contract(eligibility)
    normalized["scope_origin"] = "facts"
    normalized["facts_scope"] = normalized["scope"]
    normalized["facts_reason"] = normalized["reason"]
    return normalized


def _apply_operator_scope_override(
    facts_eligibility: dict[str, Any],
    override: str | None,
) -> dict[str, Any]:
    """应用人工确认的结构对标范围，同时保留 facts 身份审计结论。

    模型绝不能自行把跨品样本升级为同任务结构对标。该范围只能由运营或验证清单显式提供，
    因此未来线上任务也应传递元数据，而不能从目录名或产品名推断。
    """
    if override != "same_task_structure":
        return facts_eligibility
    raw_stage_eligibility = (
        facts_eligibility.get("stage_eligibility")
        if isinstance(facts_eligibility.get("stage_eligibility"), dict)
        else {}
    )
    raw_s5 = raw_stage_eligibility.get("S5") if isinstance(raw_stage_eligibility.get("S5"), dict) else None
    overridden = normalize_comparison_contract(facts_eligibility)
    overridden["scope_origin"] = "operator_certified"
    overridden["facts_scope"] = str(facts_eligibility.get("scope") or "uncertain")
    overridden["facts_reason"] = str(facts_eligibility.get("reason") or "双侧产品身份事实不足。")
    if overridden.get("identity_relation") == "different_product":
        overridden["substitution_relation"] = "strong_substitute"
        shared = dict(overridden.get("shared_job") or {})
        shared.update(
            {
                "same_consumer_job": True,
                "same_target_object": True,
                "same_desired_outcome": True,
                "same_purchase_decision": True,
                "complement_or_dependency": False,
            }
        )
        overridden["shared_job"] = shared
        stage_eligibility = dict(overridden.get("stage_eligibility") or {})
        for stage in ("S1", "S2", "S3", "S4", "S6"):
            current = dict(stage_eligibility.get(stage) or {})
            current.update(
                {
                    "status": "structural",
                    "basis": "运营已确认双方共享消费者任务、目标结果与购买决策，仅比较该阶段的内容结构和执行完成度。",
                    "shared_contract": "同任务强替代产品的结构对标",
                }
            )
            stage_eligibility[stage] = current
        if raw_s5 is not None:
            s5 = dict(raw_s5)
            if s5.get("status") == "direct":
                s5["status"] = "structural"
            stage_eligibility["S5"] = s5
        overridden["stage_eligibility"] = stage_eligibility
        shared["reason"] = "运营确认双方共享消费者任务、作用对象、目标结果与购买决策。"
    overridden["reason"] = (
        "运营确认双方属于同任务强替代产品；S1-S4/S6 只做结构对标，S5 仍按实际背书事实决定。"
    )
    return normalize_comparison_contract(overridden)


def _apply_non_comparable_result(
    analysis: dict[str, Any],
    facts: dict[str, Any],
    contract: dict[str, Any],
    run_dir: Path,
) -> None:
    """无替代或身份不确定时在主对比前结束，不伪造 S1-S6 占位结论。"""
    uncertain = contract.get("overall_status") == "uncertain"
    reason = str(contract.get("reason") or "双侧产品缺少共同消费者任务，不能进行带货内容对比。")
    analysis["analysis_status"] = "comparison_uncertain" if uncertain else "not_comparable"
    analysis["one_line_verdict"] = "商品关系不确定，暂不分析" if uncertain else "两条视频不具备比较资格"
    analysis["one_line_summary"] = reason
    analysis["executive_summary"] = reason
    analysis["key_conclusions"] = [reason]
    analysis["comparison_contract"] = contract
    analysis["comparison_eligibility"] = contract
    analysis["video_understanding"] = facts
    analysis["stage_analysis"] = []
    analysis["improvements"] = []
    analysis["improvements_status"] = "not_applicable"
    analysis["analysis_run_state"] = "completed"
    analysis["analysis_source"] = {
        "type": "comparison_contract_gate",
        "merged_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    write_json(
        run_dir / "comparison_rejection.json",
        {"analysis_status": analysis["analysis_status"], "comparison_contract": contract, "reason": reason},
    )


def _stamp_proof_contract_source(foundation: dict[str, Any], analysis: dict[str, Any]) -> None:
    """给 Step-0 合同标注证据来源，限制下游复核器的覆盖权限。

    运营提供核心卖点只证明“产品卖什么”，不证明“哪一个卖点应成为唯一 S4 主视觉信号”。
    后者若仍由 Step-0 模型排序/生成，就必须标为 inferred，避免视觉复核器把模型选尺子
    误当运营裁决，再反向否决视频中已经观察到的其他有效视觉效果。

    默认标为 inferred。只有 --primary-selling-point 能唯一对应证明计划 candidate 时才标为
    operator；模型输出或普通 core_selling_points 文本不能自行抬升来源等级。
    """
    profile = foundation.get("product_profile")
    if not isinstance(profile, dict):
        return
    profile["proof_contract_source"] = "inferred"
    product = analysis.get("product") if isinstance(analysis.get("product"), dict) else {}
    operator_point = str(product.get("primary_selling_point") or "").strip()
    plan = profile.get("short_video_proof_plan") if isinstance(profile.get("short_video_proof_plan"), dict) else {}
    candidates = plan.get("candidates") if isinstance(plan.get("candidates"), list) else []
    if not operator_point or not plan.get("valid"):
        return
    normalized_operator = re.sub(r"[^\w\u4e00-\u9fff]+", "", operator_point.lower())
    matches = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        selling_point = str(candidate.get("selling_point") or "").strip()
        normalized_candidate = re.sub(r"[^\w\u4e00-\u9fff]+", "", selling_point.lower())
        if normalized_operator and (
            normalized_operator in normalized_candidate or normalized_candidate in normalized_operator
        ):
            matches.append(candidate)
    if len(matches) != 1:
        profile["proof_contract_validation_warning"] = "运营主卖点未能唯一对应 short_video_proof_plan candidate"
        return
    plan["primary_candidate_id"] = str(matches[0].get("id") or "")
    plan["selection_source"] = "operator_priority"
    plan["anchor_confidence"] = "high"
    profile["proof_contract_source"] = "operator"


def product_foundation_validation_reason(product_profile: Any) -> str:
    """新 Step-0 必须同时产出卖点分流计划与可校验合同；历史分析结果仍由 normalize 层兼容。"""
    if not isinstance(product_profile, dict):
        return "缺少 product_profile"
    plan = product_profile.get("short_video_proof_plan")
    if not isinstance(plan, dict) or plan.get("valid") is not True:
        return str(plan.get("validation_reason") or "缺少有效 short_video_proof_plan") if isinstance(plan, dict) else "缺少 short_video_proof_plan"
    contract = product_profile.get("proof_contract")
    if not isinstance(contract, dict) or contract.get("valid") is not True:
        return str(contract.get("validation_reason") or "证明合同不合法") if isinstance(contract, dict) else "缺少 proof_contract"
    return ""


def has_product_foundation_anchor(analysis: dict[str, Any]) -> bool:
    """判断 Step-0 是否有足够产品锚点；纯英文品牌/型号不算可靠锚点。"""
    product = analysis.get("product") if isinstance(analysis.get("product"), dict) else {}
    if isinstance(analysis.get("brand_proposition"), dict) and analysis["brand_proposition"]:
        return True
    for key in ("category", "core_selling_points", "target_user", "purchase_motivation", "notes"):
        value = str(product.get(key) or "").strip()
        if value and value not in {"未填写", "未提供", "无"}:
            return True
    name = str(product.get("name") or "").strip()
    if not name or name in {"未填写", "未提供"}:
        return False
    return any("\u4e00" <= ch <= "\u9fff" for ch in name)


def _unknown_stage_qualification_check(stage: str, reason: str) -> dict[str, Any]:
    """Create a fail-closed stage check without touching the evidence ledger."""
    return {
        "stage": stage,
        "status": "unknown",
        "coverage": "unknown",
        "evidence_ids": [],
        "invalid_evidence_ids": [],
        "observed_signals": [],
        "unqualified_observed_signals": [],
        "missing_signals": [],
        "invalid_observed_signals": [],
        "invalid_missing_signals": [],
        "signal_bindings": {},
        "invalid_signal_bindings": [],
        "observed_disqualifiers": [],
        "invalid_observed_disqualifiers": [],
        "evidence_strength": None,
        "reason": reason,
    }


def _validated_stage1_qualification_response(
    response: Any,
    *,
    targets: list[str],
    valid_ids: set[str],
    phase_label: str,
) -> dict[str, dict[str, Any]]:
    if not isinstance(response, dict):
        raise ValueError(f"{phase_label} qualification 必须返回 JSON object。")
    forbidden = stage1_forbidden_field_issues(response)
    pipeline_owned = stage1_pipeline_owned_field_issues(response)
    if forbidden:
        raise ValueError(
            f"{phase_label} qualification returned downstream fields: "
            + ", ".join(forbidden)
        )
    if pipeline_owned:
        raise ValueError(
            f"{phase_label} qualification returned pipeline-owned fields: "
            + ", ".join(pipeline_owned)
        )
    allowed_keys = {"stage_evidence_contract_version", "stage_evidence_checks"}
    extra_keys = sorted(set(response) - allowed_keys)
    if extra_keys:
        raise ValueError(
            f"{phase_label} qualification returned out-of-contract fields: "
            + ", ".join(extra_keys)
        )
    if response.get("stage_evidence_contract_version") != STAGE_EVIDENCE_CONTRACT_VERSION:
        raise ValueError(f"{phase_label} qualification 缺少匹配的 evidence contract version。")
    raw_checks = response.get("stage_evidence_checks")
    if not isinstance(raw_checks, list):
        raise ValueError(f"{phase_label} qualification 的 stage_evidence_checks 必须是数组。")
    raw_codes: list[str] = []
    required_check_keys = {
        "status",
        "coverage",
        "evidence_ids",
        "observed_signals",
        "missing_signals",
        "signal_bindings",
        "reason",
    }
    valid_statuses = {"present", "absent", "unknown", "conflict", "not_applicable"}
    valid_coverages = {"complete", "partial", "unknown"}
    for item in raw_checks:
        if not isinstance(item, dict):
            raise ValueError(f"{phase_label} qualification 的 stage check 必须是对象。")
        code = normalize_stage_code(item.get("stage"))
        if code is None:
            raise ValueError(f"{phase_label} qualification 返回了无效阶段。")
        missing_keys = sorted(required_check_keys - set(item))
        if missing_keys:
            raise ValueError(
                f"{phase_label} qualification 的 {code} 缺少必填语义字段："
                + ", ".join(missing_keys)
            )
        if str(item.get("status") or "").strip().lower() not in valid_statuses:
            raise ValueError(f"{phase_label} qualification 的 {code} status 非法。")
        if str(item.get("coverage") or "").strip().lower() not in valid_coverages:
            raise ValueError(f"{phase_label} qualification 的 {code} coverage 非法。")
        for key in ("evidence_ids", "observed_signals", "missing_signals"):
            if not isinstance(item.get(key), list):
                raise ValueError(f"{phase_label} qualification 的 {code} {key} 必须是数组。")
        if not isinstance(item.get("signal_bindings"), dict):
            raise ValueError(f"{phase_label} qualification 的 {code} signal_bindings 必须是对象。")
        contract = stage_evidence_contract(code)
        if contract is None:  # pragma: no cover - normalize_stage_code already guards this
            raise ValueError(f"{phase_label} qualification 的 {code} 缺少阶段合同。")
        for key in ("observed_signals", "missing_signals"):
            raw_signals = item[key]
            if any(not isinstance(value, str) or not value.strip() for value in raw_signals):
                raise ValueError(f"{phase_label} qualification 的 {code} {key} 含无效值。")
            invalid_signals = sorted(set(raw_signals) - set(contract.allowed_signals))
            if invalid_signals:
                raise ValueError(
                    f"{phase_label} qualification 的 {code} {key} 含非合同信号："
                    + ", ".join(invalid_signals)
                )
        if any(not isinstance(value, str) or not value.strip() for value in item["evidence_ids"]):
            raise ValueError(
                f"{phase_label} qualification 的 {code} evidence_ids 只能包含非空字符串。"
            )
        invalid_ids = sorted(set(item["evidence_ids"]) - valid_ids)
        if invalid_ids:
            raise ValueError(
                f"{phase_label} qualification 的 {code} evidence_ids 含无效引用："
                + ", ".join(invalid_ids)
            )
        for signal, binding in item["signal_bindings"].items():
            if signal not in contract.allowed_signals or not isinstance(binding, dict):
                raise ValueError(f"{phase_label} qualification 的 {code} signal binding 非法：{signal}")
            binding_keys = {"status", "evidence_ids", "reason"}
            if not binding_keys.issubset(binding):
                raise ValueError(f"{phase_label} qualification 的 {code} binding 缺字段：{signal}")
            if str(binding.get("status") or "").strip().lower() not in {
                "supported", "missing", "unknown", "conflict"
            }:
                raise ValueError(f"{phase_label} qualification 的 {code} binding status 非法：{signal}")
            binding_ids = binding.get("evidence_ids")
            if not isinstance(binding_ids, list) or any(
                not isinstance(value, str) or not value.strip() for value in binding_ids
            ):
                raise ValueError(f"{phase_label} qualification 的 {code} binding IDs 非法：{signal}")
            invalid_binding_ids = sorted(set(binding_ids) - valid_ids)
            if invalid_binding_ids:
                raise ValueError(
                    f"{phase_label} qualification 的 {code} binding 含无效引用："
                    + ", ".join(invalid_binding_ids)
                )
            if not isinstance(binding.get("reason"), str) or not binding["reason"].strip():
                raise ValueError(f"{phase_label} qualification 的 {code} binding reason 不能为空：{signal}")
        if not isinstance(item.get("reason"), str) or not item["reason"].strip():
            raise ValueError(f"{phase_label} qualification 的 {code} reason 不能为空。")
        raw_codes.append(code)
    target_set = set(targets)
    returned = set(raw_codes)
    if len(raw_codes) != len(targets) or returned != target_set:
        missing = sorted(target_set - returned)
        extra = sorted(returned - target_set)
        duplicate = sorted(code for code in returned if raw_codes.count(code) > 1)
        detail = []
        if missing:
            detail.append(f"missing={','.join(missing)}")
        if extra:
            detail.append(f"extra={','.join(extra)}")
        if duplicate:
            detail.append(f"duplicate={','.join(duplicate)}")
        raise ValueError(
            f"{phase_label} qualification 必须恰好覆盖 {','.join(targets)}。"
            + (" " + " ".join(detail) if detail else "")
        )
    checks = normalize_stage_evidence_checks(raw_checks, valid_ids)
    return {
        str(item.get("stage")): item
        for item in checks
        if isinstance(item, dict) and str(item.get("stage") or "").strip()
    }


def _run_stage1_qualification(
    args: argparse.Namespace,
    analysis: dict[str, Any],
    run_dir: Path,
    api_key: str,
    role: str,
    facts: dict[str, Any],
    *,
    target_stages: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Run bounded Stage1-B/D projections over the locked atomic facts.

    Qualification is split into the same four semantic groups as Stage2. A
    response failure or cross-stage binding error therefore blocks only the
    affected group; successful groups remain available for downstream
    judgment and the single Stage1-C pass can target only the failed stages.
    A focused call is recorded as phase D so it cannot overwrite the original
    phase-B artifact that explains why recovery was needed.
    """
    requested_stages = {
        code
        for value in (target_stages or stage_codes())
        if (code := normalize_stage_code(value)) is not None
    }
    groups_to_run = [
        [stage for stage in group if stage in requested_stages]
        for group in STAGE1_QUALIFICATION_GROUPS
        if any(stage in requested_stages for stage in group)
    ]
    focused_requalification = target_stages is not None
    provider_phase = "D" if focused_requalification else "B"
    phase_label = f"Stage1-{provider_phase}"
    valid_ids = {
        str(item.get("id") or "").strip()
        for item in facts.get("evidence_units") or []
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    if args.llm_dry_run:
        facts["stage1_qualification"] = {
            "source": "pipeline",
            "status": "deferred_dry_run",
            "stage_codes": list(stage_codes()),
        }
        _block_stage_qualifications(
            facts,
            sorted(requested_stages, key=list(stage_codes()).index),
            reason="Stage1-B 在 dry-run 中未执行，阶段资格保持未知。",
        )
        return facts
    existing_checks = normalize_stage_evidence_checks(
        facts.get("stage_evidence_checks"),
        valid_ids,
    )
    checks_by_stage = {
        str(item.get("stage")): item
        for item in existing_checks
        if isinstance(item, dict) and str(item.get("stage") or "").strip()
    }
    existing_qualification = (
        facts.get("stage1_qualification")
        if isinstance(facts.get("stage1_qualification"), dict)
        else {}
    )
    group_records: list[dict[str, Any]] = [
        copy.deepcopy(item)
        for item in existing_qualification.get("group_records") or []
        if isinstance(item, dict)
    ]
    prior_failed_stage_codes = {
        code
        for value in existing_qualification.get("failed_stage_codes") or []
        if (code := normalize_stage_code(value)) is not None
    }
    if (
        focused_requalification
        and existing_qualification.get("status") == "failed"
        and not prior_failed_stage_codes
    ):
        # Legacy failed qualification records did not identify their failed
        # groups. The bounded D targets are the only safe lineage we can infer.
        prior_failed_stage_codes = set(requested_stages)
    failed_stage_codes: list[str] = [
        code
        for code in prior_failed_stage_codes
        if code not in requested_stages
    ] if focused_requalification else []
    current_successful_group_count = 0
    replay_source, provider_fallback_allowed = _stage1_replay_source(args)

    for targets in groups_to_run:
        label = "_".join(targets)
        request_kind = "requalification" if focused_requalification else "qualification"
        request_path = run_dir / f"llm_facts_{role}_{request_kind}_{label}_request.json"
        response_path = run_dir / f"llm_facts_{role}_{request_kind}_{label}_response.json"
        artifact_path = stage_fact_artifact_path(run_dir, role, provider_phase, targets)
        payload: dict[str, Any] = {}
        response_meta: dict[str, Any] = {}
        execution_source = "provider"
        response: dict[str, Any] | None = None
        replay_artifact_path = (
            stage_fact_artifact_path(replay_source, role, provider_phase, targets)
            if replay_source is not None
            else None
        )
        preserve_resume_source = (
            provider_fallback_allowed
            and _same_existing_artifact(replay_artifact_path, artifact_path)
        )
        resume_failure_path = _resume_failure_artifact_path(artifact_path)
        try:
            payload = build_stage_evidence_qualification_payload(
                judgment_model(args),
                role,
                analysis,
                facts,
                targets,
            )
            if replay_source is not None:
                execution_source = "replay"
                try:
                    response, response_meta, _source_artifact = _read_replayable_stage_fact(
                        replay_source,
                        role=role,
                        phase=provider_phase,
                        group=targets,
                        payload=payload,
                        args=args,
                    )
                    _validated_stage1_qualification_response(
                        response,
                        targets=targets,
                        valid_ids=valid_ids,
                        phase_label=phase_label,
                    )
                except (StageFactArtifactError, ValueError) as exc:
                    if not provider_fallback_allowed:
                        if isinstance(exc, StageFactArtifactError):
                            raise
                        raise StageFactArtifactError(
                            f"{phase_label} replay content contract invalid: {exc}"
                        ) from exc
                    execution_source = "provider"
                    response = None
                    response_meta = {}
            if response is None:
                write_json(request_path, payload)
                response_text = fetch_json_completion(
                    args,
                    api_key,
                    request_path,
                    response_path,
                    response_meta=response_meta,
                )
                response = parse_json_text(response_text)
            normalized_by_stage = _validated_stage1_qualification_response(
                response,
                targets=targets,
                valid_ids=valid_ids,
                phase_label=phase_label,
            )
            for stage in targets:
                checks_by_stage[stage] = normalized_by_stage.get(
                    stage,
                    _unknown_stage_qualification_check(
                        stage,
                        f"{phase_label} 响应缺少目标阶段，资格保持未知。",
                    ),
                )
            artifact = completed_stage_fact_artifact(
                role=role,
                phase=provider_phase,
                group=targets,
                payload=payload,
                response=response,
                model=judgment_model(args),
                api_url=args.llm_api_url,
                response_meta=response_meta,
                artifact_name=artifact_path.name,
            )
            write_json(artifact_path, artifact)
            resume_failure_path.unlink(missing_ok=True)
            current_successful_group_count += 1
            group_records.append(
                {
                    "phase": provider_phase,
                    "group": targets,
                    "status": "completed",
                    "execution_source": execution_source,
                    "provider_artifact": artifact_path.name,
                    "request_identity_sha256": artifact["request_identity"]["sha256"],
                    "response_sha256": artifact["response_sha256"],
                    "completion_attempts": response_meta.get("completion_attempts", 0),
                }
            )
        except (OSError, ValueError, RuntimeError, SystemExit) as exc:
            if _is_strict_replay_failure(args, exc):
                raise
            safe_error = str(exc).strip()
            if api_key and safe_error:
                safe_error = safe_error.replace(api_key, "[REDACTED]")
            failed_stage_codes.extend(targets)
            for stage in targets:
                checks_by_stage[stage] = _unknown_stage_qualification_check(
                    stage,
                    f"{phase_label} 该阶段组失败，资格保持未知；Stage1-A/C 原子观察仍保留。",
                )
            failure_record = {
                "phase": provider_phase,
                "group": targets,
                "status": "failed",
                "execution_source": execution_source,
                "failure_kind": _localized_failure_kind(
                    exc,
                    execution_source=execution_source,
                    default="provider_call_or_validation",
                ),
                "cause_type": exc.__class__.__name__,
                "failure_reason": safe_error[:500] or type(exc).__name__,
                "completion_attempts": response_meta.get("completion_attempts", 0),
            }
            if payload:
                failure_artifact = failed_stage_fact_artifact(
                    role=role,
                    phase=provider_phase,
                    group=targets,
                    payload=payload,
                    model=judgment_model(args),
                    api_url=args.llm_api_url,
                    error=safe_error or type(exc).__name__,
                    artifact_name=artifact_path.name,
                    response_meta=response_meta,
                    response=response,
                )
                failure_path = resume_failure_path if preserve_resume_source else artifact_path
                write_json(failure_path, failure_artifact)
                failure_record.update(
                    {
                        "provider_artifact": failure_path.name,
                        "request_identity_sha256": failure_artifact["request_identity"]["sha256"],
                        "response_sha256": failure_artifact.get("response_sha256", ""),
                    }
                )
            group_records.append(failure_record)

    facts["stage_evidence_checks"] = normalize_stage_evidence_checks(
        list(checks_by_stage.values()),
        valid_ids,
    )
    facts["stage_evidence_contract_version"] = STAGE_EVIDENCE_CONTRACT_VERSION
    failed_stage_codes = list(dict.fromkeys(failed_stage_codes))
    qualification = {
        "source": "pipeline",
        "status": (
            "failed"
            if focused_requalification and failed_stage_codes
            else "completed"
            if current_successful_group_count
            else "failed"
        ),
        "contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
        "stage_codes": list(stage_codes()),
        "evidence_id_count": len(valid_ids),
        "group_records": group_records,
        "failed_stage_codes": failed_stage_codes,
    }
    if focused_requalification:
        qualification["requalification_source_failed_stage_codes"] = sorted(
            prior_failed_stage_codes.intersection(requested_stages),
            key=list(stage_codes()).index,
        )
    if failed_stage_codes:
        qualification["failure_reason"] = f"一个或多个 {phase_label} 阶段组失败；仅对应阶段保持未知。"
    facts["stage1_qualification"] = qualification
    return facts


def run_video_fact_extraction(
    args: argparse.Namespace,
    analysis: dict[str, Any],
    run_dir: Path,
    api_key: str,
) -> dict[str, Any]:
    """对每个 role 跑一次单视频事实抽取，写出 video_facts_{role}.json 并返回 dict。"""
    facts: dict[str, Any] = {}
    videos = analysis.get("videos", {})
    # Benchmark and creator facts are extracted in separate requests. Dividing
    # the image budget by the number of videos would halve each request's
    # temporal coverage even though no request ever contains both sides.
    per_role_limit = max(4, args.llm_image_limit)
    stage1_replay_source, _provider_fallback_allowed = _stage1_replay_source(args)
    for role in ("benchmark", "creator"):
        if role not in videos:
            continue
        role_dir = run_dir / role
        result_path = run_dir / f"video_facts_{role}.json"
        cache_key = _video_fact_cache_key(args, analysis, role)
        cache_path = _cache_path(run_dir, ".video_fact_cache", cache_key)
        cache_record = (
            None
            if args.llm_dry_run or stage1_replay_source is not None
            else _read_cache_record(
                cache_path,
                "fact_result",
                cache_key,
                validator=lambda value: _is_valid_video_fact_cache(role, value, analysis),
            )
        )
        cached = (
            cache_record.get("fact_result")
            if isinstance(cache_record, dict)
            and _restore_stage_fact_artifacts_from_cache(cache_record, run_dir, role)
            else None
        )
        if cached is not None:
            cached.setdefault("temporal_evidence_mode", "unknown")
            # An active cache is already frozen. Re-running this sanitizer here
            # could mutate immutable audio facts after the cached digest was
            # validated; capability changes must invalidate the cache key/code
            # version instead of rewriting a locked Stage1 record in place.
            if cached.get("stage_evidence_contract_version") != STAGE_EVIDENCE_CONTRACT_VERSION:
                sanitize_audio_observations(
                    {"video_understanding": {role: cached}, "stage_analysis": []},
                    can_analyze_native_audio(args.llm_api_url, vision_model(args)),
                )
            # A valid current-contract cache already contains the frozen
            # Stage1-B result. Re-running qualification here would spend four
            # LLM calls and make a supposedly reusable fact set nondeterministic.
            # Legacy cache records are still migrated once so they can acquire
            # the current qualification metadata before being frozen again.
            if (
                cached.get("stage_evidence_contract_version") != STAGE_EVIDENCE_CONTRACT_VERSION
                or not isinstance(cached.get("stage1_qualification"), dict)
                or cached.get("stage1_qualification", {}).get("status") != "completed"
            ):
                cached = _run_stage1_qualification(args, analysis, run_dir, api_key, role, cached)
            cached = _maybe_recover_video_facts(
                args,
                analysis,
                run_dir,
                api_key,
                role,
                cached,
            )
            acquisition = cached.get("stage1_acquisition")
            if isinstance(acquisition, dict):
                acquisition["provider_artifacts"] = _current_stage1_provider_artifacts(cached)
            if cached.get("stage_evidence_contract_version") == STAGE_EVIDENCE_CONTRACT_VERSION:
                freeze_stage_evidence(cached)
            facts[role] = cached
            write_json(result_path, cached)
            continue
        visual_inputs = select_role_visual_inputs(videos[role], role, per_role_limit)
        payload = build_video_fact_payload(
            vision_model(args),
            role,
            analysis,
            visual_inputs,
            api_url=args.llm_api_url,
            budget=getattr(args, "_resource_budget", None),
        )
        request_path = run_dir / f"llm_facts_{role}_request.json"
        response_path = run_dir / f"llm_facts_{role}_response.json"
        if args.llm_dry_run:
            continue
        response_meta: dict[str, Any] = {}
        artifact_path = stage_fact_artifact_path(run_dir, role, "A")
        execution_source = "provider"
        parsed_response: dict[str, Any] | None = None
        fact_result: dict[str, Any] | None = None
        replay_artifact_path = (
            stage_fact_artifact_path(stage1_replay_source, role, "A")
            if stage1_replay_source is not None
            else None
        )
        preserve_resume_source = (
            _provider_fallback_allowed
            and _same_existing_artifact(replay_artifact_path, artifact_path)
        )
        resume_failure_path = _resume_failure_artifact_path(artifact_path)
        try:
            if stage1_replay_source is not None:
                try:
                    parsed_response, response_meta, _source_artifact = _read_replayable_stage_fact(
                        stage1_replay_source,
                        role=role,
                        phase="A",
                        group=None,
                        payload=payload,
                        args=args,
                    )
                    execution_source = "replay"
                    fact_result = normalize_video_fact_result(
                        role,
                        parsed_response,
                        analysis,
                    )
                except (StageFactArtifactError, ValueError, SystemExit) as exc:
                    # Strict replay is a reproducibility operation and must
                    # fail closed. Resume is local recovery: a missing or
                    # stale Stage1-A artifact may be regenerated by the
                    # provider, while still preserving the failure in the new
                    # run's artifact ledger if that call also fails.
                    if not _provider_fallback_allowed:
                        if isinstance(exc, StageFactArtifactError):
                            raise
                        raise StageFactArtifactError(
                            f"Stage1-A replay content contract invalid: {exc}"
                        ) from exc
                    parsed_response = None
                    fact_result = None
                    response_meta = {}
                    execution_source = "provider"
            if parsed_response is None:
                write_json(request_path, payload)
                result_text = fetch_json_completion(
                    args,
                    api_key,
                    request_path,
                    response_path,
                    request_max_time_seconds=STAGE1_A_REQUEST_TIMEOUT_SECONDS,
                    request_retries=STAGE1_A_REQUEST_RETRIES,
                    response_meta=response_meta,
                )
                parsed_response = parse_json_text(result_text)
            if not isinstance(parsed_response, dict):
                raise ValueError("Stage1-A provider response must be a JSON object")
            if fact_result is None:
                fact_result = normalize_video_fact_result(role, parsed_response, analysis)
            artifact = completed_stage_fact_artifact(
                role=role,
                phase="A",
                payload=payload,
                response=parsed_response,
                model=vision_model(args),
                api_url=args.llm_api_url,
                response_meta=response_meta,
                artifact_name=artifact_path.name,
            )
            write_json(artifact_path, artifact)
            resume_failure_path.unlink(missing_ok=True)
        except (OSError, ValueError, RuntimeError, SystemExit) as exc:
            safe_error = str(exc).replace(api_key, "[REDACTED]")[:1000]
            failure_artifact = failed_stage_fact_artifact(
                    role=role,
                    phase="A",
                    payload=payload,
                    model=vision_model(args),
                    api_url=args.llm_api_url,
                    error=safe_error or type(exc).__name__,
                    artifact_name=artifact_path.name,
                    response_meta=response_meta,
                    response=parsed_response,
                )
            failure_path = resume_failure_path if preserve_resume_source else artifact_path
            write_json(failure_path, failure_artifact)
            raise
        if fact_result is None:  # pragma: no cover - guarded by validation above
            raise RuntimeError("Stage1-A normalized result is unavailable")
        fact_result["evidence_budget_exceeded"] = response_meta.get("finish_reason") == "length"
        stage1_a_direct_audio = payload_has_direct_audio(
            payload,
            api_url=args.llm_api_url,
            model=vision_model(args),
        )
        sanitize_audio_observations(
            {"video_understanding": {role: fact_result}, "stage_analysis": []},
            stage1_a_direct_audio,
        )
        # Stage1-A is deliberately static even when the provider could accept
        # a full video; focused temporal capability is earned only by Stage1-C.
        fact_result["temporal_evidence_mode"] = "static_only"
        fact_result["stage1_acquisition"] = build_stage1_acquisition_manifest(
            analysis,
            role,
            native_video=payload_has_video(payload),
            visual_input_count=len(visual_inputs),
            visual_input_timestamps=_visual_input_timestamps(visual_inputs),
            audio_input_available=stage1_a_direct_audio,
        )
        fact_result["stage1_acquisition"]["provider_artifacts"] = [{
            "phase": "A",
            "artifact": artifact_path.name,
            "status": "completed",
            "execution_source": execution_source,
            "request_identity_sha256": artifact["request_identity"]["sha256"],
            "response_sha256": artifact["response_sha256"],
            "completion_attempts": response_meta.get("completion_attempts", 0),
        }]
        fact_result = _run_stage1_qualification(
            args,
            analysis,
            run_dir,
            api_key,
            role,
            fact_result,
        )
        fact_result = _maybe_recover_video_facts(
            args,
            analysis,
            run_dir,
            api_key,
            role,
            fact_result,
        )
        recovery_meta = fact_result.get("stage1_recovery") if isinstance(fact_result.get("stage1_recovery"), dict) else {}
        recovery_media_mode = str(recovery_meta.get("media_mode") or "")
        recovery_direct_audio = (
            recovery_media_mode in {"focused_native_video", "focused_audio"}
            and can_analyze_native_audio(args.llm_api_url, vision_model(args))
        )
        sanitize_audio_observations(
            {"video_understanding": {role: fact_result}, "stage_analysis": []},
            stage1_a_direct_audio or recovery_direct_audio,
        )
        if recovery_media_mode == "focused_native_video":
            fact_result["temporal_evidence_mode"] = "focused_temporal"
        existing_provider_artifacts = [{
            "phase": "A",
            "artifact": artifact_path.name,
            "status": "completed",
            "execution_source": execution_source,
            "request_identity_sha256": artifact["request_identity"]["sha256"],
            "response_sha256": artifact["response_sha256"],
            "completion_attempts": response_meta.get("completion_attempts", 0),
        }]
        qualification_records = [
            item
            for item in (
                fact_result.get("stage1_qualification", {}).get("group_records", [])
                if isinstance(fact_result.get("stage1_qualification"), dict)
                else []
            )
            if isinstance(item, dict) and item.get("provider_artifact")
        ]
        for item in qualification_records:
            if str(item.get("phase") or "B").strip().upper() != "B":
                continue
            existing_provider_artifacts.append({
                "phase": "B",
                "artifact": item.get("provider_artifact"),
                "status": item.get("status", "completed"),
                "execution_source": item.get("execution_source", "provider"),
                "request_identity_sha256": item.get("request_identity_sha256", ""),
                "response_sha256": item.get("response_sha256", ""),
                "completion_attempts": item.get("completion_attempts", 0),
                "failure_kind": item.get("failure_kind", ""),
                "cause_type": item.get("cause_type", ""),
                "failure_reason": item.get("failure_reason", ""),
            })
        recovery_meta = fact_result.get("stage1_recovery")
        if isinstance(recovery_meta, dict) and recovery_meta.get("provider_artifact"):
            existing_provider_artifacts.append({
                "phase": "C",
                "artifact": recovery_meta.get("provider_artifact"),
                "status": recovery_meta.get("provider_status", "completed"),
                "execution_source": recovery_meta.get("execution_source", "provider"),
                "request_identity_sha256": recovery_meta.get("request_identity_sha256", ""),
                "response_sha256": recovery_meta.get("response_sha256", ""),
                "completion_attempts": recovery_meta.get("completion_attempts", 0),
                "failure_reason": recovery_meta.get("failure_reason", ""),
            })
        for item in qualification_records:
            if str(item.get("phase") or "B").strip().upper() != "D":
                continue
            existing_provider_artifacts.append({
                "phase": "D",
                "artifact": item.get("provider_artifact"),
                "status": item.get("status", "completed"),
                "execution_source": item.get("execution_source", "provider"),
                "request_identity_sha256": item.get("request_identity_sha256", ""),
                "response_sha256": item.get("response_sha256", ""),
                "completion_attempts": item.get("completion_attempts", 0),
                "failure_kind": item.get("failure_kind", ""),
                "cause_type": item.get("cause_type", ""),
                "failure_reason": item.get("failure_reason", ""),
            })
        fact_result["stage1_acquisition"]["provider_artifacts"] = existing_provider_artifacts
        if fact_result.get("stage_evidence_contract_version") == STAGE_EVIDENCE_CONTRACT_VERSION:
            freeze_stage_evidence(fact_result)
        facts[role] = fact_result
        write_json(result_path, fact_result)
        _write_cache_result(
            cache_path,
            {
                **cache_key,
                "fact_result": fact_result,
                "stage_fact_artifacts": _stage_fact_artifacts_for_cache(run_dir, role, fact_result),
            },
        )
    return facts


def _merge_video_fact_coverage_audit(
    role: str,
    base: dict[str, Any],
    audit: dict[str, Any],
    analysis: dict[str, Any],
    target_stages: list[str] | None = None,
) -> dict[str, Any]:
    """Append independent audit candidates and reconcile stage projections.

    The primary extraction remains authoritative for facts it already found.
    Audit candidates are append-only.  A primary ``absent`` can become
    ``present`` only when the independent pass supplies all required signals
    with a complete scope; a disagreement that cannot satisfy the contract
    remains ``conflict`` and blocks Stage2.
    """
    candidate_response = {
        "candidate_evidence_units": audit.get("candidate_evidence_units") or [],
        "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
    }
    merged = _merge_video_fact_recovery(
        role,
        base,
        candidate_response,
        analysis,
        [],
    )
    valid_ids = {
        str(item.get("id") or "").strip()
        for item in merged.get("evidence_units") or []
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    raw_stage_map = audit.get("stages") if isinstance(audit.get("stages"), dict) else {}
    valid_stage_codes = set(stage_codes())
    requested_stages = list(dict.fromkeys(
        code
        for code in (
            normalize_stage_code(item)
            for item in (target_stages if target_stages is not None else raw_stage_map.keys())
        )
        if code in valid_stage_codes
    ))
    requested_stage_set = set(requested_stages)
    normalized_audit = normalize_stage1_coverage_audit(
        {
            **copy.deepcopy(audit),
            "source": "pipeline",
            "target_stages": requested_stages,
            # The request boundary is code-owned.  Do not trust a model echo
            # to prove that this was a separate request.
            "independence": STAGE1_COVERAGE_AUDIT_INDEPENDENCE,
        },
        valid_ids,
    )
    primary_checks = stage_evidence_check_map(base)
    audit_stages = normalized_audit.get("stages") if isinstance(normalized_audit.get("stages"), dict) else {}
    merged_checks: list[dict[str, Any]] = []
    audit_stages_out: dict[str, dict[str, Any]] = {}

    for code in stage_codes():
        contract = stage_evidence_contract(code)
        primary = copy.deepcopy(primary_checks.get(code) or {
            "stage": code,
            "status": "unknown",
            "coverage": "unknown",
            "evidence_ids": [],
            "observed_signals": [],
            "missing_signals": [],
            "observed_disqualifiers": [],
            "reason": "primary extraction omitted this stage",
        })
        audit_item = audit_stages.get(code) if isinstance(audit_stages.get(code), dict) else {}
        primary_status = str(primary.get("status") or "unknown").strip().lower()
        audit_status = str(audit_item.get("status") or "unknown").strip().lower()
        primary_ids = [str(value).strip() for value in primary.get("evidence_ids") or [] if str(value).strip()]
        audit_ids = [
            str(value).strip()
            for value in audit_item.get("evidence_ids") or []
            if str(value).strip() in valid_ids
        ]
        merged_signal_bindings = merge_stage_signal_bindings(
            primary.get("signal_bindings"),
            audit_item.get("signal_bindings"),
        )
        observed = list(dict.fromkeys(
            [str(value).strip() for value in primary.get("observed_signals") or [] if str(value).strip()]
            + [str(value).strip() for value in audit_item.get("observed_signals") or [] if str(value).strip()]
        ))
        missing = [
            str(value).strip()
            for value in primary.get("missing_signals") or []
            if str(value).strip() not in observed
        ]
        coverage = str(primary.get("coverage") or "unknown").strip().lower()
        stage_was_requested = code in requested_stage_set
        if stage_was_requested and audit_status == "found" and primary_status == "present":
            # The primary pass already owns the atomic fact and its signal
            # bindings.  A separate complete scan may close only the coverage
            # dimension; it must not replace or rewrite the primary evidence.
            audit_coverage = str(audit_item.get("coverage") or "unknown").strip().lower()
            if audit_coverage == "complete":
                coverage = "complete"
        elif stage_was_requested and audit_status == "found" and primary_status in {"absent", "unknown"}:
            coverage = str(audit_item.get("coverage") or "unknown").strip().lower()
            if (
                contract is not None
                and coverage == "complete"
                and required_stage_signals_satisfied(contract, observed)
            ):
                primary_status = "present"
                missing = []
            elif primary_status == "absent":
                primary_status = "conflict"
        elif stage_was_requested and audit_status == "clear" and primary_status == "unknown":
            coverage = str(audit_item.get("coverage") or "unknown").strip().lower()
            if contract is not None and coverage == "complete":
                primary_status = "absent"
                missing = list(dict.fromkeys(
                    missing + list(contract.required_signals)
                ))
        elif stage_was_requested and audit_status in {"unknown", "conflict"} and primary_status in {"present", "absent"}:
            primary_status = "conflict"

        primary["stage"] = code
        primary["status"] = primary_status
        primary["coverage"] = coverage
        primary["evidence_ids"] = list(dict.fromkeys(primary_ids + audit_ids))
        primary["observed_signals"] = observed
        primary["missing_signals"] = [value for value in dict.fromkeys(missing) if value not in observed]
        primary["signal_bindings"] = merged_signal_bindings
        primary["invalid_signal_bindings"] = list(dict.fromkeys(
            [
                *[str(value).strip() for value in primary.get("invalid_signal_bindings") or [] if str(value).strip()],
                *[str(value).strip() for value in audit_item.get("invalid_signal_bindings") or [] if str(value).strip()],
            ]
        ))
        if stage_was_requested and audit_status in {"unknown", "conflict"}:
            primary["reason"] = (
                str(primary.get("reason") or "").strip()
                + "；独立覆盖审计未能闭合。"
            ).strip("；")
        merged_checks.append(primary)

        effective_audit_status = audit_status
        if stage_was_requested and primary_status == "present" and audit_status == "clear":
            effective_audit_status = "conflict"
        audit_stages_out[code] = {
            "status": effective_audit_status,
            "coverage": str(audit_item.get("coverage") or "unknown").strip().lower(),
            "evidence_ids": audit_ids,
            "invalid_evidence_ids": list(audit_item.get("invalid_evidence_ids") or []),
            "observed_signals": list(audit_item.get("observed_signals") or []),
            "missing_signals": list(audit_item.get("missing_signals") or []),
            "signal_bindings": copy.deepcopy(audit_item.get("signal_bindings") or {}),
            "invalid_signal_bindings": list(audit_item.get("invalid_signal_bindings") or []),
            "primary_status": str(primary_checks.get(code, {}).get("status") or "unknown"),
            "primary_evidence_ids": primary_ids,
            "reason": str(audit_item.get("reason") or "").strip(),
        }

    merged["stage_evidence_checks"] = merged_checks
    normalized = normalize_video_fact_result(
        role,
        merged,
        analysis,
        allow_trusted_pipeline_metadata=True,
    )
    if isinstance(base.get("stage1_acquisition"), dict):
        normalized["stage1_acquisition"] = copy.deepcopy(base["stage1_acquisition"])
    if isinstance(base.get("stage1_qualification"), dict):
        normalized["stage1_qualification"] = copy.deepcopy(base["stage1_qualification"])
    normalized["stage1_coverage_audit"] = normalize_stage1_coverage_audit(
        {
            "version": STAGE1_COVERAGE_AUDIT_VERSION,
            "source": "pipeline",
            "status": normalized_audit.get("status") or "unknown",
            "independence": STAGE1_COVERAGE_AUDIT_INDEPENDENCE,
            "target_stages": requested_stages,
            "stages": audit_stages_out,
            "errors": normalized_audit.get("errors") or [],
        },
        {
            str(item.get("id") or "").strip()
            for item in normalized.get("evidence_units") or []
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        },
    )
    return normalized


def _block_stage_qualifications(
    facts: dict[str, Any],
    target_stages: list[str],
    *,
    reason: str,
) -> dict[str, Any]:
    """Fail closed only the affected stage projections, not their atomic facts."""
    valid_ids = {
        str(item.get("id") or "").strip()
        for item in facts.get("evidence_units") or []
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    target_set = set(target_stages)
    blocked_checks: list[dict[str, Any]] = []
    for stage in stage_codes():
        current = copy.deepcopy(stage_evidence_check_map(facts).get(stage) or {"stage": stage})
        if stage in target_set:
            current.update(
                {
                    "status": "unknown",
                    "coverage": "unknown",
                    "evidence_ids": [],
                    "invalid_evidence_ids": [],
                    "observed_signals": [],
                    "missing_signals": [],
                    "invalid_observed_signals": [],
                    "invalid_missing_signals": [],
                    "signal_bindings": {},
                    "invalid_signal_bindings": [],
                    "observed_disqualifiers": [],
                    "invalid_observed_disqualifiers": [],
                    "evidence_strength": None,
                    "reason": reason,
                }
            )
        blocked_checks.append(current)
    facts["stage_evidence_checks"] = normalize_stage_evidence_checks(blocked_checks, valid_ids)
    return facts


def _materialize_stage_recovery_audit(
    facts: dict[str, Any],
    target_stages: list[str],
    *,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    """Project one focused Stage1-C result into the legacy audit field.

    ``stage1_coverage_audit`` is retained as an artifact name so old readers
    can explain why a stage is blocked.  The active producer is deterministic:
    it reads the post-recovery stage checks and never invents a second semantic
    opinion.  This keeps the runtime gate useful while removing the old
    same-media, full-video audit request from production.
    """
    valid_ids = {
        str(item.get("id") or "").strip()
        for item in facts.get("evidence_units") or []
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    requested = list(dict.fromkeys(
        code
        for code in (normalize_stage_code(value) for value in target_stages)
        if code in set(stage_codes())
    ))
    checks = stage_evidence_check_map(facts)
    # This function is the sole producer of the post-recovery coverage audit.
    # The previous audit describes the pre-recovery state and must not take
    # part in validating its replacement; otherwise a stale ``unknown`` audit
    # can veto newly qualified stage checks and make recovery self-blocking.
    facts_without_previous_audit = dict(facts)
    facts_without_previous_audit.pop("stage1_coverage_audit", None)
    structural_issues = stage_evidence_contract_issues(
        facts_without_previous_audit,
        require_version=True,
    )
    global_issues = [
        issue
        for issue in structural_issues
        if issue.split(":", 1)[0] not in set(stage_codes())
    ]
    stages: dict[str, dict[str, Any]] = {}
    unresolved: list[str] = []
    for code in requested:
        check = checks.get(code) if isinstance(checks.get(code), dict) else {}
        stage_issues = [
            issue for issue in structural_issues
            if issue.split(":", 1)[0] == code
        ]
        stage_issues.extend(stage1_acquisition_issues(facts, code))
        status = str(check.get("status") or "unknown").strip().lower()
        if global_issues or stage_issues or status not in {"present", "absent", "not_applicable"}:
            audit_status = "conflict" if status == "conflict" else "unknown"
            coverage = "unknown"
            unresolved.append(code)
        else:
            # The legacy audit field has no not_applicable enum. Preserve the
            # closed primary state while using its historical non-positive
            # projection; stage_evidence_readiness remains authoritative.
            audit_status = "found" if status == "present" else "clear"
            coverage = "complete"
        stage_ids = [
            str(value).strip()
            for value in check.get("evidence_ids") or []
            if str(value).strip() in valid_ids
        ]
        stages[code] = {
            "status": audit_status,
            "coverage": coverage,
            "evidence_ids": list(dict.fromkeys(stage_ids)),
            "observed_signals": list(dict.fromkeys(
                str(value).strip()
                for value in check.get("observed_signals") or []
                if str(value).strip()
            )),
            "missing_signals": list(dict.fromkeys(
                str(value).strip()
                for value in check.get("missing_signals") or []
                if str(value).strip()
            )),
            "signal_bindings": copy.deepcopy(check.get("signal_bindings") or {}),
            "invalid_signal_bindings": list(check.get("invalid_signal_bindings") or []),
            "reason": str(check.get("reason") or "").strip(),
        }
    audit_errors = list(dict.fromkeys([
        *(str(item).strip() for item in (errors or []) if str(item).strip()),
        *(f"{code}:focused_recovery_unresolved" for code in unresolved),
        *global_issues,
    ]))
    audit_status = "completed" if requested and not unresolved and not global_issues else "partial"
    return normalize_stage1_coverage_audit(
        {
            "version": STAGE1_COVERAGE_AUDIT_VERSION,
            "source": "pipeline",
            "status": audit_status,
            "independence": STAGE1_COVERAGE_AUDIT_INDEPENDENCE,
            "target_stages": requested,
            "stages": stages,
            "errors": audit_errors,
        },
        valid_ids,
    )


def _mark_video_fact_coverage_audit_failed(
    facts: dict[str, Any],
    *,
    target_stages: list[str],
    trigger_reasons: list[str],
    contract_issues: list[str],
    budget_flag: bool,
    error: BaseException,
    api_key: str,
) -> dict[str, Any]:
    """Persist a failed audit as a Stage1 block instead of losing the run.

    A coverage pass is a qualification prerequisite, not the comparison
    itself.  Transport, JSON, and audit-contract failures therefore need to
    remain visible in the run artifact while keeping all affected stages
    ``unknown``.  This prevents the caller from treating an audit outage as a
    successful negative observation or as an unstructured process crash.
    """
    safe_error = str(error).strip()
    if api_key and safe_error:
        safe_error = safe_error.replace(api_key, "[REDACTED]")
    safe_error = safe_error[:500] or type(error).__name__
    _block_stage_qualifications(
        facts,
        target_stages,
        reason="独立覆盖审计失败，阶段资格保持未知；原子观察仍保留在 evidence_units。",
    )
    valid_ids = {
        str(item.get("id") or "").strip()
        for item in facts.get("evidence_units") or []
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    failed_stages = {
        stage: {
            "status": "unknown",
            "coverage": "unknown",
            "evidence_ids": [],
            "invalid_evidence_ids": [],
            "observed_signals": [],
            "missing_signals": [],
            "reason": "独立覆盖审计失败，不能确认该阶段是否已完整扫描。",
        }
        for stage in target_stages
    }
    audit = normalize_stage1_coverage_audit(
        {
            "version": STAGE1_COVERAGE_AUDIT_VERSION,
            "source": "pipeline",
            "status": "failed",
            "independence": STAGE1_COVERAGE_AUDIT_INDEPENDENCE,
            "target_stages": list(target_stages),
            "stages": failed_stages,
            "errors": [f"{type(error).__name__}: {safe_error}"],
        },
        valid_ids,
    )
    facts["stage1_coverage_audit"] = audit
    facts["stage1_recovery"] = {
        "source": "pipeline",
        "status": "coverage_audited_with_unresolved",
        "target_stages": list(target_stages),
        "unresolved_stages": list(target_stages),
        "candidate_unit_count": 0,
        "contract_issues_before_recovery": list(contract_issues),
        "trigger_reasons": list(dict.fromkeys([*trigger_reasons, "coverage_audit_failed"])),
        "budget_flag_before_recovery": budget_flag,
        "budget_status": "unresolved" if budget_flag else "not_flagged",
        "audit_independence": STAGE1_COVERAGE_AUDIT_INDEPENDENCE,
        "failure_reason": safe_error,
    }
    return facts


def _mark_video_fact_recovery_failed(
    facts: dict[str, Any],
    *,
    target_stages: list[str],
    trigger_reasons: list[str],
    contract_issues: list[str],
    budget_flag: bool,
    error: BaseException,
    api_key: str,
) -> dict[str, Any]:
    """Record a failed focused recovery without losing primary observations."""
    failed = _mark_video_fact_coverage_audit_failed(
        facts,
        target_stages=target_stages,
        trigger_reasons=trigger_reasons,
        contract_issues=contract_issues,
        budget_flag=budget_flag,
        error=error,
        api_key=api_key,
    )
    failed["stage1_coverage_audit"] = _materialize_stage_recovery_audit(
        failed,
        target_stages,
        errors=[f"focused_recovery_failed:{type(error).__name__}"],
    )
    recovery = failed.get("stage1_recovery") if isinstance(failed.get("stage1_recovery"), dict) else {}
    recovery.update(
        {
            "status": "focused_recovery_with_unresolved",
            "recovery_mode": "stage1_c_observe_stage1_d_qualify_once",
            "audit_independence": STAGE1_COVERAGE_AUDIT_INDEPENDENCE,
            "trigger_reasons": list(dict.fromkeys([
                *[str(value) for value in trigger_reasons],
                "focused_recovery_failed",
            ])),
        }
    )
    failed["stage1_recovery"] = recovery
    return failed


def _extend_stage1_acquisition_for_recovery(
    analysis: dict[str, Any],
    role: str,
    facts: dict[str, Any],
    payload: dict[str, Any],
    recovery_visual_inputs: list[dict[str, Any]],
    *,
    direct_audio: bool,
) -> dict[str, Any]:
    """Record media actually consumed by Stage1-C before Stage1-D gates it."""
    existing = (
        facts.get("stage1_acquisition")
        if isinstance(facts.get("stage1_acquisition"), dict)
        else {}
    )
    existing_channels = existing.get("channels") if isinstance(existing.get("channels"), dict) else {}
    native_video = existing.get("input_mode") == "native_video" or payload_has_video(payload)
    recovery_timestamps = _visual_input_timestamps(recovery_visual_inputs)
    if not native_video and not recovery_timestamps and not direct_audio:
        return copy.deepcopy(existing)
    visual_timestamps = [
        *[item for item in existing.get("visual_input_timestamps") or []],
        *recovery_timestamps,
    ]
    audio_ready = (
        isinstance(existing_channels.get("audio"), dict)
        and existing_channels["audio"].get("status") == "ready"
    )
    manifest = build_stage1_acquisition_manifest(
        analysis,
        role,
        native_video=native_video,
        visual_input_count=(
            int(existing_channels.get("visual", {}).get("count") or 0)
            if isinstance(existing_channels.get("visual"), dict)
            else 0
        ) + len(recovery_visual_inputs),
        visual_input_timestamps=visual_timestamps,
        audio_input_available=audio_ready or direct_audio,
    )
    manifest["provider_artifacts"] = copy.deepcopy(existing.get("provider_artifacts") or [])
    return manifest


def _maybe_recover_video_facts(
    args: argparse.Namespace,
    analysis: dict[str, Any],
    run_dir: Path,
    api_key: str,
    role: str,
    facts: dict[str, Any],
) -> dict[str, Any]:
    """Run one bounded, stage-focused Stage1-C recovery before facts lock.

    The pass receives only a read-only handoff and target-stage media windows.
    Stage1-C may append candidate observations, but only the focused Stage1-D
    judgment pass may replace target qualifications. This prevents the vision
    provider from owning business qualification or silently re-running the
    full-video extractor.
    """
    recovery_meta = facts.get("stage1_recovery") if isinstance(facts.get("stage1_recovery"), dict) else {}
    if (
        recovery_meta.get("source") == "pipeline"
        and recovery_meta.get("status") in {
            "focused_recovery",
            "focused_recovery_with_unresolved",
            # Read old artifacts without running a second recovery pass. New
            # cache validation rejects their stale audit version; this branch
            # only protects explicit offline imports.
            "coverage_audited",
            "coverage_audited_with_unresolved",
        }
    ):
        return facts
    budget_flag = facts.get("evidence_budget_exceeded") is True
    contract_issues = stage_evidence_contract_issues(facts, require_version=True)
    primary_targets = stage_evidence_recovery_targets(
        facts,
        include_budget=False,
        include_coverage_audit=False,
    )
    # A missed tail CTA is costly and common in noisy/local-language speech.
    # Open one bounded S6 tail review whenever S6 is unresolved and already
    # belongs to this single recovery pass; the prompt still requires semantic
    # confirmation and may keep the result unknown/absent.
    s6_check = stage_evidence_check_map(facts).get("S6")
    s6_explicitly_absent = (
        isinstance(s6_check, dict)
        and str(s6_check.get("status") or "").strip().lower() == "absent"
    )
    s6_status = str(s6_check.get("status") or "").strip().lower() if isinstance(s6_check, dict) else ""
    s6_coverage = str(s6_check.get("coverage") or "").strip().lower() if isinstance(s6_check, dict) else ""
    s6_tail_review_required = (
        "S6" in set(primary_targets)
        and not (s6_status == "present" and s6_coverage == "complete")
    )
    if s6_explicitly_absent and "S6" not in primary_targets:
        primary_targets.append("S6")
    issue_targets = [
        issue.split(":", 1)[0]
        for issue in contract_issues
        if issue.split(":", 1)[0] in set(stage_codes())
    ]
    if budget_flag:
        targets = list(stage_codes())
    else:
        targets = list(dict.fromkeys([*primary_targets, *issue_targets]))
        if contract_issues and not targets:
            # A global contract defect (for example an old contract version or
            # duplicate IDs) has no safe stage-local target. One bounded pass
            # is the narrowest honest recovery in that case.
            targets = list(stage_codes())
    trigger_reasons: list[str] = []
    if budget_flag:
        trigger_reasons.append("evidence_budget_exceeded")
    if primary_targets:
        trigger_reasons.append("stage_coverage_incomplete")
    if any(
        isinstance(stage_evidence_check_map(facts).get(code), dict)
        and str(stage_evidence_check_map(facts)[code].get("status") or "").strip().lower() == "conflict"
        for code in targets
    ) or contract_issues:
        trigger_reasons.append("evidence_qualification_conflict")
    if any(code in {"S3", "S4"} for code in targets):
        manifest = facts.get("stage1_acquisition") if isinstance(facts.get("stage1_acquisition"), dict) else {}
        if manifest.get("input_mode") == "canonical_frames":
            trigger_reasons.append("temporal_continuity_uncertain")
    if s6_explicitly_absent or s6_tail_review_required:
        trigger_reasons.append("s6_tail_unclosed")
    trigger_reasons = list(dict.fromkeys(trigger_reasons))
    if not targets and not trigger_reasons:
        facts["stage1_recovery"] = {
            "source": "pipeline",
            "status": "not_needed",
            "target_stages": [],
            "unresolved_stages": [],
            "candidate_unit_count": 0,
            "trigger_reasons": [],
            "audit_independence": STAGE1_COVERAGE_AUDIT_INDEPENDENCE,
        }
        return facts
    if args.llm_dry_run:
        facts["stage1_recovery"] = {
            "source": "pipeline",
            "status": "deferred_dry_run",
            "target_stages": targets,
            "trigger_reasons": trigger_reasons,
        }
        return facts

    request_path = run_dir / f"llm_facts_{role}_stage_recovery_request.json"
    response_path = run_dir / f"llm_facts_{role}_stage_recovery_response.json"
    artifact_path = stage_fact_artifact_path(run_dir, role, "C", targets)
    replay_source, provider_fallback_allowed = _stage1_replay_source(args)
    payload: dict[str, Any] = {}
    response_meta: dict[str, Any] = {}
    execution_source = "provider"
    request_started_at = time.monotonic()
    media_windows: list[dict[str, Any]] = []
    media_mode = "canonical_frames"
    request_bytes = 0
    recovery: dict[str, Any] | None = None
    recovery_visual_inputs: list[dict[str, Any]] = []
    recorded_artifact_path = artifact_path

    def validate_recovery_response(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("Stage1 focused recovery 必须返回 JSON object。")
        forbidden = stage1_forbidden_field_issues(value)
        pipeline_owned = stage1_pipeline_owned_field_issues(value)
        if forbidden:
            raise ValueError(
                "Stage1 focused recovery returned downstream fields: " + ", ".join(forbidden)
            )
        if pipeline_owned:
            raise ValueError(
                "Stage1 focused recovery returned pipeline-owned fields: "
                + ", ".join(pipeline_owned)
            )
        if value.get("stage_evidence_contract_version") != STAGE_EVIDENCE_CONTRACT_VERSION:
            raise ValueError("Stage1 focused recovery 缺少匹配的 evidence contract version。")
        if not isinstance(value.get("candidate_evidence_units"), list):
            raise ValueError("Stage1 focused recovery 的 candidate_evidence_units 必须是数组。")
        target_set = set(targets)
        for candidate in value["candidate_evidence_units"]:
            if not isinstance(candidate, dict):
                continue
            candidate_stages = {
                code
                for raw in candidate.get("functions") or []
                if (code := normalize_stage_code(str(raw).split("_", 1)[0])) is not None
            }
            out_of_scope = sorted(candidate_stages - target_set, key=list(stage_codes()).index)
            if out_of_scope:
                raise ValueError(
                    "Stage1-C candidate escaped target stages: " + ",".join(out_of_scope)
                )
        allowed_keys = {"candidate_evidence_units", "stage_evidence_contract_version"}
        out_of_contract = sorted(set(value) - allowed_keys)
        if out_of_contract:
            raise ValueError(
                "Stage1-C observation returned fields owned by another phase: "
                + ", ".join(out_of_contract)
            )
        return value

    replay_artifact_path = (
        stage_fact_artifact_path(replay_source, role, "C", targets)
        if replay_source is not None
        else None
    )
    preserve_resume_source = (
        provider_fallback_allowed
        and _same_existing_artifact(replay_artifact_path, artifact_path)
    )
    resume_failure_path = _resume_failure_artifact_path(artifact_path)
    try:
        video_info = analysis.get("videos", {}).get(role, {}) if isinstance(analysis.get("videos"), dict) else {}
        recovery_visual_inputs = select_stage_recovery_visual_inputs(
            video_info if isinstance(video_info, dict) else {},
            role,
            targets,
            image_limit=max(4, int(getattr(args, "llm_image_limit", 0) or 0)),
        )
        payload = build_video_fact_recovery_payload(
            vision_model(args),
            role,
            analysis,
            recovery_visual_inputs,
            stage_analysis_evidence_view(facts, targets),
            targets,
            api_url=args.llm_api_url,
            budget=getattr(args, "_resource_budget", None),
        )
        media_mode = (
            "focused_native_video"
            if payload_has_video(payload)
            else "focused_audio"
            if payload_has_audio(payload)
            else "canonical_frames"
        )
        media_windows = stage1_recovery_media_windows(
            analysis,
            role,
            targets,
            s6_tail_review=s6_tail_review_required or s6_explicitly_absent,
        )
        request_bytes = _payload_size_bytes(payload)
        if replay_source is not None:
            try:
                recovery, response_meta, _source_artifact = _read_replayable_stage_fact(
                    replay_source,
                    role=role,
                    phase="C",
                    group=targets,
                    payload=payload,
                    args=args,
                )
                execution_source = "replay"
                validate_recovery_response(recovery)
            except (StageFactArtifactError, ValueError) as exc:
                if not provider_fallback_allowed:
                    if isinstance(exc, StageFactArtifactError):
                        raise
                    raise StageFactArtifactError(
                        f"Stage1-C replay content contract invalid: {exc}"
                    ) from exc
                recovery = None
                response_meta = {}
                execution_source = "provider"
        if recovery is None:
            write_json(request_path, payload)
            recovery_text = fetch_json_completion(
                args, api_key, request_path, response_path, response_meta=response_meta
            )
            recovery = parse_json_text(recovery_text)
        recovery = validate_recovery_response(recovery)
        artifact = completed_stage_fact_artifact(
            role=role,
            phase="C",
            group=targets,
            payload=payload,
            response=recovery,
            model=vision_model(args),
            api_url=args.llm_api_url,
            response_meta=response_meta,
            artifact_name=artifact_path.name,
        )
        write_json(artifact_path, artifact)
        resume_failure_path.unlink(missing_ok=True)
    except (OSError, ValueError, RuntimeError, SystemExit) as exc:
        if _is_strict_replay_failure(args, exc):
            raise
        failure_artifact: dict[str, Any] | None = None
        if payload:
            failure_artifact = failed_stage_fact_artifact(
                role=role,
                phase="C",
                group=targets,
                payload=payload,
                model=vision_model(args),
                api_url=args.llm_api_url,
                error=str(exc) or type(exc).__name__,
                artifact_name=artifact_path.name,
                response_meta=response_meta,
                response=recovery,
            )
            failure_path = resume_failure_path if preserve_resume_source else artifact_path
            write_json(failure_path, failure_artifact)
            recorded_artifact_path = failure_path
        failed = _mark_video_fact_recovery_failed(
            facts,
            target_stages=targets,
            trigger_reasons=trigger_reasons,
            contract_issues=contract_issues,
            budget_flag=budget_flag,
            error=exc,
            api_key=api_key,
        )
        failure_meta = failed.get("stage1_recovery") if isinstance(failed.get("stage1_recovery"), dict) else {}
        failure_meta.update(
            {
                "media_mode": media_mode,
                "media_windows": media_windows,
                "request_bytes": request_bytes,
                "elapsed_seconds": round(max(0.0, time.monotonic() - request_started_at), 3),
                "effective_patch": {
                    "candidate_units_added": 0,
                    "resolved_stages": [],
                    "unresolved_stages": targets,
                },
            }
        )
        if failure_artifact is not None:
            failure_meta.update(
                {
                    "provider_status": "failed",
                    "execution_source": execution_source,
                    "provider_artifact": recorded_artifact_path.name,
                    "request_identity_sha256": failure_artifact["request_identity"]["sha256"],
                    "response_sha256": failure_artifact.get("response_sha256", ""),
                    "completion_attempts": response_meta.get("completion_attempts", 0),
                }
            )
        failed["stage1_recovery"] = failure_meta
        return failed

    recovery_for_merge = copy.deepcopy(recovery)
    recovery_direct_audio = payload_has_direct_audio(
        payload,
        api_url=args.llm_api_url,
        model=vision_model(args),
    )
    recovery_base = copy.deepcopy(facts)
    recovery_base["stage1_acquisition"] = _extend_stage1_acquisition_for_recovery(
        analysis,
        role,
        facts,
        payload,
        recovery_visual_inputs,
        direct_audio=recovery_direct_audio,
    )
    candidate_id_map: list[dict[str, Any]] = []
    sanitize_audio_observations(
        {
            "video_understanding": {
                role: {
                    "evidence_units": recovery_for_merge.get("candidate_evidence_units") or [],
                }
            },
            "stage_analysis": [],
        },
        recovery_direct_audio,
    )
    merged = _merge_video_fact_recovery(
        role,
        recovery_base,
        recovery_for_merge,
        analysis,
        targets,
        budget_exceeded=response_meta.get("finish_reason") == "length",
        candidate_id_map=candidate_id_map,
    )
    merged = _run_stage1_qualification(
        args,
        analysis,
        run_dir,
        api_key,
        role,
        merged,
        target_stages=targets,
    )
    merged["stage1_coverage_audit"] = _materialize_stage_recovery_audit(
        merged,
        targets,
    )
    unresolved_stages = [
        stage for stage in targets
        if stage in stage_evidence_recovery_targets(
            merged,
            include_budget=False,
            include_coverage_audit=True,
        )
    ]
    merged["stage1_recovery"] = {
        "source": "pipeline",
        "status": "focused_recovery_with_unresolved" if unresolved_stages else "focused_recovery",
        "recovery_mode": "stage1_c_observe_stage1_d_qualify_once",
        "target_stages": targets,
        "unresolved_stages": unresolved_stages,
        "candidate_unit_count": sum(
            item.get("status") == "accepted" for item in candidate_id_map
        ),
        "contract_issues_before_recovery": contract_issues,
        "trigger_reasons": trigger_reasons,
        "budget_flag_before_recovery": budget_flag,
        "budget_status": "resolved_for_stage_qualification" if budget_flag and not unresolved_stages else (
            "unresolved" if budget_flag else "not_flagged"
        ),
        "audit_independence": STAGE1_COVERAGE_AUDIT_INDEPENDENCE,
        "provider_status": "completed",
        "execution_source": execution_source,
        "provider_artifact": artifact_path.name,
        "request_identity_sha256": artifact["request_identity"]["sha256"],
        "response_sha256": artifact["response_sha256"],
        "completion_attempts": response_meta.get("completion_attempts", 0),
        "candidate_id_map": candidate_id_map,
        "qualification_provider_artifacts": [
            str(item.get("provider_artifact"))
            for item in (
                merged.get("stage1_qualification", {}).get("group_records", [])
                if isinstance(merged.get("stage1_qualification"), dict)
                else []
            )
            if isinstance(item, dict)
            and str(item.get("phase") or "").strip().upper() == "D"
            and item.get("provider_artifact")
        ],
        "media_mode": media_mode,
        "media_windows": media_windows,
        "request_bytes": request_bytes,
        "elapsed_seconds": round(max(0.0, time.monotonic() - request_started_at), 3),
        "effective_patch": {
            "candidate_units_added": sum(
                item.get("status") == "accepted" for item in candidate_id_map
            ),
            "resolved_stages": [stage for stage in targets if stage not in unresolved_stages],
            "unresolved_stages": unresolved_stages,
        },
    }
    final_issues = stage_evidence_contract_issues(merged, require_version=True)
    if final_issues:
        affected_stages = sorted({
            issue.split(":", 1)[0]
            for issue in final_issues
            if issue.split(":", 1)[0] in set(stage_codes())
        })
        unscoped_issues = [
            issue
            for issue in final_issues
            if issue.split(":", 1)[0] not in set(stage_codes())
        ]
        if affected_stages and not unscoped_issues:
            _block_stage_qualifications(
                merged,
                affected_stages,
                reason="该阶段资格在定向补观察后仍不满足结构合同，保持未知；原子观察仍保留。",
            )
            merged["stage1_coverage_audit"] = _materialize_stage_recovery_audit(
                merged,
                targets,
                errors=["focused_recovery_contract_invalid"],
            )
            recovery = merged.get("stage1_recovery") if isinstance(merged.get("stage1_recovery"), dict) else {}
            unresolved = list(dict.fromkeys([
                *[str(stage) for stage in recovery.get("unresolved_stages") or []],
                *affected_stages,
            ]))
            recovery.update(
                {
                    "status": "focused_recovery_with_unresolved",
                    "recovery_mode": "stage1_c_observe_stage1_d_qualify_once",
                    "unresolved_stages": unresolved,
                    "contract_issues_after_recovery": final_issues,
                }
            )
            merged["stage1_recovery"] = recovery
            remaining_issues = stage_evidence_contract_issues(merged, require_version=True)
            if not remaining_issues:
                _mark_stage1_qualification_recovered(merged, targets)
                return merged
            final_issues = remaining_issues
        return _mark_video_fact_recovery_failed(
            facts,
            target_stages=targets,
            trigger_reasons=trigger_reasons,
            contract_issues=[*contract_issues, *final_issues],
            budget_flag=budget_flag,
            error=ValueError("Stage1 evidence contract remains invalid after bounded recovery."),
            api_key=api_key,
        )
    _mark_stage1_qualification_recovered(merged, targets)
    return merged


def _merge_video_fact_recovery(
    role: str,
    base: dict[str, Any],
    recovery: dict[str, Any],
    analysis: dict[str, Any],
    target_stages: list[str],
    *,
    budget_exceeded: bool = False,
    candidate_id_map: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Append Stage1-C observations and fail closed pending Stage1-D."""
    code = "B" if role == "benchmark" else "C"
    merged = copy.deepcopy(base)
    # These fields are pipeline-owned and must not be fed through the
    # model-shaped fact normalizer.  The caller restores the locked metadata
    # after the candidate observations have been normalized.
    existing_acquisition = copy.deepcopy(merged.pop("stage1_acquisition", None))
    existing_qualification = copy.deepcopy(merged.pop("stage1_qualification", None))
    existing_coverage_audit = copy.deepcopy(merged.pop("stage1_coverage_audit", None))
    existing_units = merged.get("evidence_units") if isinstance(merged.get("evidence_units"), list) else []
    existing_units_snapshot = copy.deepcopy(existing_units)
    existing_ids = {
        str(item.get("id") or "").strip()
        for item in existing_units
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    candidates = recovery.get("candidate_evidence_units")
    if not isinstance(candidates, list):
        candidates = []
    safe_candidates: list[dict[str, Any]] = []
    allocated_ids = set(existing_ids)
    next_index = len(existing_units) + 1
    target_set = set(target_stages)
    mapping = candidate_id_map if candidate_id_map is not None else []
    for candidate_index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            continue
        item = copy.deepcopy(candidate)
        raw_candidate_id = str(item.get("id") or "").strip().upper()
        candidate_stages = {
            stage_code
            for value in item.get("functions") or []
            if (stage_code := normalize_stage_code(str(value).split("_", 1)[0])) is not None
        }
        out_of_scope = sorted(candidate_stages - target_set, key=list(stage_codes()).index)
        if out_of_scope:
            raise ValueError(
                "Stage1-C candidate escaped target stages: " + ",".join(out_of_scope)
            )
        # Stage1-C candidate IDs are response-local handles. The code owns the
        # canonical ledger namespace and allocates the final ID. Stage1-C owns
        # no qualification references; Stage1-D sees only these canonical IDs.
        candidate_id = normalized_fact_id(raw_candidate_id, code, next_index, allocated_ids)
        item["id"] = candidate_id
        safe_candidates.append(item)
        mapping.append(
            {
                "candidate_index": candidate_index,
                "raw_id": raw_candidate_id,
                "canonical_id": candidate_id,
                "status": "accepted",
            }
        )
        next_index += 1
    normalized_candidates: list[dict[str, Any]] = []
    if safe_candidates:
        candidate_result = normalize_video_fact_result(
            role,
            {
                "evidence_units": safe_candidates,
                "stage_evidence_contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            },
            analysis,
            allow_trusted_pipeline_metadata=True,
        )
        normalized_candidates = candidate_result.get("evidence_units") or []
    # Existing locked observations are copied byte-for-byte. Only newly
    # appended candidates go through normalization.
    merged["evidence_units"] = existing_units_snapshot + normalized_candidates
    merged["stage_evidence_contract_version"] = STAGE_EVIDENCE_CONTRACT_VERSION

    normalized = normalize_video_fact_result(
        role,
        merged,
        analysis,
        allow_trusted_pipeline_metadata=True,
    )
    normalized["evidence_units"] = existing_units_snapshot + copy.deepcopy(normalized_candidates)
    # ``stage1_acquisition`` is code-owned and is deliberately discarded from
    # model-shaped normalization. Preserve the locked manifest while bounded
    # recovery replaces only the requested factual observations.
    if isinstance(existing_acquisition, dict):
        normalized["stage1_acquisition"] = existing_acquisition
    if isinstance(existing_qualification, dict):
        normalized["stage1_qualification"] = existing_qualification
    if isinstance(existing_coverage_audit, dict):
        normalized["stage1_coverage_audit"] = existing_coverage_audit
    normalized["evidence_budget_exceeded"] = bool(
        base.get("evidence_budget_exceeded") is True or budget_exceeded
    )
    _block_stage_qualifications(
        normalized,
        target_stages,
        reason="Stage1-C 已追加候选观察；目标阶段等待 Stage1-D 独立资格投影。",
    )
    return normalized


def _mark_stage1_qualification_recovered(
    facts: dict[str, Any],
    target_stages: list[str],
) -> dict[str, Any]:
    """Close Stage1-B metadata after a valid Stage1-C/D recovery.

    Stage1-C appends observations and Stage1-D may replace qualifications after
    an independent Stage1-B request failed. The original failure remains useful
    provenance, but keeping ``status=failed`` would make the valid recovered
    fact set fail cache validation and misrepresent the final handoff.
    """
    metadata = facts.get("stage1_qualification")
    if not isinstance(metadata, dict):
        return facts
    normalized_targets = [
        code
        for code in (
            normalize_stage_code(value)
            for value in target_stages
        )
        if code in set(stage_codes())
    ]
    failed_codes = {
        code
        for code in (
            normalize_stage_code(value)
            for value in metadata.get("failed_stage_codes") or []
        )
        if code in set(stage_codes())
    }
    source_failed_codes = {
        code
        for code in (
            normalize_stage_code(value)
            for value in metadata.get("requalification_source_failed_stage_codes") or []
        )
        if code in set(stage_codes())
    }
    if metadata.get("status") == "failed" and not failed_codes and not source_failed_codes:
        # Older failed artifacts did not persist failed_stage_codes. Treat the
        # bounded recovery targets as failed until their final readiness closes.
        source_failed_codes = set(normalized_targets)
    if metadata.get("status") != "failed" and not failed_codes and not source_failed_codes:
        return facts
    recovered = copy.deepcopy(metadata)
    initial_failure_reason = str(recovered.get("failure_reason") or "").strip()
    resolved_statuses = {"present", "absent", "not_applicable"}
    recovered_codes = [
        code
        for code in normalized_targets
        if (
            code in set(stage_codes())
            and (not source_failed_codes or code in source_failed_codes)
            and stage_evidence_readiness(facts, code) in resolved_statuses
        )
    ]
    remaining_failed_codes = [
        code for code in sorted(failed_codes, key=list(stage_codes()).index)
        if code not in recovered_codes
    ]
    recovered.update(
        {
            "status": "failed" if remaining_failed_codes else "completed",
            "recovered_from": (
                "stage1_b_partial_failure" if metadata.get("status") == "completed"
                else "stage1_b_failed"
            ),
            "recovered_stage_codes": list(dict.fromkeys(recovered_codes)),
            "failed_stage_codes": remaining_failed_codes,
        }
    )
    if initial_failure_reason:
        recovered["initial_failure_reason"] = initial_failure_reason
    facts["stage1_qualification"] = recovered
    return facts


def run_video_identity_extraction(
    args: argparse.Namespace,
    analysis: dict[str, Any],
    run_dir: Path,
    api_key: str,
) -> dict[str, Any]:
    """scope 预检专用：每侧只取产品身份，避免完整事实抽取的原生视频和音频成本。"""
    identities: dict[str, Any] = {}
    videos = analysis.get("videos", {})
    for role in ("benchmark", "creator"):
        if role not in videos:
            continue
        payload = build_video_identity_payload(
            vision_model(args),
            role,
            analysis,
            select_role_visual_inputs(videos[role], role, image_limit=2),
        )
        request_path = run_dir / f"llm_identity_{role}_request.json"
        response_path = run_dir / f"llm_identity_{role}_response.json"
        result_path = run_dir / f"video_identity_{role}.json"
        write_json(request_path, payload)
        if args.llm_dry_run:
            request_path.unlink(missing_ok=True)
            continue
        live_meta: dict[str, Any] = {}
        response, response_meta, execution_source = provider_call_with_artifact(
            artifact_path=run_dir / f"provider_video_identity_{role}.json",
            replay_root=getattr(args, "provider_replay_from", None),
            call_kind=f"video_identity:{role}",
            payload=payload,
            model=vision_model(args),
            api_url=args.llm_api_url,
            response_meta=live_meta,
            call=lambda: (
                parse_json_text(
                    fetch_json_completion(
                        args,
                        api_key,
                        request_path,
                        response_path,
                        response_meta=live_meta,
                    )
                ),
                live_meta,
            ),
        )
        identity = {"product_identity": normalize_video_product_identity(response.get("product_identity"))}
        identities[role] = identity
        write_json(result_path, identity)
        write_json(run_dir / f"video_identity_{role}_provider_meta.json", response_meta)
        write_json(
            run_dir / f"video_identity_{role}_provider_artifact_ref.json",
            {"path": f"provider_video_identity_{role}.json", "execution_source": execution_source},
        )
    return identities
