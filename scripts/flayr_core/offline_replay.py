"""Offline derive replay and provenance helpers.

This module only reads a saved analysis artifact and runs deterministic
post-processing. It never constructs an LLM request or contacts a provider.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .analysis_model import CanonicalAnalysisResult
from .evidence_states import evidence_strength_gate_report
from .postprocess.derive import derive_severity_from_facts


OFFLINE_REPLAY_SCHEMA_VERSION = 2
MAX_REPLAY_INPUT_FILES = 1000
MAX_REPLAY_INPUT_DEPTH = 16
MAX_REPLAY_INPUT_FILE_BYTES = 64 * 1024 * 1024
MAX_REPLAY_INPUT_TOTAL_BYTES = 512 * 1024 * 1024
SIDECAR_ARTIFACT_NAMES = (
    "analysis_result.json",
    "llm_response.json",
    "raw_model_response.json",
    "validated_normalized_result.json",
    "final_derived_result.json",
    "postprocess_change_log.json",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_identity(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    return {
        "path": str(resolved),
        "size_bytes": resolved.stat().st_size,
        "sha256": sha256_file(resolved),
    }


def _git_commit(root: Path) -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=False
    )
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def _stage_id(stage: dict[str, Any]) -> str:
    return str(stage.get("stage") or "")[:2].upper()


def _stage_snapshot(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    snapshot: dict[str, dict[str, Any]] = {}
    for stage in result.get("stage_analysis") or []:
        if not isinstance(stage, dict):
            continue
        stage_id = _stage_id(stage)
        if not stage_id:
            continue
        trace = stage.get("severity_derivation") or {}
        constraints = trace.get("constraints") if isinstance(trace.get("constraints"), list) else []
        snapshot[stage_id] = {
            "model_severity": stage.get("model_severity"),
            "severity": stage.get("severity"),
            "derivation_status": trace.get("status"),
            "floor": trace.get("floor"),
            "ceiling": trace.get("ceiling"),
            "constraint_rules": sorted(
                str(item.get("rule") or "")
                for item in constraints
                if isinstance(item, dict) and str(item.get("rule") or "").strip()
            ),
            "constraint_count": len(constraints),
        }
    return snapshot


def _sidecar_identities(source_path: Path) -> dict[str, dict[str, Any]]:
    root = source_path.parent
    identities: dict[str, dict[str, Any]] = {}
    for name in SIDECAR_ARTIFACT_NAMES:
        candidate = root / name
        if candidate.is_file() and candidate != source_path:
            identities[name] = _file_identity(candidate)
    return identities


def _read_bounded_json(path: Path) -> tuple[dict[str, Any], int]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"replay input 不可读取：{path}") from exc
    if size > MAX_REPLAY_INPUT_FILE_BYTES:
        raise ValueError(
            f"replay input 超过单文件上限 {MAX_REPLAY_INPUT_FILE_BYTES} bytes：{path}"
        )
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_REPLAY_INPUT_FILE_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"replay input 不可读取：{path}") from exc
    if len(raw) > MAX_REPLAY_INPUT_FILE_BYTES:
        raise ValueError(
            f"replay input 超过单文件上限 {MAX_REPLAY_INPUT_FILE_BYTES} bytes：{path}"
        )
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"replay input 不是有效 JSON：{path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"replay input must be a JSON object: {path}")
    return value, len(raw)


def read_analysis_input(path: Path) -> dict[str, Any]:
    """Read one bounded analysis artifact for CLI and library callers."""
    value, _ = _read_bounded_json(path.expanduser().resolve())
    return value


def replay_canonical_finalization(
    canonical_source: dict[str, Any],
    analysis_context: dict[str, Any],
    analysis_input: str,
    *,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Replay the complete deterministic finalizer without provider access."""
    from .llm.pipeline import finalize_canonical_analysis_result

    canonical = CanonicalAnalysisResult.from_mapping(canonical_source)
    context = copy.deepcopy(analysis_context)
    if output_dir is not None:
        resolved_output = output_dir.expanduser().resolve()
        resolved_output.mkdir(parents=True, exist_ok=True)
        context["run_dir"] = str(resolved_output)
    pipeline_version = str(canonical_source.get("stage2_pipeline_version") or "").strip()
    if pipeline_version:
        context["stage2_pipeline_version"] = pipeline_version
    if pipeline_version == "segmented_stage_v1":
        context["stage_evidence_contract_required"] = True
    expected_hashes: dict[str, str] = {}
    understanding = canonical_source.get("video_understanding")
    if isinstance(understanding, dict):
        for role in ("benchmark", "creator"):
            side = understanding.get(role)
            digest = str(side.get("evidence_set_sha256") or "") if isinstance(side, dict) else ""
            if digest:
                expected_hashes[role] = digest
    return finalize_canonical_analysis_result(
        canonical,
        context,
        analysis_input,
        expected_stage1_hashes=expected_hashes,
    )


