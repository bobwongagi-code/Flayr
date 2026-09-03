"""Stage1 qualification response validation and binding normalization."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from ..stage_evidence_contracts import (
    STAGE_EVIDENCE_CONTRACT_VERSION,
    STAGE1_QUALIFICATION_GROUPS,
    normalize_stage_code,
    normalize_stage_evidence_checks,
    stage1_forbidden_field_issues,
    stage1_pipeline_owned_field_issues,
    stage_evidence_contract,
    stage_codes,
)
from ..stage_ownership import (
    certification_policy_violations,
    is_certification_owner_stage,
)
from .artifact_identity import stable_sha256


@dataclass
class Stage1QualificationPlan:
    """Pure per-run qualification state prepared before provider calls."""

    requested_stages: set[str]
    groups_to_run: list[list[str]]
    focused_requalification: bool
    provider_phase: str
    phase_label: str
    valid_ids: set[str]
    checks_by_stage: dict[str, dict[str, Any]]
    group_records: list[dict[str, Any]]
    prior_failed_stage_codes: set[str]
    failed_stage_codes: list[str]


def build_stage1_qualification_plan(
    facts: dict[str, Any],
    target_stages: list[str] | tuple[str, ...] | None = None,
    *,
    include_existing_state: bool = True,
) -> Stage1QualificationPlan:
    """Prepare qualification targets and optional existing state without providers."""
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
    if not include_existing_state:
        return Stage1QualificationPlan(
            requested_stages=requested_stages,
            groups_to_run=groups_to_run,
            focused_requalification=focused_requalification,
            provider_phase=provider_phase,
            phase_label=phase_label,
            valid_ids=valid_ids,
            checks_by_stage={},
            group_records=[],
            prior_failed_stage_codes=set(),
            failed_stage_codes=[],
        )
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
    group_records = [
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
    failed_stage_codes = [
        code
        for code in prior_failed_stage_codes
        if code not in requested_stages
    ] if focused_requalification else []
    return Stage1QualificationPlan(
        requested_stages=requested_stages,
        groups_to_run=groups_to_run,
        focused_requalification=focused_requalification,
        provider_phase=provider_phase,
        phase_label=phase_label,
        valid_ids=valid_ids,
        checks_by_stage=checks_by_stage,
        group_records=group_records,
        prior_failed_stage_codes=prior_failed_stage_codes,
        failed_stage_codes=failed_stage_codes,
    )


def prepare_stage1_qualification_group(
    response: Any,
    *,
    targets: list[str],
    valid_ids: set[str],
    phase_label: str,
    prepare_response: Any = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None, set[str]]:
    """Validate and normalize one provider response before pipeline bookkeeping."""
    prepare = prepare_response or _prepare_stage1_qualification_response
    _, normalized_by_stage, qualification_normalization = prepare(
        response,
        targets=targets,
        valid_ids=valid_ids,
        phase_label=phase_label,
    )
    blocked_stage_codes = {
        code
        for item in qualification_normalization.get("blocked_stages") or []
        if isinstance(item, dict)
        and (code := normalize_stage_code(item.get("stage"))) is not None
    } if qualification_normalization is not None else set()
    return normalized_by_stage, qualification_normalization, blocked_stage_codes


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


STAGE1_QUALIFICATION_BINDING_ERROR_CODE = (
    "nested_binding_id_outside_top_level_stage_evidence_ids"
)


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
    valid_statuses = {"present", "partial", "absent", "unknown", "conflict", "not_applicable"}
    valid_coverages = {"complete", "partial", "unknown"}
    for item in raw_checks:
        if not isinstance(item, dict):
            raise ValueError(f"{phase_label} qualification 的 stage check 必须是对象。")
        code = normalize_stage_code(item.get("stage"))
        if code is None:
            raise ValueError(f"{phase_label} qualification 返回了无效阶段。")
        if not is_certification_owner_stage(code) and certification_policy_violations(
            item, allow_stage1_disclaimer=True
        ):
            raise ValueError(
                f"{phase_label} qualification 的 {code} 不得将第三方认证作为资格信号或阶段字段；"
                "认证主张只能归入 S5"
            )
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


def _normalize_stage1_qualification_bindings(
    response: Any,
    *,
    phase_label: str,
) -> tuple[Any, dict[str, Any] | None]:
    """Normalize nested references against each stage's immutable ID list.

    The provider response is never mutated. The consumption copy only removes
    nested IDs absent from the same stage's top-level list; a supported binding
    emptied by that removal blocks that stage with a code-owned unknown check.
    """
    normalized = copy.deepcopy(response)
    checks = normalized.get("stage_evidence_checks") if isinstance(normalized, dict) else None
    if not isinstance(checks, list):
        return normalized, None
    removed_refs: list[dict[str, Any]] = []
    blocked_stages: list[dict[str, str]] = []
    for index, item in enumerate(checks):
        if not isinstance(item, dict):
            continue
        top_level_ids = item.get("evidence_ids")
        bindings = item.get("signal_bindings")
        if not isinstance(top_level_ids, list) or not isinstance(bindings, dict):
            continue
        allowed_ids = set(top_level_ids)
        stage = normalize_stage_code(item.get("stage")) or str(item.get("stage") or "")
        blocked = False
        for signal, binding in bindings.items():
            if not isinstance(binding, dict) or not isinstance(binding.get("evidence_ids"), list):
                continue
            original_ids = binding["evidence_ids"]
            outside_ids = [value for value in original_ids if value not in allowed_ids]
            if not outside_ids:
                continue
            normalized_ids = [value for value in original_ids if value in allowed_ids]
            removed_refs.append(
                {
                    "stage": stage,
                    "signal": str(signal),
                    "removed_evidence_ids": outside_ids,
                }
            )
            if (
                str(binding.get("status") or "").strip().lower() == "supported"
                and not normalized_ids
            ):
                blocked = True
                continue
            binding["evidence_ids"] = normalized_ids
        if blocked:
            blocked_stages.append(
                {
                    "stage": stage,
                    "reason": (
                        f"{phase_label} {stage} supported binding lost all evidence_ids "
                        "after normalization; 资格保持未知。"
                    ),
                }
            )
            normalized["stage_evidence_checks"][index] = _unknown_stage_qualification_check(
                stage,
                blocked_stages[-1]["reason"],
            )
    if not removed_refs and not blocked_stages:
        return normalized, None
    return normalized, {
        "reason_code": STAGE1_QUALIFICATION_BINDING_ERROR_CODE,
        "removed_refs": removed_refs,
        "blocked_stages": blocked_stages,
        "raw_response_sha256": stable_sha256(response),
    }


def _prepare_stage1_qualification_response(
    response: Any,
    *,
    targets: list[str],
    valid_ids: set[str],
    phase_label: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, Any] | None]:
    """Validate raw JSON, normalize references, then validate the copy."""
    _validated_stage1_qualification_response(
        response,
        targets=targets,
        valid_ids=valid_ids,
        phase_label=phase_label,
    )
    normalized, record = _normalize_stage1_qualification_bindings(
        response,
        phase_label=phase_label,
    )
    normalized_by_stage = _validated_stage1_qualification_response(
        normalized,
        targets=targets,
        valid_ids=valid_ids,
        phase_label=phase_label,
    )
    if not isinstance(normalized, dict):  # pragma: no cover - validator guards this
        raise ValueError(f"{phase_label} normalized qualification must be an object")
    return normalized, normalized_by_stage, record
