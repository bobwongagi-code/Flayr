"""Shared contracts for evidence completion states and derive audit reasons.

The state values are semantic facts emitted by the structured analysis layer.
Post-processing may validate their consistency, but it must not infer or rewrite
them from neighboring booleans.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


S3_USAGE_EVIDENCE_STATES = ("none", "partial", "complete", "uncertain")
S4_EFFECT_EVIDENCE_STATES = ("none", "result_only", "verified", "uncertain")
EVIDENCE_STATE_STRENGTHS = ("direct", "explicit", "inferred", "absent")

# These are the exact facts consumed by severity-increasing derive rules. Keep
# them centralized so repair markers and derive cannot silently disagree about
# which inputs were checked.
S1_HOOK_FLOOR_FIELDS = ("exists", "landing_met", "anchors_proposition")
S2_HARD_FACT_FIELDS = (
    "exists",
    "merged_with_s3",
    "handoff_met",
    "product_identity_clear",
    "product_role_clear",
)
S3_HARD_FACT_FIELDS = (
    "exists",
    "usage_process_visible",
    "real_usage_met",
    "core_selling_point_visible",
    "action_proof_met",
    "action_target_contact_met",
    "action_application_change_visible",
    "critical_action_continuity_met",
    "result_only_without_process",
    "mouth_only_or_static",
    "fake_or_staged",
)
S4_HARD_FACT_FIELDS = (
    "effect_visible",
    "effect_proposition_matched",
    "visual_difference_observed",
    "module_constraints_met",
    "effect_attribution_supported",
    "process_linked_effect",
    "result_only_without_process",
    "requires_close_inspection",
    "tamper_or_cut_risk",
)


def hard_fact_fingerprint(value: Any, fields: Iterable[str]) -> str:
    """Hash only the hard facts a repair marker claims to have checked.

    ``value`` may be one flag or a role-to-flag mapping. Missing values remain
    explicit ``None`` values in the snapshot so a marker cannot be reused
    after a field is added, removed, or changed.
    """
    field_names = tuple(str(field) for field in fields)
    if isinstance(value, dict) and value and all(isinstance(item, dict) for item in value.values()):
        snapshot = {
            str(role): {field: value[role].get(field) for field in field_names}
            for role in sorted(value)
        }
    elif isinstance(value, dict):
        snapshot = {field: value.get(field) for field in field_names}
    else:
        snapshot = {field: None for field in field_names}
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def s2_hard_fact_snapshot(flag: Any) -> dict[str, Any]:
    """Return the S2 facts after applying the canonical compatibility value."""
    if not isinstance(flag, dict):
        return {}
    snapshot = dict(flag)
    computed = flag.get("computed_s1_s2_compatible")
    if computed in {True, False}:
        snapshot["s1_s2_compatible"] = computed
    return snapshot

# Keep this list closed. These values are written into constraint evaluations
# and are consumed by audit/reporting code downstream.
AUDIT_REASON_CODES = frozenset(
    {
        "constraint_applied",
        "constraint_conflict",
        "predicate_not_met",
        "missing_field",
        "evidence_state_missing",
        "hard_fact_missing",
        "uncertain_fact",
        "insufficient_strength",
        "evidence_strength_missing",
        "evidence_strength_inferred",
        "evidence_strength_absent",
        "evidence_strength_invalid",
        "precondition_missing",
        "repair_incomplete",
        "state_hard_fact_conflict",
        "creator_usage_partial",
        "benchmark_usage_partial",
        "creator_usage_uncertain",
        "benchmark_usage_uncertain",
        "creator_effect_result_only",
        "benchmark_effect_result_only",
        "creator_effect_uncertain",
        "benchmark_effect_uncertain",
        "s3_creator_none_benchmark_complete",
        "s4_creator_none_benchmark_verified",
        "activation_gate_closed",
        "model_preserved",
        "rule_error",
    }
)


def normalize_evidence_state(value: Any, allowed: Iterable[str]) -> str | None:
    """Return a canonical state, or ``None`` for missing/invalid input."""
    state = str(value or "").strip().lower()
    return state if state in set(allowed) else None


def normalize_reason_code(value: Any, fallback: str = "rule_error") -> str:
    """Keep audit reason codes inside the closed taxonomy."""
    code = str(value or "").strip().lower()
    return code if code in AUDIT_REASON_CODES else fallback


__all__ = [
    "AUDIT_REASON_CODES",
    "EVIDENCE_STATE_STRENGTHS",
    "S1_HOOK_FLOOR_FIELDS",
    "S2_HARD_FACT_FIELDS",
    "S3_HARD_FACT_FIELDS",
    "S4_HARD_FACT_FIELDS",
    "S3_USAGE_EVIDENCE_STATES",
    "S4_EFFECT_EVIDENCE_STATES",
    "hard_fact_fingerprint",
    "s2_hard_fact_snapshot",
    "normalize_evidence_state",
    "normalize_reason_code",
]
