"""验证 cohort 冻结与 GT 契约。

本模块不调用模型。它用内容哈希锁定 blind 批次，避免同一批样本在看过结果并
修改规则后仍被当作泛化验收集。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .freeze_contract import (
    EVALUATOR_CONTRACT_FILES,
    PROMPT_CONTRACT_FILES,
    REQUIRED_FREEZE_CHECKS,
    SCHEMA_CONTRACT_FILES,
    cohort_freeze_status,
    evaluation_role_for_sample,
    format_freeze_blocked,
)
from .model_execution import ModelExecutionConfig

LOCK_SCHEMA_VERSION = 2
LEGACY_LOCK_SCHEMA_VERSION = 1
LOCK_STATUSES = {"frozen", "spent"}
EXECUTION_VALUES = {0.0, 0.5, 1.0, 2.0}
RELATIONS = {"creator_better", "tie", "benchmark_better", "uncertain"}
LEGACY_RELATIONS = {"matched"}
GAP_MAGNITUDES = {"none", "small", "medium", "large", "uncertain", "not_applicable"}
CONFIDENCE_VALUES = {"low", "medium", "high"}
STAGES = tuple(f"S{index}" for index in range(1, 7))
STAGE_LABEL_STATUSES = {"labeled", "not_applicable", "uncertain", "missing"}
SOURCE_CONTRACT_FILES = (
    "ARCHITECTURE.md",
    "structure_library_full.md",
    "QA-RULES.md",
    "references/ADR006.md",
    "references/analysis-output-schema.json",
    "references/brand_propositions.json",
    "references/commercial-judgement-framework.md",
    "references/observation-guide.md",
    "scripts/flayr_core/llm/payload.py",
    "scripts/flayr_core/llm/parse.py",
    "scripts/flayr_core/llm/pipeline.py",
    "scripts/flayr_core/llm/s4_visual_verifier.py",
    "scripts/flayr_core/llm/stage_review_contract.py",
    "scripts/flayr_core/evidence_states.py",
    "scripts/flayr_core/finalization/__init__.py",
    "scripts/flayr_core/finalization/contracts.py",
    "scripts/flayr_core/finalization/facade.py",
    "scripts/flayr_core/postprocess/chain.py",
    "scripts/flayr_core/postprocess/calibration.py",
    "scripts/flayr_core/postprocess/derive.py",
    "scripts/flayr_core/postprocess/repair_evidence.py",
    "scripts/flayr_core/postprocess/validate.py",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根节点必须是 object：{path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def resolve_manifest_video_path(value: Any) -> Path:
    """Resolve a local validation path after expanding its documented env root."""
    raw = os.path.expandvars(str(value or "")).strip()
    return Path(raw).expanduser()


def manifest_samples(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    samples = manifest.get("samples")
    if not isinstance(samples, list):
        return {}
    return {
        str(sample.get("id")): sample
        for sample in samples
        if isinstance(sample, dict) and str(sample.get("id") or "").strip()
    }


def stage_label_status(label: dict[str, Any], stage: str) -> tuple[str, str]:
    """Return the explicit GT status and reason for one stage.

    Historical labels may omit this metadata. They remain readable, but a blind
    cohort must make every `na` explicit so that inapplicability is never
    conflated with a missing annotation.
    """
    statuses = label.get("stage_label_statuses") if isinstance(label.get("stage_label_statuses"), dict) else {}
    entry = statuses.get(stage)
    if isinstance(entry, dict):
        return str(entry.get("status") or "").strip(), str(entry.get("reason") or "").strip()
    gap_values, _ = _stage_gap_values(label)
    gap = str(gap_values.get(stage) or "").strip().lower()
    if gap in {"na", "not_applicable"}:
        return "not_applicable_legacy", ""
    if gap == "uncertain":
        return "uncertain_legacy", ""
    if gap in {"none", "small", "medium", "large"}:
        return "labeled", ""
    return "missing", ""


def _finite_time_range(value: Any) -> bool:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return False
    try:
        start = float(value[0])
        end = float(value[1])
    except (TypeError, ValueError):
        return False
    return math.isfinite(start) and math.isfinite(end) and start >= 0 and end > start


def _stage_gap_values(label: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return canonical human_gap values and whether the new axis is present."""
    human_gap = label.get("human_gap")
    if isinstance(human_gap, dict):
        return human_gap, True
    stages = label.get("stages")
    return stages if isinstance(stages, dict) else {}, False


