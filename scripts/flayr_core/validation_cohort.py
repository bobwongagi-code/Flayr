"""验证 cohort 冻结与 GT 契约。

本模块不调用模型。它用内容哈希锁定 blind 批次，避免同一批样本在看过结果并
修改规则后仍被当作泛化验收集。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LOCK_SCHEMA_VERSION = 1
LOCK_STATUSES = {"frozen", "spent"}
EXECUTION_VALUES = {0.0, 0.5, 1.0, 2.0}
RELATIONS = {"creator_better", "matched", "benchmark_better"}
CONFIDENCE_VALUES = {"low", "medium", "high"}
STAGES = tuple(f"S{index}" for index in range(1, 7))
SOURCE_CONTRACT_FILES = (
    "structure_library_full.md",
    "QA-RULES.md",
    "references/analysis-output-schema.json",
    "references/brand_propositions.json",
    "references/commercial-judgement-framework.md",
    "references/observation-guide.md",
    "scripts/flayr_core/llm/payload.py",
    "scripts/flayr_core/llm/parse.py",
    "scripts/flayr_core/postprocess/derive.py",
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


def validate_blind_sample_contract(
    sample_id: str,
    label: dict[str, Any],
    sample: dict[str, Any] | None,
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

    stages = label.get("stages") if isinstance(label.get("stages"), dict) else {}
    oracles = label.get("stage_oracles") if isinstance(label.get("stage_oracles"), dict) else {}
    events = label.get("key_events") if isinstance(label.get("key_events"), list) else []
    event_ids = [str(event.get("id") or "") for event in events if isinstance(event, dict)]
    if len(event_ids) != len(set(event_ids)) or any(not value for value in event_ids):
        errors.append(f"{sample_id}: key_events id 不能为空或重复")
    for index, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            errors.append(f"{sample_id}: key_events[{index}] 必须是 object")
            continue
        if event.get("role") not in {"creator", "benchmark"} or event.get("stage") not in STAGES:
            errors.append(f"{sample_id}: key_events[{index}] 缺有效 role/stage")
        time_range = event.get("time_range")
        if not isinstance(time_range, list) or len(time_range) != 2:
            errors.append(f"{sample_id}: key_events[{index}].time_range 必须是 [start,end]")
        expected_state = str(event.get("expected_state") or "present")
        if expected_state not in {"present", "absent"}:
            errors.append(f"{sample_id}: key_events[{index}].expected_state 非法")
        if expected_state == "absent" and not event.get("terms_any"):
            errors.append(f"{sample_id}: key_events[{index}] 缺失事件必须提供 terms_any")

    for stage in STAGES:
        severity = str(stages.get(stage) or "").lower()
        if severity not in {"small", "medium", "large", "na"}:
            errors.append(f"{sample_id}: {stage} 缺有效 severity")
            continue
        if severity == "na":
            continue
        oracle = oracles.get(stage)
        if not isinstance(oracle, dict):
            errors.append(f"{sample_id}: {stage} 缺 stage_oracles")
            continue
        for role in ("creator", "benchmark"):
            value = oracle.get(f"{role}_execution")
            if not isinstance(value, (int, float)) or float(value) not in EXECUTION_VALUES:
                errors.append(f"{sample_id}: {stage}.{role}_execution 必须是 0/0.5/1/2")
        if oracle.get("relation") not in RELATIONS:
            errors.append(f"{sample_id}: {stage}.relation 非法")
        if oracle.get("confidence") not in CONFIDENCE_VALUES:
            errors.append(f"{sample_id}: {stage}.confidence 非法")
        if not str(oracle.get("reason") or "").strip():
            errors.append(f"{sample_id}: {stage}.reason 不能为空")
        decision_ids = oracle.get("decision_event_ids")
        if not isinstance(decision_ids, list) or not decision_ids:
            errors.append(f"{sample_id}: {stage}.decision_event_ids 必须是非空数组")
        else:
            unknown = sorted(set(str(value) for value in decision_ids) - set(event_ids))
            if unknown:
                errors.append(f"{sample_id}: {stage} 引用未知 key_event：{','.join(unknown)}")

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
    if not str(model_config.get("model") or "").strip() or not str(model_config.get("api_url") or "").strip():
        raise ValueError("model_config 必须包含 model 和 api_url")
    if not isinstance(model_config.get("temperature"), (int, float)):
        raise ValueError("model_config.temperature 必须是数字")
    labels = read_json(labels_path)
    manifest = read_json(manifest_path)
    label_samples = labels.get("samples") if isinstance(labels.get("samples"), dict) else {}
    inputs = manifest_samples(manifest)
    errors: list[str] = []
    locked_samples: list[dict[str, Any]] = []
    selected_hashes: set[str] = set()

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
            videos[role] = identity
        locked_samples.append({
            "id": sample_id,
            "product_category": str(sample.get("product_category") or ""),
            "target_market": str(sample.get("target_market") or ""),
            "gt_sha256": sha256_json(label),
            "videos": videos,
        })
    if errors:
        raise ValueError("无法冻结 cohort：\n- " + "\n- ".join(errors))

    source_files = {
        relative: _file_identity(root / relative)
        for relative in SOURCE_CONTRACT_FILES
    }
    worktree = _worktree_identity(root)
    return {
        "schema_version": LOCK_SCHEMA_VERSION,
        "status": "frozen",
        "created_at": utc_now(),
        "spent_at": None,
        "spent_reason": None,
        "code": {
            "repo_root": str(root.resolve()),
            "commit": _git_value(root, "rev-parse", "HEAD"),
            "worktree_clean": worktree["clean"],
            "worktree_status_sha256": worktree["status_sha256"],
            "worktree_diff_sha256": worktree["diff_sha256"],
            "untracked_files": worktree["untracked_files"],
            "worktree_fingerprint_sha256": worktree["fingerprint_sha256"],
        },
        "model_config": model_config,
        "labels": _file_identity(labels_path),
        "manifest": _file_identity(manifest_path),
        "source_contract_files": source_files,
        "sample_ids": list(sample_ids),
        "samples": locked_samples,
    }


def verify_cohort_lock(lock: dict[str, Any]) -> list[str]:
    """校验冻结后的输入是否发生漂移。spent 合法，但不能再作为 blind 晋级依据。"""
    errors: list[str] = []
    if lock.get("schema_version") != LOCK_SCHEMA_VERSION:
        errors.append("cohort lock schema_version 不兼容")
    if lock.get("status") not in LOCK_STATUSES:
        errors.append("cohort lock status 非法")
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
    for label in ("labels", "manifest"):
        identity = lock.get(label)
        if not isinstance(identity, dict):
            errors.append(f"缺 {label} identity")
            continue
        path = Path(str(identity.get("path") or ""))
        if not path.is_file() or sha256_file(path) != identity.get("sha256"):
            errors.append(f"{label} 已漂移或缺失")
    for relative, identity in (lock.get("source_contract_files") or {}).items():
        if not isinstance(identity, dict):
            errors.append(f"source contract identity 非法：{relative}")
            continue
        path = Path(str(identity.get("path") or ""))
        if not path.is_file() or sha256_file(path) != identity.get("sha256"):
            errors.append(f"source contract 已漂移或缺失：{relative}")
    for sample in lock.get("samples") or []:
        if not isinstance(sample, dict):
            errors.append("cohort sample 非 object")
            continue
        for role, identity in (sample.get("videos") or {}).items():
            path = Path(str((identity or {}).get("path") or ""))
            if not path.is_file() or sha256_file(path) != (identity or {}).get("sha256"):
                errors.append(f"{sample.get('id')}.{role} 视频已漂移或缺失")
    return errors


def spend_cohort_lock(lock: dict[str, Any], reason: str) -> dict[str, Any]:
    if lock.get("status") != "frozen":
        raise ValueError("只有 frozen cohort 可以标记为 spent")
    if not reason.strip():
        raise ValueError("spent reason 不能为空")
    updated = dict(lock)
    updated.update({"status": "spent", "spent_at": utc_now(), "spent_reason": reason.strip()})
    return updated
