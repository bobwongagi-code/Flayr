"""Offline audits for the structured S4/S5 evaluation chain.

These helpers deliberately do not change production severity.  They expose
the intermediate facts needed to tell extraction failures, state-contract
failures, and judgment failures apart when replaying saved artifacts.
"""

from __future__ import annotations

from typing import Any

from .evidence_states import (
    S4_EFFECT_EVIDENCE_STATES,
    S5_TRUST_STATES,
    evidence_strength_gate_report,
    normalize_reason_code,
)
from .stage_evidence_contracts import (
    STAGE_EVIDENCE_CONTRACT_VERSION,
    qualified_stage_evidence_ids,
    stage_codes,
    stage_evidence_check_map,
)


ROLE_NAMES = ("creator", "benchmark")
_S5_CREDIBLE_BASES = {
    "authority",
    "traceable_data",
    "independent_user",
    "social_consensus",
    "process_transparency",
}


def _is_bool(value: Any) -> bool:
    return value is True or value is False


def audit_s4_flag(flag: Any) -> dict[str, Any]:
    """Check S4 state against the hard facts emitted with that state.

    This is intentionally mechanical.  It does not decide whether a video
    *should* have been labelled ``verified``; it only detects impossible or
    incomplete combinations that must not silently reach a resolver.
    """

    if not isinstance(flag, dict):
        return {
            "status": "missing",
            "state": None,
            "reason_code": normalize_reason_code("missing_field"),
            "errors": ["flag_missing"],
        }

    state = str(flag.get("effect_evidence_state") or "").strip().lower() or None
    errors: list[str] = []
    if state not in S4_EFFECT_EVIDENCE_STATES:
        errors.append("state_missing_or_invalid")

    required_bools = (
        "effect_visible",
        "effect_attribution_supported",
        "process_linked_effect",
        "result_only_without_process",
    )
    missing_bools = [name for name in required_bools if not _is_bool(flag.get(name))]
    if missing_bools:
        errors.append("hard_fact_missing:" + ",".join(missing_bools))

    if state == "none":
        if flag.get("effect_visible") is True:
            errors.append("none_with_visible_effect")
        if str(flag.get("effect_type") or "").strip() not in {"", "none"}:
            errors.append("none_with_effect_type")
    elif state == "result_only":
        if flag.get("result_only_without_process") is not True:
            errors.append("result_only_without_marker")
        if flag.get("process_linked_effect") is True and flag.get("effect_attribution_supported") is True:
            errors.append("result_only_with_complete_causal_facts")
    elif state == "verified":
        for name in ("effect_visible", "effect_attribution_supported", "process_linked_effect"):
            if flag.get(name) is not True:
                errors.append("verified_without_" + name)
        if flag.get("result_only_without_process") is True:
            errors.append("verified_marked_result_only")

    evidence_ids = flag.get("evidence_ids")
    if state == "none" and evidence_ids:
        errors.append("none_with_evidence_ids")
    if state in {"verified", "result_only"} and not evidence_ids:
        errors.append("evidence_ids_missing_for_state")
    if evidence_ids is not None and (
        not isinstance(evidence_ids, list)
        or any(not isinstance(item, str) or not item.strip() for item in evidence_ids)
    ):
        errors.append("evidence_ids_invalid")

    return {
        "status": "consistent" if not errors else "state_conflict",
        "state": state,
        "reason_code": normalize_reason_code(
            "state_hard_fact_conflict" if errors else "audit_consistent"
        ),
        "errors": errors,
        "evidence_ids": list(evidence_ids) if isinstance(evidence_ids, list) else [],
    }