def _normalize_gap_value(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return "not_applicable" if normalized == "na" else normalized


def _stage_relations(label: dict[str, Any]) -> dict[str, Any]:
    relations = label.get("stage_relations")
    if not isinstance(relations, dict):
        relations = label.get("relations")
    return relations if isinstance(relations, dict) else {}


def _valid_relation(value: Any, *, allow_legacy: bool) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized in RELATIONS or (allow_legacy and normalized in LEGACY_RELATIONS)


def _relation_gap_compatible(relation: str, gap: str) -> bool:
    if relation == "uncertain" or gap == "uncertain":
        return True
    if gap == "none":
        return relation == "tie"
    if relation == "tie":
        return False
    return relation in {"creator_better", "benchmark_better"}


def validate_blind_sample_contract(
    sample_id: str,
    label: dict[str, Any],
    sample: dict[str, Any] | None,
    *,
    require_canonical: bool = True,
) -> list[str]:
    """校验新 blind 样本具备分层诊断所需的人工 GT。"""
    errors: list[str] = []
    if label.get("partition") != "blind":
        errors.append(f"{sample_id}: GT partition 必须是 blind")
    if not isinstance(sample, dict) or sample.get("group") != "blind":
        errors.append(f"{sample_id}: validation-inputs group 必须是 blind")
    evaluation_scope = str(label.get("evaluation_scope") or "stage_severity")
    if evaluation_scope == "whole_video_observation":
        if not str(label.get("overall_verdict") or "").strip() or not str(label.get("overall_reason") or "").strip():
            errors.append(f"{sample_id}: whole_video_observation 缺 overall_verdict/overall_reason")
        return errors

    gap_values, has_canonical_gap = _stage_gap_values(label)
    relations = _stage_relations(label)
    legacy_contract = not has_canonical_gap and not require_canonical
    if not has_canonical_gap and require_canonical:
        errors.append(f"{sample_id}: blind GT 必须提供 human_gap；stages 只能作为 legacy 投影")
    if not isinstance(label.get("stage_relations"), dict) and require_canonical:
        errors.append(f"{sample_id}: blind GT 必须提供 stage_relations")
    legacy_stages = label.get("stages")
    if has_canonical_gap and isinstance(legacy_stages, dict):
        for stage in STAGES:
            canonical = _normalize_gap_value(gap_values.get(stage))
            projection = _normalize_gap_value(legacy_stages.get(stage))
            if canonical and projection and canonical != projection:
                errors.append(f"{sample_id}: {stage} 的 stages 兼容投影与 human_gap 不一致")
    canonical_relations = label.get("stage_relations")
    legacy_relations = label.get("relations")
    if isinstance(canonical_relations, dict) and isinstance(legacy_relations, dict):
        for stage in STAGES:
            canonical = str(canonical_relations.get(stage) or "").strip().lower()
            projection = str(legacy_relations.get(stage) or "").strip().lower()
            if canonical and projection and canonical != projection:
                errors.append(f"{sample_id}: {stage} 的 relations 兼容投影与 stage_relations 不一致")
    oracles = label.get("stage_oracles") if isinstance(label.get("stage_oracles"), dict) else {}
    events = label.get("key_events") if isinstance(label.get("key_events"), list) else []
    event_ids = [str(event.get("id") or "").strip() for event in events if isinstance(event, dict)]
    if not events and require_canonical:
        errors.append(f"{sample_id}: blind GT 必须提供非空 key_events")
    if len(event_ids) != len(set(event_ids)) or any(not value for value in event_ids):
        errors.append(f"{sample_id}: key_events id 不能为空或重复")
    for index, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            errors.append(f"{sample_id}: key_events[{index}] 必须是 object")
            continue
        if event.get("role") not in {"creator", "benchmark"} or event.get("stage") not in STAGES:
            errors.append(f"{sample_id}: key_events[{index}] 缺有效 role/stage")
        time_range = event.get("time_range")
        if not _finite_time_range(time_range):
            errors.append(f"{sample_id}: key_events[{index}].time_range 必须是有限、非负且 start<end 的 [start,end]")
        expected_state = str(event.get("expected_state") or "present")
        if expected_state not in {"present", "absent"}:
            errors.append(f"{sample_id}: key_events[{index}].expected_state 非法")
        terms_any = event.get("terms_any")
        if expected_state == "absent" and (
            not isinstance(terms_any, list)
            or not any(str(value).strip() for value in terms_any)
        ):
            errors.append(f"{sample_id}: key_events[{index}] 缺失事件必须提供 terms_any")

    for stage in STAGES:
        gap = str(gap_values.get(stage) or "").strip().lower()
        normalized_gap = _normalize_gap_value(gap)
        allowed_gaps = GAP_MAGNITUDES | ({"na"} if legacy_contract else set())
        if gap not in allowed_gaps:
            errors.append(f"{sample_id}: {stage} 缺有效 human_gap")
            continue
        status, reason = stage_label_status(label, stage)
        allowed_statuses = STAGE_LABEL_STATUSES | ({"not_applicable_legacy", "uncertain_legacy"} if legacy_contract else set())
        if status not in allowed_statuses:
            errors.append(f"{sample_id}: {stage} stage_label_status 非法")
        if normalized_gap == "not_applicable" and require_canonical and (status != "not_applicable" or not reason):
            errors.append(f"{sample_id}: {stage}=not_applicable 必须标记 not_applicable 并说明原因")
        if normalized_gap == "uncertain" and require_canonical and (status != "uncertain" or not reason):
            errors.append(f"{sample_id}: {stage}=uncertain 必须标记 uncertain 并说明原因")
        if normalized_gap in {"none", "small", "medium", "large"} and status != "labeled":
            errors.append(f"{sample_id}: {stage} 有可评分 human_gap 时 stage_label_status 必须为 labeled")
        relation = str(relations.get(stage) or "").strip().lower()
        if normalized_gap == "not_applicable":
            if relation and not _valid_relation(relation, allow_legacy=legacy_contract):
                errors.append(f"{sample_id}: {stage}.stage_relations 非法")
            continue
        if require_canonical and not _valid_relation(relation, allow_legacy=False):
            errors.append(f"{sample_id}: {stage}.stage_relations 非法")
        elif relation and not _valid_relation(relation, allow_legacy=legacy_contract):
            errors.append(f"{sample_id}: {stage}.stage_relations 非法")
        elif relation and normalized_gap in {"none", "small", "medium", "large"} and not _relation_gap_compatible(relation, normalized_gap):
            errors.append(f"{sample_id}: {stage} relation 与 human_gap 组合矛盾")
        if normalized_gap == "uncertain":
            continue
        oracle = oracles.get(stage)
        if not isinstance(oracle, dict):
            errors.append(f"{sample_id}: {stage} 缺 stage_oracles")
            continue
        for role in ("creator", "benchmark"):
            value = oracle.get(f"{role}_execution")
            if not isinstance(value, (int, float)) or float(value) not in EXECUTION_VALUES:
                errors.append(f"{sample_id}: {stage}.{role}_execution 必须是 0/0.5/1/2")
        oracle_relation = str(oracle.get("relation") or "").strip().lower()
        if oracle_relation == "matched":
            oracle_relation = "tie"
        if not _valid_relation(oracle.get("relation"), allow_legacy=legacy_contract):
            errors.append(f"{sample_id}: {stage}.relation 非法")
        elif relation in RELATIONS and oracle_relation in RELATIONS and relation != oracle_relation:
            errors.append(f"{sample_id}: {stage} stage_relations 与 stage_oracles.relation 不一致")
        if oracle.get("confidence") not in CONFIDENCE_VALUES:
            errors.append(f"{sample_id}: {stage}.confidence 非法")
        if not str(oracle.get("reason") or "").strip():
            errors.append(f"{sample_id}: {stage}.reason 不能为空")
        decision_ids = oracle.get("decision_event_ids")
        if not isinstance(decision_ids, list) or not decision_ids:
            errors.append(f"{sample_id}: {stage}.decision_event_ids 必须是非空数组")
        else:
            normalized_ids = [str(value).strip() for value in decision_ids]
            if any(not value for value in normalized_ids) or len(normalized_ids) != len(set(normalized_ids)):
                errors.append(f"{sample_id}: {stage}.decision_event_ids 不能为空或重复")
            unknown = sorted(set(normalized_ids) - set(event_ids))
            if unknown:
                errors.append(f"{sample_id}: {stage} 引用未知 key_event：{','.join(unknown)}")

    if not require_canonical and not isinstance(label.get("decision_gt"), dict):
        return errors
    decision_gt = label.get("decision_gt") if isinstance(label.get("decision_gt"), dict) else {}
    roots = decision_gt.get("top_root_causes") if isinstance(decision_gt.get("top_root_causes"), list) else []
    if not roots:
        errors.append(f"{sample_id}: 缺 decision_gt.top_root_causes")
    priorities = []
    for index, root in enumerate(roots, start=1):
        if not isinstance(root, dict):
            errors.append(f"{sample_id}: top_root_causes[{index}] 必须是 object")
            continue
        if not str(root.get("reference_id") or "").strip() or not str(root.get("reason") or "").strip():
            errors.append(f"{sample_id}: top_root_causes[{index}] 缺 reference_id/reason")
        priority = root.get("priority")
        if not isinstance(priority, int) or priority < 1:
            errors.append(f"{sample_id}: top_root_causes[{index}].priority 必须是正整数")
        else:
            priorities.append(priority)
        evidence_ids = root.get("evidence_event_ids")
        if not isinstance(evidence_ids, list):
            errors.append(f"{sample_id}: top_root_causes[{index}].evidence_event_ids 必须是数组")
        elif require_canonical and not evidence_ids:
            errors.append(f"{sample_id}: top_root_causes[{index}].evidence_event_ids 不能为空")
        elif set(str(value) for value in evidence_ids) - set(event_ids):
            errors.append(f"{sample_id}: top_root_causes[{index}] 引用未知 key_event")
    if priorities and sorted(priorities) != list(range(1, len(priorities) + 1)):
        errors.append(f"{sample_id}: top_root_causes priority 必须从 1 连续排列")
    return errors


def _git_value(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=False
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _worktree_identity(root: Path) -> dict[str, Any]:
    """锁定 tracked diff 与未跟踪文件内容；gitignored 运行产物不参与。"""
    status = _git_value(root, "status", "--porcelain=v1", "-uall")
    diff = _git_value(root, "diff", "--binary", "--", ".")
    untracked = _git_value(root, "ls-files", "--others", "--exclude-standard").splitlines()
    untracked_files = {
        relative: sha256_file(root / relative)
        for relative in sorted(untracked)
        if (root / relative).is_file()
    }
    fingerprint = sha256_json({
        "status": status,
        "diff": diff,
        "untracked_files": untracked_files,
    })
    return {
        "clean": not bool(status),
        "status_sha256": hashlib.sha256(status.encode("utf-8")).hexdigest(),
        "diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        "untracked_files": untracked_files,
        "fingerprint_sha256": fingerprint,
    }


def _file_identity(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"文件不存在：{resolved}")
    return {
        "path": str(resolved),
        "size_bytes": resolved.stat().st_size,
        "sha256": sha256_file(resolved),
    }


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.expanduser().resolve().relative_to(parent.expanduser().resolve())
    except ValueError:
        return False
    return True


def _validation_root_from_env() -> tuple[Path | None, str | None]:
    raw = os.environ.get("FLAYR_VALIDATION_ROOT", "").strip()
    if not raw:
        return None, "missing_validation_root"
    resolved = Path(os.path.expandvars(raw)).expanduser().resolve()
    if not resolved.is_dir():
        return None, "validation_root_missing"
    return resolved, None


def _surface_identities(root: Path, relatives: tuple[str, ...]) -> tuple[dict[str, Any], list[str]]:
    identities: dict[str, Any] = {}
    errors: list[str] = []
    for relative in relatives:
        try:
            identities[relative] = _file_identity(root / relative)
        except ValueError as exc:
            errors.append(f"{relative}: {exc}")
    return identities, errors


def _contract_surface_hashes(root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, str], list[str]]:
    surfaces: dict[str, tuple[str, ...]] = {
        "prompt": PROMPT_CONTRACT_FILES,
        "schema": SCHEMA_CONTRACT_FILES,
        "evaluator": EVALUATOR_CONTRACT_FILES,
    }
    identities: dict[str, dict[str, Any]] = {}
    hashes: dict[str, str] = {}
    errors: list[str] = []
    for name, relatives in surfaces.items():
        current, current_errors = _surface_identities(root, relatives)
        identities[name] = current
        errors.extend(f"{name} surface: {error}" for error in current_errors)
        hashes[f"{name}_sha256"] = sha256_json(current)
    return identities, hashes, errors


def _locked_video_payload(samples: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "samples": [
            {"id": str(sample.get("id") or ""), "videos": sample.get("videos")}
            for sample in samples
        ]
    }


def _model_execution_error(model_config: Any) -> ValueError:
    checks = {name: {"ok": False} for name in REQUIRED_FREEZE_CHECKS}
    status = cohort_freeze_status(checks, ["model_config_hash"])
    try:
        ModelExecutionConfig.from_mapping(model_config)
    except ValueError as exc:
        return ValueError(f"{format_freeze_blocked(status)}\n- {exc}")
    return ValueError(format_freeze_blocked(status))


def _verify_surface_identities(
    expected: Any,
    relatives: tuple[str, ...],
    name: str,
) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    if not isinstance(expected, dict):
        return [f"缺 freeze contract surface：{name}"], {}
    missing = sorted(set(relatives) - set(expected))
    if missing:
        errors.append(f"缺 {name} surface identity：" + ",".join(missing))
    for relative, identity in expected.items():
        if not isinstance(identity, dict):
            errors.append(f"{name} surface identity 非法：{relative}")
            continue
        path = Path(str(identity.get("path") or ""))
        if not path.is_file() or sha256_file(path) != identity.get("sha256"):
            errors.append(f"{name} surface 已漂移或缺失：{relative}")
    return errors, expected


def _verify_v2_freeze_header(lock: dict[str, Any]) -> list[str]:
    """Verify governance fields that do not depend on sample iteration."""
    errors: list[str] = []
    try:
        execution_config = ModelExecutionConfig.from_mapping(lock.get("model_execution_config"))
    except (TypeError, ValueError) as exc:
        errors.append(f"model_execution_config 非法：{exc}")
        execution_config = None
    if execution_config is not None:
        expected_hash = execution_config.sha256
        if lock.get("model_execution_config_sha256") != expected_hash:
            errors.append("model execution config hash 已漂移")
        if lock.get("model_config") != execution_config.as_dict():
            errors.append("model_config compatibility alias 与 model_execution_config 不一致")

    contract_hashes = lock.get("contract_hashes") if isinstance(lock.get("contract_hashes"), dict) else {}
    surfaces = lock.get("freeze_contract_files") if isinstance(lock.get("freeze_contract_files"), dict) else {}
    surface_specs = {
        "prompt": PROMPT_CONTRACT_FILES,
        "schema": SCHEMA_CONTRACT_FILES,
        "evaluator": EVALUATOR_CONTRACT_FILES,
    }
    for name, relatives in surface_specs.items():
        surface_errors, identities = _verify_surface_identities(
            surfaces.get(name),
            relatives,
            name,
        )
        errors.extend(surface_errors)
        expected_hash = sha256_json(identities)
        if contract_hashes.get(f"{name}_sha256") != expected_hash:
            errors.append(f"{name} hash 已漂移")

    status = lock.get("cohort_freeze_status")
    if not isinstance(status, dict) or status.get("status") != "READY" or status.get("blocked"):
        errors.append("cohort_freeze_status 不是 READY")
    if lock.get("evaluation_role") != "blind_promotion":
        errors.append("cohort lock evaluation_role 必须是 blind_promotion")

    validation_root, root_error = _validation_root_from_env()
    if root_error:
        errors.append(f"FLAYR_VALIDATION_ROOT：{root_error}")
    else:
        locked_root = lock.get("validation_root") if isinstance(lock.get("validation_root"), dict) else {}
        if locked_root.get("path_sha256") != sha256_json(str(validation_root)):
            errors.append("FLAYR_VALIDATION_ROOT identity 已漂移")
    return errors


def build_cohort_lock(
    root: Path,
    labels_path: Path,
    manifest_path: Path,
    sample_ids: list[str],
    model_config: dict[str, Any],
) -> dict[str, Any]:
    """构建可复核的 blind cohort 锁；只读输入，不运行分析。"""
    if not sample_ids or len(sample_ids) != len(set(sample_ids)):
        raise ValueError("sample_ids 必须非空且不能重复")
    try:
        execution_config = ModelExecutionConfig.from_mapping(model_config)
    except (TypeError, ValueError) as exc:
        raise _model_execution_error(model_config) from exc
    normalized_model_config = execution_config.as_dict()
    labels = read_json(labels_path)
    manifest = read_json(manifest_path)
    label_samples = labels.get("samples") if isinstance(labels.get("samples"), dict) else {}
    inputs = manifest_samples(manifest)
    errors: list[str] = []
    locked_samples: list[dict[str, Any]] = []
    selected_hashes: set[str] = set()
    validation_root, validation_root_error = _validation_root_from_env()
    if validation_root_error:
        errors.append(validation_root_error)

    historical_hashes: dict[str, str] = {}
    for historical_id, sample in inputs.items():
        if historical_id in sample_ids:
            continue
        for field in ("creator_video", "benchmark_video"):
            candidate = resolve_manifest_video_path(sample.get(field))
            if candidate.is_file():
                historical_hashes.setdefault(sha256_file(candidate), f"{historical_id}.{field}")

    for sample_id in sample_ids:
        label = label_samples.get(sample_id)
        sample = inputs.get(sample_id)
        if not isinstance(label, dict):
            errors.append(f"{sample_id}: 缺 GT")
            continue
        errors.extend(validate_blind_sample_contract(sample_id, label, sample))
        evaluation_role, role_errors = evaluation_role_for_sample(label, sample)
        errors.extend(f"{sample_id}: {error}" for error in role_errors)
        if evaluation_role != "blind_promotion":
            errors.append(f"{sample_id}: evaluation_role 必须是 blind_promotion")
        if not isinstance(sample, dict):
            continue
        videos: dict[str, Any] = {}
        for role, field in (("creator", "creator_video"), ("benchmark", "benchmark_video")):
            try:
                identity = _file_identity(resolve_manifest_video_path(sample.get(field)))
            except ValueError as exc:
                errors.append(f"{sample_id}.{field}: {exc}")
                continue
            digest = identity["sha256"]
            if digest in historical_hashes:
                errors.append(f"{sample_id}.{field}: 视频内容复用了 {historical_hashes[digest]}")
            if digest in selected_hashes:
                errors.append(f"{sample_id}.{field}: cohort 内视频内容重复")
            selected_hashes.add(digest)
            if validation_root is not None and not _path_is_within(Path(str(identity["path"])), validation_root):
                errors.append(f"{sample_id}.{field}: 视频不在 FLAYR_VALIDATION_ROOT 内")
            videos[role] = identity
        locked_samples.append({
            "id": sample_id,
            "product_category": str(sample.get("product_category") or ""),
            "target_market": str(sample.get("target_market") or ""),
            "gt_sha256": sha256_json(label),
            "evaluation_role": evaluation_role,
            "videos": videos,
        })
    input_errors = [
        error
        for error in errors
        if error not in {"missing_validation_root", "validation_root_missing"}
    ]
    if input_errors:
        raise ValueError("无法冻结 cohort：\n- " + "\n- ".join(input_errors))

    source_files, source_errors = _surface_identities(root, SOURCE_CONTRACT_FILES)
    surface_files, surface_hashes, surface_errors = _contract_surface_hashes(root)
    errors.extend(source_errors)
    errors.extend(surface_errors)
    try:
        labels_identity = _file_identity(labels_path)
        manifest_identity = _file_identity(manifest_path)
    except ValueError as exc:
        errors.append(str(exc))
        labels_identity = {}
        manifest_identity = {}
    worktree = _worktree_identity(root)
    commit = _git_value(root, "rev-parse", "HEAD")
    gt_registry = {"labels": labels_identity, "manifest": manifest_identity}
    gt_hash = sha256_json(gt_registry)
    video_identity_hash = sha256_json(_locked_video_payload(locked_samples))
    checks = {
        "source_commit": {
            "ok": bool(commit) and bool(worktree["clean"]),
            "commit": commit,
            "worktree_clean": worktree["clean"],
        },
        "prompt_hash": {
            "ok": len(surface_files.get("prompt", {})) == len(PROMPT_CONTRACT_FILES),
            "sha256": surface_hashes.get("prompt_sha256"),
        },
        "schema_hash": {
            "ok": len(surface_files.get("schema", {})) == len(SCHEMA_CONTRACT_FILES),
            "sha256": surface_hashes.get("schema_sha256"),
        },
        "evaluator_hash": {
            "ok": len(surface_files.get("evaluator", {})) == len(EVALUATOR_CONTRACT_FILES),
            "sha256": surface_hashes.get("evaluator_sha256"),
        },
        "gt_hash": {"ok": bool(labels_identity and manifest_identity), "sha256": gt_hash},
        "model_config_hash": {"ok": bool(execution_config.sha256), "sha256": execution_config.sha256},
        "video_identity": {
            "ok": bool(locked_samples)
            and all(
                isinstance(sample.get("videos"), dict)
                and set(sample["videos"]) == {"creator", "benchmark"}
                and all(isinstance(identity, dict) for identity in sample["videos"].values())
                for sample in locked_samples
            ),
            "sha256": video_identity_hash,
        },
        "validation_root": {
            "ok": validation_root is not None,
            "path": str(validation_root) if validation_root is not None else None,
            "path_sha256": sha256_json(str(validation_root)) if validation_root is not None else None,
        },
    }
    freeze_status = cohort_freeze_status(checks)
    if surface_errors or source_errors:
        freeze_status = cohort_freeze_status(checks, ["source_contract_surface"])
    if freeze_status["status"] != "READY":
        detail = "\n- " + "\n- ".join(errors) if errors else ""
        raise ValueError(format_freeze_blocked(freeze_status) + detail)
    return {
        "schema_version": LOCK_SCHEMA_VERSION,
        "status": "frozen",
        "created_at": utc_now(),
        "spent_at": None,
        "spent_reason": None,
        "code": {
            "repo_root": str(root.resolve()),
            "commit": commit,
            "worktree_clean": worktree["clean"],
            "worktree_status_sha256": worktree["status_sha256"],
            "worktree_diff_sha256": worktree["diff_sha256"],
            "untracked_files": worktree["untracked_files"],
            "worktree_fingerprint_sha256": worktree["fingerprint_sha256"],
        },
        # Keep the old key as a read-only compatibility alias for existing
        # promotion metadata consumers. The value is the complete manifest.
        "model_config": normalized_model_config,
        "model_execution_config": normalized_model_config,
        "model_execution_config_sha256": execution_config.sha256,
        "labels": labels_identity,
        "manifest": manifest_identity,
        "gt_registry": gt_registry,
        "contract_hashes": {
            **surface_hashes,
            "gt_sha256": gt_hash,
            "model_config_sha256": execution_config.sha256,
            "video_identity_sha256": video_identity_hash,
        },
        "source_contract_files": source_files,
        "freeze_contract_files": surface_files,
        "validation_root": checks["validation_root"],
        "evaluation_role": "blind_promotion",
        "cohort_freeze_status": freeze_status,
        "sample_ids": list(sample_ids),
        "samples": locked_samples,
    }


def verify_cohort_lock(lock: dict[str, Any]) -> list[str]:
    """校验冻结后的输入是否发生漂移。spent 合法，但不能再作为 blind 晋级依据。"""
    errors: list[str] = []
    schema_version = lock.get("schema_version")
    if schema_version not in {LEGACY_LOCK_SCHEMA_VERSION, LOCK_SCHEMA_VERSION}:
        errors.append("cohort lock schema_version 不兼容")
    if lock.get("status") not in LOCK_STATUSES:
        errors.append("cohort lock status 非法")
    is_current_lock = schema_version == LOCK_SCHEMA_VERSION
    if is_current_lock:
        errors.extend(_verify_v2_freeze_header(lock))
    code = lock.get("code") if isinstance(lock.get("code"), dict) else {}
    repo_root = Path(str(code.get("repo_root") or ""))
    if not repo_root.is_dir():
        errors.append("cohort lock 缺有效 code.repo_root")
    else:
        if _git_value(repo_root, "rev-parse", "HEAD") != code.get("commit"):
            errors.append("代码 commit 已漂移")
        current_worktree = _worktree_identity(repo_root)
        if current_worktree["fingerprint_sha256"] != code.get("worktree_fingerprint_sha256"):
            errors.append("代码工作树已漂移")
        if is_current_lock and code.get("worktree_clean") is not True:
            errors.append("cohort lock source worktree 必须 clean")
    loaded_documents: dict[str, dict[str, Any]] = {}
    for label in ("labels", "manifest"):
        identity = lock.get(label)
        if not isinstance(identity, dict):
            errors.append(f"缺 {label} identity")
            continue
        path = Path(str(identity.get("path") or ""))
        if not path.is_file() or sha256_file(path) != identity.get("sha256"):
            errors.append(f"{label} 已漂移或缺失")
            continue
        try:
            loaded_documents[label] = read_json(path)
        except (OSError, TypeError, ValueError) as exc:
            errors.append(f"{label} 无法读取：{exc}")
    source_contracts = lock.get("source_contract_files")
    if not isinstance(source_contracts, dict):
        errors.append("缺 source_contract_files")
    else:
        missing_contracts = sorted(set(SOURCE_CONTRACT_FILES) - set(source_contracts))
        if missing_contracts:
            errors.append("缺 source contract identity：" + ",".join(missing_contracts))
    for relative, identity in (source_contracts or {}).items():
        if not isinstance(identity, dict):
            errors.append(f"source contract identity 非法：{relative}")
            continue
        path = Path(str(identity.get("path") or ""))
        if not path.is_file() or sha256_file(path) != identity.get("sha256"):
            errors.append(f"source contract 已漂移或缺失：{relative}")
    label_samples = loaded_documents.get("labels", {}).get("samples")
    manifest_by_id = manifest_samples(loaded_documents.get("manifest", {}))
    verification_root, verification_root_error = _validation_root_from_env() if is_current_lock else (None, None)
    if is_current_lock:
        contract_hashes = lock.get("contract_hashes") if isinstance(lock.get("contract_hashes"), dict) else {}
        gt_registry = lock.get("gt_registry") if isinstance(lock.get("gt_registry"), dict) else {}
        if gt_registry.get("labels") != lock.get("labels") or gt_registry.get("manifest") != lock.get("manifest"):
            errors.append("gt_registry identity 与 labels/manifest 不一致")
        if contract_hashes.get("gt_sha256") != sha256_json({"labels": lock.get("labels"), "manifest": lock.get("manifest")}):
            errors.append("GT registry hash 已漂移")
    locked_samples = lock.get("samples")
    sample_ids = lock.get("sample_ids")
    if not isinstance(sample_ids, list) or any(not str(value).strip() for value in sample_ids):
        errors.append("cohort lock sample_ids 必须是非空字符串列表")
        sample_ids = []
    if len(sample_ids) != len(set(str(value) for value in sample_ids)):
        errors.append("cohort lock sample_ids 不能重复")
    if not isinstance(locked_samples, list):
        errors.append("cohort lock samples 必须是列表")
        locked_samples = []
    locked_ids = [str(sample.get("id") or "").strip() for sample in locked_samples if isinstance(sample, dict)]
    if locked_ids != [str(value).strip() for value in sample_ids]:
        errors.append("cohort lock sample_ids 与 samples 不一致")
    if not isinstance(label_samples, dict):
        errors.append("labels.samples 必须是对象")
        label_samples = {}
    for sample in locked_samples:
        if not isinstance(sample, dict):
            errors.append("cohort sample 非 object")
            continue
        sample_id = str(sample.get("id") or "").strip()
        label = label_samples.get(sample_id)
        manifest_sample = manifest_by_id.get(sample_id)
        if not isinstance(label, dict):
            errors.append(f"{sample_id}: locked GT 缺失")
        elif sample.get("gt_sha256") != sha256_json(label):
            errors.append(f"{sample_id}: locked GT 已漂移")
        if not isinstance(manifest_sample, dict):
            errors.append(f"{sample_id}: locked manifest sample 缺失")
        else:
            if manifest_sample.get("group") != "blind":
                errors.append(f"{sample_id}: manifest sample 必须属于 blind cohort")
            if str(manifest_sample.get("product_category") or "") != str(sample.get("product_category") or ""):
                errors.append(f"{sample_id}: product_category 已漂移")
            if str(manifest_sample.get("target_market") or "") != str(sample.get("target_market") or ""):
                errors.append(f"{sample_id}: target_market 已漂移")
        if isinstance(label, dict) and isinstance(manifest_sample, dict):
            errors.extend(
                validate_blind_sample_contract(
                    sample_id,
                    label,
                    manifest_sample,
                    require_canonical=is_current_lock,
                )
            )
            if is_current_lock:
                evaluation_role, role_errors = evaluation_role_for_sample(label, manifest_sample)
                errors.extend(f"{sample_id}: {error}" for error in role_errors)
                if evaluation_role != "blind_promotion" or sample.get("evaluation_role") != evaluation_role:
                    errors.append(f"{sample_id}: evaluation_role lock 不一致")
        locked_videos = sample.get("videos") if isinstance(sample.get("videos"), dict) else {}
        for role, field in (("creator", "creator_video"), ("benchmark", "benchmark_video")):
            identity = locked_videos.get(role)
            if not isinstance(identity, dict):
                errors.append(f"{sample_id}.{role} 缺视频 identity")
                continue
            path = Path(str((identity or {}).get("path") or ""))
            if not path.is_file() or sha256_file(path) != (identity or {}).get("sha256"):
                errors.append(f"{sample_id}.{role} 视频已漂移或缺失")
            if is_current_lock and verification_root is not None and not _path_is_within(path, verification_root):
                errors.append(f"{sample_id}.{role} 视频不在 FLAYR_VALIDATION_ROOT 内")
            if isinstance(manifest_sample, dict):
                try:
                    manifest_identity = _file_identity(resolve_manifest_video_path(manifest_sample.get(field)))
                except (OSError, TypeError, ValueError) as exc:
                    errors.append(f"{sample_id}.{field}: {exc}")
                else:
                    if manifest_identity != identity:
                        errors.append(f"{sample_id}.{role} lock 与 manifest 视频 identity 不一致")
    if is_current_lock:
        contract_hashes = lock.get("contract_hashes") if isinstance(lock.get("contract_hashes"), dict) else {}
        if contract_hashes.get("video_identity_sha256") != sha256_json(_locked_video_payload(locked_samples)):
            errors.append("video identity hash 已漂移")
    return errors


def spend_cohort_lock(lock: dict[str, Any], reason: str) -> dict[str, Any]:
    if lock.get("status") != "frozen":
        raise ValueError("只有 frozen cohort 可以标记为 spent")
    if not reason.strip():
        raise ValueError("spent reason 不能为空")
    updated = dict(lock)
    updated.update({"status": "spent", "spent_at": utc_now(), "spent_reason": reason.strip()})
    return updated