def _is_replay_derived_result(value: dict[str, Any]) -> bool:
    metadata = value.get("offline_derive_replay")
    return isinstance(metadata, dict) and metadata.get("mode") == "offline_derive_only"


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _severity_transitions(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    *,
    baseline: str,
) -> list[dict[str, Any]]:
    """Return severity deltas against one named baseline without inferring cause."""
    transitions: list[dict[str, Any]] = []
    for stage_id in sorted(set(before) | set(after)):
        previous = before.get(stage_id) or {}
        current = after.get(stage_id) or {}
        if baseline == "historical_final":
            baseline_severity = previous.get("severity")
            baseline_source = "persisted_final_severity"
        elif baseline == "model":
            baseline_severity = previous.get("model_severity")
            baseline_source = "persisted_model_severity"
            if _severity_rank(baseline_severity) < 0:
                baseline_severity = previous.get("severity")
                baseline_source = "historical_final_fallback"
        else:
            raise ValueError(f"unknown severity baseline: {baseline}")
        final_severity = current.get("severity")
        if _severity_rank(baseline_severity) < 0 or _severity_rank(final_severity) < 0:
            continue
        if baseline_severity == final_severity:
            continue
        transitions.append({
            "stage": stage_id,
            "baseline": baseline,
            "baseline_source": baseline_source,
            "before_severity": baseline_severity,
            "after_severity": final_severity,
            "after_derivation_status": current.get("derivation_status"),
            "constraint_rules": current.get("constraint_rules") or [],
        })
    return transitions


def _transition_summary(transitions: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "changed_stage_count": len(transitions),
        "severity_increases": sum(
            _severity_rank(item["after_severity"]) > _severity_rank(item["before_severity"])
            for item in transitions
        ),
        "severity_decreases": sum(
            _severity_rank(item["after_severity"]) < _severity_rank(item["before_severity"])
            for item in transitions
        ),
    }


def _resolver_effects(model_transitions: list[dict[str, Any]]) -> dict[str, Any]:
    """Attribute model-to-replay deltas only when one constraint rule caused them."""
    direct_rule_effects: dict[str, list[dict[str, Any]]] = {}
    ambiguous_rule_sets: list[dict[str, Any]] = []
    untraced_effects: list[dict[str, Any]] = []
    for item in model_transitions:
        rules = list(item.get("constraint_rules") or [])
        if len(rules) == 1:
            direct_rule_effects.setdefault(rules[0], []).append(item)
        elif rules:
            ambiguous_rule_sets.append(item)
        else:
            untraced_effects.append(item)
    return {
        "effective_stage_count": len(model_transitions),
        "direct_rule_effects": {
            rule: {
                "changed_stage_count": len(items),
                "severity_increases": sum(
                    _severity_rank(item["after_severity"]) > _severity_rank(item["before_severity"])
                    for item in items
                ),
                "severity_decreases": sum(
                    _severity_rank(item["after_severity"]) < _severity_rank(item["before_severity"])
                    for item in items
                ),
                "transitions": items,
            }
            for rule, items in sorted(direct_rule_effects.items())
        },
        "ambiguous_rule_set_effects": ambiguous_rule_sets,
        "untraced_effects": untraced_effects,
    }


