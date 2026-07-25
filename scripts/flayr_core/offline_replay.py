"""Offline derive replay and provenance helpers.

This module only reads a saved analysis artifact and runs deterministic
post-processing. It never constructs an LLM request or contacts a provider.
"""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .evidence_states import evidence_strength_gate_report
from .postprocess.derive import derive_severity_from_facts


OFFLINE_REPLAY_SCHEMA_VERSION = 2
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


def replay_many(paths: Iterable[Path]) -> dict[str, Any]:
    """Replay multiple saved artifacts and return a report without writing files."""
    records: list[dict[str, Any]] = []
    for path in paths:
        source = path.expanduser().resolve()
        value = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"replay input must be a JSON object: {source}")
        result = replay_derive_result(value, source)
        metadata = result["offline_derive_replay"]
        records.append({
            "sample_id": source.parent.name.removeprefix("sample-"),
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


__all__ = ["OFFLINE_REPLAY_SCHEMA_VERSION", "replay_derive_result", "replay_many", "sha256_file"]
