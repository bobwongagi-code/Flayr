"""Cache records, identities, and Stage1 artifact reuse validation.

This module owns the filesystem cache contract used by the LLM pipeline.  It
does not decide semantic results or invoke a provider; it only computes
identity keys, validates serialized records, and snapshots the provider
artifacts referenced by an already-built fact ledger.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Callable

from ..analysis_model import schema_sha256
from ..stage_evidence_contracts import (
    STAGE_EVIDENCE_CONTRACT_VERSION,
    normalize_stage1_coverage_audit,
    normalize_stage_code,
    stage1_coverage_audit_issues,
    stage_codes,
    stage_evidence_snapshot_issues,
)
from ..utils import write_json
from .artifact_identity import identity_value
from .parse import normalize_video_fact_result
from .stage_fact_artifacts import (
    StageFactArtifactError,
    read_stage_fact_artifact,
)


# These versions are part of the persisted cache contract.  Keep them stable
# across a structural extraction; callers still expose the old pipeline names.
VIDEO_FACT_CACHE_SCHEMA_VERSION = 31
PRODUCT_FOUNDATION_CACHE_SCHEMA_VERSION = 3
CACHE_RECORD_SCHEMA_VERSION = 1


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


def _git_commit_sha(repo_root: Path) -> str:
    """Return the current code identity without making cache reuse depend on git availability."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
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


def _cache_reference_digests(repo_root: Path) -> dict[str, str]:
    """Hash code and reference material that changes the meaning of an LLM request."""
    llm_dir = repo_root / "scripts" / "flayr_core" / "llm"
    candidates = set(llm_dir.glob("*.py")) if llm_dir.is_dir() else set()
    candidates.update(
        repo_root / relative_path
        for relative_path in (
            "scripts/flayr_core/stage_evidence_contracts.py",
            "scripts/flayr_core/stage_contract_registry.py",
            "scripts/flayr_core/stage_ownership.py",
            "scripts/flayr_core/market.py",
            "scripts/flayr_core/structure_modules.py",
            "structure_library_full.md",
            "QA-RULES.md",
        )
    )
    references_dir = repo_root / "references"
    if references_dir.is_dir():
        candidates.update(path for path in references_dir.iterdir() if path.is_file())
    return {
        relative_path: _sha256_file(repo_root / relative_path)
        for relative_path in sorted(
            {
                path.relative_to(repo_root).as_posix()
                for path in candidates
                if path.is_file()
            }
        )
    }


def _product_context_digest(analysis: dict[str, Any]) -> str:
    product = analysis.get("product") if isinstance(analysis.get("product"), dict) else {}
    brand = analysis.get("brand_proposition") if isinstance(analysis.get("brand_proposition"), dict) else {}
    return _stable_digest({"product": product, "brand_proposition": brand})


def _product_foundation_cache_key(
    args: argparse.Namespace,
    analysis: dict[str, Any],
    *,
    judgment_model_fn: Callable[[argparse.Namespace], str],
    payload_builder: Callable[[str, dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    model = judgment_model_fn(args)
    payload = payload_builder(model, analysis)
    return {
        "cache_schema_version": PRODUCT_FOUNDATION_CACHE_SCHEMA_VERSION,
        "llm_model": model,
        "llm_api_url": str(args.llm_api_url or ""),
        "temperature": 0.0,
        "product_context_digest": _product_context_digest(analysis),
        "request_payload_sha256": _stable_digest(identity_value(payload)),
    }


def _video_fact_cache_key(
    args: argparse.Namespace,
    analysis: dict[str, Any],
    role: str,
    *,
    judgment_model_fn: Callable[[argparse.Namespace], str],
    vision_model_fn: Callable[[argparse.Namespace], str],
    git_commit_sha_fn: Callable[[], str],
    cache_reference_digests_fn: Callable[[], dict[str, str]],
) -> dict[str, Any]:
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
        "judgment_model": judgment_model_fn(args),
        "vision_model": vision_model_fn(args),
        "llm_api_url": str(args.llm_api_url or ""),
        "foundation_digest": _stable_digest(foundation),
        "product_context_digest": _product_context_digest(analysis),
        "code_commit": git_commit_sha_fn(),
        "reference_digests": cache_reference_digests_fn(),
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