def replay_derive_result(source_result: dict[str, Any], source_path: Path | None = None) -> dict[str, Any]:
    """Run derive deterministically and attach an auditable offline trace."""
    if _is_replay_derived_result(source_result):
        raise ValueError("replay input 已经是离线派生结果，不能再次作为原始输入")
    replayed = copy.deepcopy(source_result)
    before = _stage_snapshot(replayed)
    derive_severity_from_facts(replayed, replayed)
    after = _stage_snapshot(replayed)

    changes: list[dict[str, Any]] = []
    for stage_id in sorted(set(before) | set(after)):
        if before.get(stage_id) == after.get(stage_id):
            continue
        changes.append({"stage": stage_id, "before": before.get(stage_id), "after": after.get(stage_id)})

    source_identity = _file_identity(source_path) if source_path else None
    source_root = Path(__file__).resolve().parents[1]
    historical_final_transitions = _severity_transitions(before, after, baseline="historical_final")
    model_transitions = _severity_transitions(before, after, baseline="model")
    resolver_effects = _resolver_effects(model_transitions)
    replayed["offline_derive_replay"] = {
        "schema_version": OFFLINE_REPLAY_SCHEMA_VERSION,
        "mode": "offline_derive_only",
        "api_calls": 0,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "code_commit": _git_commit(source_root),
        "source": source_identity,
        "sidecar_artifacts": _sidecar_identities(source_path) if source_path else {},
        "evidence_strength": evidence_strength_gate_report(replayed),
        "summary": {
            "stage_count": len(after),
            "changed_stage_count": len(changes),
            "historical_final_to_replay": _transition_summary(historical_final_transitions),
            "model_to_replay": _transition_summary(model_transitions),
            "floor_applied": sum(
                1
                for item in after.values()
                if item.get("derivation_status") == "constrained" and item.get("floor") is not None
            ),
            "phase_c_candidates": sum(
                1
                for stage in replayed.get("stage_analysis") or []
                if isinstance(stage, dict)
                and (stage.get("severity_derivation") or {}).get("phase_c_candidate") is True
            ),
            "model_preserved": sum(
                1
                for item in after.values()
                if item.get("derivation_status") == "model_preserved"
            ),
        },
        "stage_changes": changes,
        "historical_final_transitions": historical_final_transitions,
        "model_transitions": model_transitions,
        "resolver_effects": resolver_effects,
    }
    return replayed


def _severity_rank(value: Any) -> int:
    return {"small": 0, "medium": 1, "large": 2}.get(str(value or ""), -1)


def discover_analysis_inputs(
    root: Path,
    *,
    exclude_root: Path | None = None,
) -> list[Path]:
    """Discover bounded original analysis artifacts without following symlinks."""
    resolved_root = root.expanduser().resolve()
    if not resolved_root.is_dir():
        raise NotADirectoryError(resolved_root)
    resolved_exclude = exclude_root.expanduser().resolve() if exclude_root else None
    if resolved_exclude and not _path_is_under(resolved_exclude, resolved_root):
        resolved_exclude = None
    paths: list[Path] = []
    total_bytes = 0
    candidate_count = 0
    for current, directories, filenames in os.walk(resolved_root, followlinks=False):
        current_path = Path(current).resolve()
        if resolved_exclude and _path_is_under(current_path, resolved_exclude):
            directories[:] = []
            continue
        relative_depth = len(current_path.relative_to(resolved_root).parts)
        if relative_depth > MAX_REPLAY_INPUT_DEPTH:
            raise ValueError(f"replay input 目录深度超过上限 {MAX_REPLAY_INPUT_DEPTH}：{current_path}")
        directories[:] = [
            name
            for name in directories
            if not (current_path / name).is_symlink()
        ]
        if "analysis.json" not in filenames:
            continue
        candidate = current_path / "analysis.json"
        if candidate.is_symlink() or not candidate.is_file():
            continue
        candidate_count += 1
        if candidate_count > MAX_REPLAY_INPUT_FILES:
            raise ValueError(f"replay 输入文件数超过上限 {MAX_REPLAY_INPUT_FILES}")
        value, size = _read_bounded_json(candidate)
        total_bytes += size
        if total_bytes > MAX_REPLAY_INPUT_TOTAL_BYTES:
            raise ValueError(
                f"replay 输入总大小超过上限 {MAX_REPLAY_INPUT_TOTAL_BYTES} bytes"
            )
        if _is_replay_derived_result(value):
            continue
        paths.append(candidate)
    return sorted(paths)


def _safe_sample_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip(" .")
    return normalized or "unnamed"