def classify_s5_trust_state(flag: Any) -> dict[str, Any]:
    """Classify S5 source semantics without treating missing as absence."""

    if not isinstance(flag, dict):
        return {"state": "uncertain", "reason_code": normalize_reason_code("missing_field")}

    basis = str(flag.get("trust_basis") or "").strip().lower()
    trust_type = str(flag.get("trust_evidence_type") or "").strip().lower()
    exists = flag.get("exists")
    source_visible = flag.get("trust_source_visible")
    source_credible = flag.get("trust_source_credible")
    source_ids = flag.get("trust_source_evidence_ids")
    has_source_ids = isinstance(source_ids, list) and any(
        isinstance(item, str) and item.strip() for item in source_ids
    )

    if basis in {"product_claim", "offer_or_spec"}:
        return {
            "state": "product_claim_or_offer",
            "reason_code": normalize_reason_code("s5_product_claim_or_offer"),
            "trust_basis": basis,
        }

    if basis == "none":
        contradictory_source_fields = (
            trust_type not in {"", "none", "unknown"}
            or source_visible is True
            or source_credible is True
            or has_source_ids
        )
        if (
            not contradictory_source_fields
            and exists is False
            and source_visible is False
            and source_credible is False
        ):
            return {
                "state": "explicit_absence",
                "reason_code": normalize_reason_code("s5_explicit_absence"),
                "trust_basis": basis,
            }
        return {
            "state": "uncertain",
            "reason_code": normalize_reason_code("s5_absence_not_explicit"),
            "trust_basis": basis,
        }

    if basis in _S5_CREDIBLE_BASES:
        if exists is not False and source_visible is True and source_credible is True and has_source_ids:
            return {
                "state": "credible_source",
                "reason_code": normalize_reason_code("s5_credible_source"),
                "trust_basis": basis,
            }
        return {
            "state": "uncertain",
            "reason_code": normalize_reason_code("s5_source_not_verified"),
            "trust_basis": basis,
        }

    if exists is False and trust_type == "none" and source_visible is False and source_credible is False:
        return {
            "state": "explicit_absence",
            "reason_code": normalize_reason_code("s5_explicit_absence"),
        }

    return {
        "state": "uncertain",
        "reason_code": normalize_reason_code("s5_missing_or_unknown_source_fields"),
    }


def _unit_map(result: dict[str, Any], role: str) -> dict[str, dict[str, Any]]:
    understanding = result.get("video_understanding")
    side = understanding.get(role) if isinstance(understanding, dict) else None
    units = side.get("evidence_units") if isinstance(side, dict) else None
    return {
        str(unit.get("id")): unit
        for unit in units or []
        if isinstance(unit, dict) and str(unit.get("id") or "").strip()
    }


def _stage_referenced_ids(result: dict[str, Any], stage_code: str, role: str) -> set[str]:
    suffix = {"S1": "hook", "S2": "s2", "S3": "s3", "S4": "s4", "S5": "s5", "S6": "s6"}.get(stage_code)
    for stage in result.get("stage_analysis") or []:
        if not isinstance(stage, dict) or not str(stage.get("stage") or "").upper().startswith(stage_code):
            continue
        references = {
            str(value)
            for value in stage.get(f"{role}_evidence_ids") or []
            if str(value).strip()
        }
        flag = stage.get(f"{role}_{suffix}") if suffix else None
        if isinstance(flag, dict):
            references.update(
                str(value)
                for value in flag.get("evidence_ids") or []
                if str(value).strip()
            )
        return references
    return set()


def _audit_stage_evidence_ids(
    result: dict[str, Any],
    role: str,
    stage_code: str,
    evidence_ids: list[str] | set[str],
) -> list[str]:
    """Audit one stage's references against the active Stage1 qualification.

    Legacy artifacts still use the descriptive ``functions`` projection.  New
    artifacts must use the canonical stage check and the strength on the
    referenced locked units; no downstream repair or audit may infer ownership
    from a free-form compatibility field.
    """
    units = _unit_map(result, role)
    side = (result.get("video_understanding") or {}).get(role, {})
    active_contract = isinstance(side, dict) and side.get("stage_evidence_contract_version") == STAGE_EVIDENCE_CONTRACT_VERSION
    check = stage_evidence_check_map(side).get(stage_code) if active_contract else None
    qualified = qualified_stage_evidence_ids(side, stage_code) if active_contract else set()
    errors: list[str] = []
    for evidence_id in evidence_ids:
        evidence_id = str(evidence_id)
        unit = units.get(evidence_id)
        if unit is None:
            errors.append(f"unknown_evidence_id:{evidence_id}")
            continue
        if active_contract:
            if not isinstance(check, dict) or check.get("status") in {"unknown", "conflict"}:
                errors.append(f"stage_evidence_unresolved:{stage_code}:{evidence_id}")
            elif evidence_id not in qualified:
                errors.append(f"stage_evidence_not_qualified:{stage_code}:{evidence_id}")
            continue
        functions = {
            str(function).strip().upper().split("_", 1)[0]
            for function in unit.get("functions", [])
            if isinstance(function, str)
        }
        if stage_code not in functions:
            errors.append(f"evidence_temporal_mismatch:{evidence_id}")
    return errors


