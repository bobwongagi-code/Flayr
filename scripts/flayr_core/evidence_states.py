"""Shared contracts for evidence completion states and derive audit reasons.

The state values are semantic facts emitted by the structured analysis layer.
Post-processing may validate their consistency, but it must not infer or rewrite
them from neighboring booleans.
"""

from __future__ import annotations

from typing import Any, Iterable


S3_USAGE_EVIDENCE_STATES = ("none", "partial", "complete", "uncertain")
S4_EFFECT_EVIDENCE_STATES = ("none", "result_only", "verified", "uncertain")
EVIDENCE_STATE_STRENGTHS = ("direct", "explicit", "inferred", "absent")

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
    "S3_USAGE_EVIDENCE_STATES",
    "S4_EFFECT_EVIDENCE_STATES",
    "normalize_evidence_state",
    "normalize_reason_code",
]