def _sample_id_for_source(source: Path) -> str:
    """Return a stable, path-safe ID that cannot collide across nested runs."""
    resolved = source.expanduser().resolve()
    sample_root: Path | None = None
    for parent in (resolved.parent, *resolved.parents):
        if parent.name.startswith("sample-") and len(parent.name) > len("sample-"):
            sample_root = parent
            break
    if sample_root is not None:
        sample_name = _safe_sample_component(sample_root.name.removeprefix("sample-"))
        relative_parent = resolved.parent.relative_to(sample_root)
        suffix = [
            _safe_sample_component(part)
            for part in relative_parent.parts
            if part not in {"", "."}
        ]
        return "__".join([sample_name, *suffix])

    path_digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
    return f"source-{path_digest}"


def replay_many(paths: Iterable[Path]) -> dict[str, Any]:
    """Replay multiple saved artifacts and return a report without writing files."""
    records: list[dict[str, Any]] = []
    seen_sample_ids: dict[str, Path] = {}
    total_bytes = 0
    for path in paths:
        source = path.expanduser().resolve()
        if len(records) >= MAX_REPLAY_INPUT_FILES:
            raise ValueError(f"replay 输入文件数超过上限 {MAX_REPLAY_INPUT_FILES}")
        value, size = _read_bounded_json(source)
        total_bytes += size
        if total_bytes > MAX_REPLAY_INPUT_TOTAL_BYTES:
            raise ValueError(
                f"replay 输入总大小超过上限 {MAX_REPLAY_INPUT_TOTAL_BYTES} bytes"
            )
        if _is_replay_derived_result(value):
            raise ValueError(f"replay input 是派生结果，不能再次消费：{source}")
        result = replay_derive_result(value, source)
        metadata = result["offline_derive_replay"]
        sample_id = _sample_id_for_source(source)
        previous_source = seen_sample_ids.get(sample_id)
        if previous_source is not None:
            raise ValueError(
                f"offline replay sample_id collision: {sample_id!r} maps to both "
                f"{previous_source} and {source}"
            )
        seen_sample_ids[sample_id] = source
        records.append({
            "sample_id": sample_id,
            "source": metadata["source"],
            "summary": metadata["summary"],
            "evidence_strength": metadata["evidence_strength"],
            "stage_changes": metadata["stage_changes"],
            "historical_final_transitions": metadata["historical_final_transitions"],
            "model_transitions": metadata["model_transitions"],
            "resolver_effects": metadata["resolver_effects"],
            "result": result,
        })
    direct_rule_effects: dict[str, dict[str, int]] = {}
    for record in records:
        for rule, effect in record["resolver_effects"]["direct_rule_effects"].items():
            aggregate = direct_rule_effects.setdefault(rule, {
                "changed_stage_count": 0,
                "severity_increases": 0,
                "severity_decreases": 0,
            })
            for key in aggregate:
                aggregate[key] += int(effect[key])
    return {
        "schema_version": OFFLINE_REPLAY_SCHEMA_VERSION,
        "mode": "offline_derive_only",
        "api_calls": 0,
        "records": records,
        "summary": {
            "inputs": len(records),
            "changed_stages": sum(record["summary"]["changed_stage_count"] for record in records),
            "historical_final_to_replay": {
                key: sum(record["summary"]["historical_final_to_replay"][key] for record in records)
                for key in ("changed_stage_count", "severity_increases", "severity_decreases")
            },
            "model_to_replay": {
                key: sum(record["summary"]["model_to_replay"][key] for record in records)
                for key in ("changed_stage_count", "severity_increases", "severity_decreases")
            },
            "direct_rule_effects": dict(sorted(direct_rule_effects.items())),
            "ambiguous_rule_set_effects": sum(
                len(record["resolver_effects"]["ambiguous_rule_set_effects"])
                for record in records
            ),
            "untraced_effects": sum(
                len(record["resolver_effects"]["untraced_effects"])
                for record in records
            ),
            "floor_applied": sum(record["summary"]["floor_applied"] for record in records),
            "phase_c_candidates": sum(record["summary"]["phase_c_candidates"] for record in records),
            "evidence_strength_ready": sum(
                record["evidence_strength"]["status"] == "ready" for record in records
            ),
        },
    }


__all__ = [
    "MAX_REPLAY_INPUT_DEPTH",
    "MAX_REPLAY_INPUT_FILES",
    "MAX_REPLAY_INPUT_FILE_BYTES",
    "MAX_REPLAY_INPUT_TOTAL_BYTES",
    "OFFLINE_REPLAY_SCHEMA_VERSION",
    "discover_analysis_inputs",
    "read_analysis_input",
    "replay_derive_result",
    "replay_many",
    "sha256_file",
]