def _audit_s4_evidence_ids(result: dict[str, Any], role: str, flag_audit: dict[str, Any]) -> list[str]:
    return _audit_stage_evidence_ids(result, role, "S4", flag_audit.get("evidence_ids", []))


def audit_analysis_chain(result: Any) -> dict[str, Any]:
    """Return a complete offline S4/S5/evidence-strength audit for one run."""

    if not isinstance(result, dict):
        return {
            "schema_version": 1,
            "status": "invalid_result",
            "errors": ["result_must_be_object"],
        }

    stages = result.get("stage_analysis") if isinstance(result.get("stage_analysis"), list) else []
    s4 = next((item for item in stages if isinstance(item, dict) and str(item.get("stage", "")).upper().startswith("S4")), None)
    s5 = next((item for item in stages if isinstance(item, dict) and str(item.get("stage", "")).upper().startswith("S5")), None)

    s4_roles: dict[str, Any] = {}
    for role in ROLE_NAMES:
        flag = s4.get(f"{role}_s4") if isinstance(s4, dict) else None
        flag_audit = audit_s4_flag(flag)
        evidence_errors = _audit_s4_evidence_ids(result, role, flag_audit)
        s4_roles[role] = {
            **flag_audit,
            "evidence_errors": evidence_errors,
            "status": "consistent" if flag_audit["status"] == "consistent" and not evidence_errors else (
                "missing" if flag_audit["status"] == "missing" else "state_conflict"
            ),
        }

    s5_roles: dict[str, Any] = {}
    for role in ROLE_NAMES:
        flag = s5.get(f"{role}_s5") if isinstance(s5, dict) else None
        s5_roles[role] = classify_s5_trust_state(flag)

    strength = evidence_strength_gate_report(result)
    s4_errors = sum(len(item.get("evidence_errors", [])) + len(item.get("errors", [])) for item in s4_roles.values())
    stage_evidence_roles: dict[str, dict[str, Any]] = {}
    for stage_code in stage_codes():
        stage_evidence_roles[stage_code] = {}
        for role in ROLE_NAMES:
            references = sorted(_stage_referenced_ids(result, stage_code, role))
            errors = _audit_stage_evidence_ids(result, role, stage_code, references)
            stage_evidence_roles[stage_code][role] = {
                "referenced_evidence_ids": references,
                "errors": errors,
                "status": "ok" if not errors else "invalid",
            }
    stage_evidence_error_count = sum(
        len(role_data.get("errors", []))
        for stage_data in stage_evidence_roles.values()
        for role_data in stage_data.values()
    )
    return {
        "schema_version": 1,
        "status": "ok" if s4 is not None and s5 is not None else "incomplete",
        "evidence_strength": strength,
        "s4": {
            "model_severity": s4.get("model_severity", s4.get("severity")) if isinstance(s4, dict) else None,
            "final_severity": s4.get("severity") if isinstance(s4, dict) else None,
            "roles": s4_roles,
            "state_conflict_count": sum(item["status"] == "state_conflict" for item in s4_roles.values()),
            "evidence_error_count": s4_errors,
            "severity_derivation": s4.get("severity_derivation") if isinstance(s4, dict) else None,
        },
        "s5": {
            "model_severity": s5.get("model_severity", s5.get("severity")) if isinstance(s5, dict) else None,
            "final_severity": s5.get("severity") if isinstance(s5, dict) else None,
            "roles": s5_roles,
        },
        "stage_evidence": {
            "contract_version": STAGE_EVIDENCE_CONTRACT_VERSION,
            "roles": stage_evidence_roles,
            "error_count": stage_evidence_error_count,
        },
        "summary": {
            "s4_state_conflict": s4_errors > 0 or any(item["status"] == "state_conflict" for item in s4_roles.values()),
            "s5_uncertain_roles": sum(item.get("state") == "uncertain" for item in s5_roles.values()),
            "evidence_strength_gate": strength.get("status"),
            "stage_evidence_error_count": stage_evidence_error_count,
        },
    }


__all__ = [
    "ROLE_NAMES",
    "S5_TRUST_STATES",
    "audit_analysis_chain",
    "audit_s4_flag",
    "classify_s5_trust_state",
]
