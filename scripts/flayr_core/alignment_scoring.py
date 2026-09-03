"""Pure offline scoring for human/model alignment artifacts.

This module does not read or write files and never calls a model.  It owns the
stage judgment and extraction scoring functions used by the alignment
evaluator, while the executable script remains responsible for artifact
loading, provenance checks, aggregation, and command-line output.
"""

from __future__ import annotations

import math
from typing import Any

from .artifacts import parse_time_range_seconds
from .stage_catalog import DEFAULT_STAGES


STAGE_CODES = tuple(stage.code for stage in DEFAULT_STAGES)
ROLE_NAMES = ("creator", "benchmark")
GT_GAPS = frozenset({"none", "small", "medium", "large", "uncertain"})
SCORABLE_GAPS = frozenset({"none", "small", "medium", "large"})
SCORABLE_RELATIONS = frozenset({"benchmark_better", "creator_better", "tie"})
QUALITY_FIELDS = ("subject", "visibility", "composition", "completion", "proof", "causal_link")
S3_QUALITY_FIELDS = ("subject", "visibility", "composition", "completion")
S4_QUALITY_FIELDS = ("visibility", "proof", "causal_link")


def _human_stage_status(label: dict[str, Any] | None) -> str:
    return str((label or {}).get("status") or "missing")


def _empty_denominator() -> dict[str, int]:
    return {
        "gt_cells": 0,
        "gt_labeled_cells": 0,
        "gt_not_applicable_cells": 0,
        "gt_uncertain_cells": 0,
        "gt_missing_cells": 0,
        "gt_invalid_cells": 0,
        "model_available_cells": 0,
        "model_failed_or_missing_cells": 0,
        "gt_scorable_gap_cells": 0,
        "gt_scorable_relation_cells": 0,
        "scored_gap_cells": 0,
        "scored_relation_cells": 0,
        "semantic_gap_cells": 0,
        "contract_representation_gap_cells": 0,
        "adjusted_gap_cells": 0,
        "adjusted_gap_correct_cells": 0,
        "adjusted_relation_cells": 0,
        "adjusted_relation_correct_cells": 0,
        "fact_sufficient_unavailable_gap_cells": 0,
        "fact_sufficient_unavailable_relation_cells": 0,
        "prediction_unavailable_gap_cells": 0,
        "prediction_unavailable_relation_cells": 0,
        "gt_relation_missing_cells": 0,
        "gt_relation_uncertain_cells": 0,
        "gt_relation_invalid_cells": 0,
        "gt_relation_gap_conflict_cells": 0,
    }


def _stage_predictions(result: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(result, dict):
        return {}
    rows = result.get("stage_judgments")
    if not isinstance(rows, list):
        return {}
    predictions: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_stage = str(row.get("stage") or "").strip().upper()
        stage_code = raw_stage[:2] if raw_stage[:2] in STAGE_CODES else ""
        if not stage_code:
            continue
        gap = row.get("gap_magnitude")
        if gap is None:
            # v1 artifacts are intentionally read as legacy, severity-only
            # predictions. They cannot represent human ``none`` correctly.
            gap = row.get("severity")
        relation = row.get("relation")
        if isinstance(relation, str):
            relation = relation.strip().lower()
            if relation in {"equivalent", "matched"}:
                relation = "tie"
        predictions[stage_code] = {
            "gap_magnitude": str(gap).strip().lower() if isinstance(gap, str) else None,
            "relation": relation if isinstance(relation, str) else None,
            "confidence": row.get("confidence"),
            "legacy_severity_only": "gap_magnitude" not in row,
        }
    return predictions


def score_judgment(
    result: dict[str, Any] | None,
    labels: dict[str, dict[str, Any]],
    *,
    artifact_status: str,
    fact_sufficiency: dict[str, bool | None] | None = None,
) -> dict[str, Any]:
    """Score direction and magnitude independently with explicit exclusions."""
    denominator = _empty_denominator()
    rows: list[dict[str, Any]] = []
    predictions = _stage_predictions(result)
    for stage_code in STAGE_CODES:
        denominator["gt_cells"] += 1
        label = labels.get(stage_code) or {}
        status = _human_stage_status(label)
        gap = label.get("gap_magnitude")
        relation = label.get("relation")
        prediction = predictions.get(stage_code)
        fact_sufficient = (fact_sufficiency or {}).get(stage_code) is True
        if status == "not_applicable":
            denominator["gt_not_applicable_cells"] += 1
        elif status == "uncertain" or gap in {"uncertain", "unknown"}:
            denominator["gt_uncertain_cells"] += 1
        elif status == "missing":
            denominator["gt_missing_cells"] += 1
        elif status == "invalid" or gap not in GT_GAPS:
            denominator["gt_invalid_cells"] += 1
        else:
            relation_gap_conflict = (
                gap in SCORABLE_GAPS
                and relation in SCORABLE_RELATIONS
                and not _relation_gap_compatible(relation, gap)
            )
            if relation_gap_conflict:
                denominator["gt_invalid_cells"] += 1
                denominator["gt_relation_gap_conflict_cells"] += 1
                rows.append(
                    {
                        "stage": stage_code,
                        "status": "invalid",
                        "gt_gap_magnitude": gap,
                        "gt_relation": relation,
                        "prediction": prediction,
                        "error_class": "gt_relation_gap_conflict",
                    }
                )
                continue
            denominator["gt_labeled_cells"] += 1
            if gap in SCORABLE_GAPS:
                denominator["gt_scorable_gap_cells"] += 1
            if relation in SCORABLE_RELATIONS:
                denominator["gt_scorable_relation_cells"] += 1
            elif relation == "uncertain":
                denominator["gt_relation_uncertain_cells"] += 1
            elif relation is None:
                denominator["gt_relation_missing_cells"] += 1
            else:
                denominator["gt_relation_invalid_cells"] += 1

        if status != "labeled" or gap not in GT_GAPS:
            row_status = "invalid" if status == "labeled" and gap not in GT_GAPS else status
            rows.append(
                {
                    "stage": stage_code,
                    "status": row_status,
                    "gt_gap_magnitude": gap,
                    "gt_relation": relation,
                    "prediction": prediction,
                    "error_class": "gt_invalid" if row_status == "invalid" else "gt_not_scored",
                }
            )
            continue
        if artifact_status != "completed":
            denominator["model_failed_or_missing_cells"] += 1
            rows.append(
                {
                    "stage": stage_code,
                    "status": status,
                    "gt_gap_magnitude": gap,
                    "gt_relation": relation,
                    "prediction": prediction,
                    "error_class": "model_failed_or_missing",
                }
            )
            continue
        if prediction is None:
            if gap in SCORABLE_GAPS:
                denominator["prediction_unavailable_gap_cells"] += 1
                if fact_sufficient:
                    denominator["adjusted_gap_cells"] += 1
                    denominator["fact_sufficient_unavailable_gap_cells"] += 1
            if relation in SCORABLE_RELATIONS:
                denominator["prediction_unavailable_relation_cells"] += 1
                if fact_sufficient:
                    denominator["adjusted_relation_cells"] += 1
                    denominator["fact_sufficient_unavailable_relation_cells"] += 1
            rows.append(
                {
                    "stage": stage_code,
                    "status": status,
                    "gt_gap_magnitude": gap,
                    "gt_relation": relation,
                    "prediction": None,
                    "predicted_gap_available": False,
                    "predicted_relation_available": False,
                    "gt_gap_scorable": gap in SCORABLE_GAPS,
                    "contract_representation_gap": False,
                    "semantic_gap_comparable": False,
                    "fact_sufficient": fact_sufficient,
                    "adjusted_gap_eligible": gap in SCORABLE_GAPS and fact_sufficient,
                    "adjusted_relation_eligible": relation in SCORABLE_RELATIONS and fact_sufficient,
                    "gap_correct": None,
                    "relation_correct": None,
                    "error_class": "prediction_unavailable",
                }
            )
            continue
        denominator["model_available_cells"] += 1
        predicted_gap = prediction.get("gap_magnitude")
        predicted_relation = prediction.get("relation")
        gt_gap_available = gap in SCORABLE_GAPS
        gap_available = predicted_gap in SCORABLE_GAPS
        relation_available = (
            relation not in SCORABLE_RELATIONS or predicted_relation in SCORABLE_RELATIONS
        )
        if predicted_gap in SCORABLE_GAPS and gap in SCORABLE_GAPS:
            denominator["scored_gap_cells"] += 1
        representation_gap = bool(prediction.get("legacy_severity_only") and gap == "none")
        semantic_gap_comparable = bool(
            gt_gap_available and gap_available and not representation_gap
        )
        if semantic_gap_comparable:
            denominator["semantic_gap_cells"] += 1
        if representation_gap:
            denominator["contract_representation_gap_cells"] += 1
        if gt_gap_available and not gap_available:
            denominator["prediction_unavailable_gap_cells"] += 1
        gap_correct = predicted_gap == gap if gt_gap_available and gap_available else None
        if relation in SCORABLE_RELATIONS:
            if predicted_relation in SCORABLE_RELATIONS:
                denominator["scored_relation_cells"] += 1
                relation_correct = predicted_relation == relation
            else:
                denominator["prediction_unavailable_relation_cells"] += 1
                relation_correct = None
        else:
            relation_correct = None
        adjusted_gap_eligible = bool(
            gt_gap_available and not representation_gap and (gap_available or fact_sufficient)
        )
        adjusted_relation_eligible = bool(
            relation in SCORABLE_RELATIONS and (predicted_relation in SCORABLE_RELATIONS or fact_sufficient)
        )
        if gt_gap_available and not gap_available and fact_sufficient:
            denominator["fact_sufficient_unavailable_gap_cells"] += 1
        if relation in SCORABLE_RELATIONS and predicted_relation not in SCORABLE_RELATIONS and fact_sufficient:
            denominator["fact_sufficient_unavailable_relation_cells"] += 1
        if adjusted_gap_eligible:
            denominator["adjusted_gap_cells"] += 1
            denominator["adjusted_gap_correct_cells"] += int(gap_correct is True)
        if adjusted_relation_eligible:
            denominator["adjusted_relation_cells"] += 1
            denominator["adjusted_relation_correct_cells"] += int(relation_correct is True)
        if representation_gap:
            error_class = "contract_representation_gap"
        elif (gt_gap_available and not gap_available) or not relation_available:
            error_class = "prediction_unavailable"
        elif relation in SCORABLE_RELATIONS and not relation_correct and not gap_correct:
            error_class = "direction_and_magnitude_error"
        elif relation in SCORABLE_RELATIONS and not relation_correct:
            error_class = "direction_error"
        elif not gap_correct:
            error_class = "magnitude_error"
        else:
            error_class = "aligned"
        rows.append(
            {
                "stage": stage_code,
                "status": status,
                "gt_gap_magnitude": gap,
                "gt_relation": relation,
                "predicted_gap_magnitude": predicted_gap,
                "predicted_relation": predicted_relation,
                "confidence": prediction.get("confidence"),
                "predicted_gap_available": gap_available,
                "predicted_relation_available": relation_available,
                "gt_gap_scorable": gt_gap_available,
                "contract_representation_gap": representation_gap,
                "semantic_gap_comparable": semantic_gap_comparable,
                "fact_sufficient": fact_sufficient,
                "adjusted_gap_eligible": adjusted_gap_eligible,
                "adjusted_relation_eligible": adjusted_relation_eligible,
                "gap_correct": gap_correct,
                "relation_correct": relation_correct,
                "error_class": error_class,
            }
        )
    scored_gap = [
        row
        for row in rows
        if row.get("status") == "labeled"
        and row.get("gt_gap_magnitude") in SCORABLE_GAPS
        and row.get("predicted_gap_magnitude") in SCORABLE_GAPS
    ]
    scored_relation = [row for row in rows if row.get("relation_correct") is not None]
    exact_rows = [
        row
        for row in scored_gap
        if row.get("relation_correct") is not None and row.get("semantic_gap_comparable")
    ]
    return {
        "artifact_status": artifact_status,
        "denominator": denominator,
        "metrics": {
            # ``gap_accuracy`` is deliberately the semantic metric. Legacy
            # severity-only rows that cannot express GT=none are excluded
            # from it and exposed separately below.
            "gap_accuracy": (
                sum(row.get("gap_correct") is True for row in scored_gap if row.get("semantic_gap_comparable"))
                / sum(row.get("semantic_gap_comparable") is True for row in scored_gap)
                if any(row.get("semantic_gap_comparable") for row in scored_gap)
                else None
            ),
            "contract_aware_gap_accuracy": (
                sum(row.get("gap_correct") is True for row in scored_gap) / len(scored_gap)
                if scored_gap
                else None
            ),
            "semantic_gap_accuracy_excluding_representation_gap": (
                sum(row.get("gap_correct") is True for row in scored_gap if row.get("semantic_gap_comparable"))
                / sum(row.get("semantic_gap_comparable") is True for row in scored_gap)
                if any(row.get("semantic_gap_comparable") for row in scored_gap)
                else None
            ),
            "contract_representation_gap_rate": (
                sum(row.get("contract_representation_gap") is True for row in rows)
                / sum(row.get("status") == "labeled" for row in rows)
                if any(row.get("status") == "labeled" for row in rows)
                else None
            ),
            "gap_coverage": (
                denominator["scored_gap_cells"] / denominator["gt_scorable_gap_cells"]
                if denominator["gt_scorable_gap_cells"]
                else None
            ),
            "relation_coverage": (
                denominator["scored_relation_cells"] / denominator["gt_scorable_relation_cells"]
                if denominator["gt_scorable_relation_cells"]
                else None
            ),
            "adjusted_gap_accuracy": (
                denominator["adjusted_gap_correct_cells"] / denominator["adjusted_gap_cells"]
                if denominator["adjusted_gap_cells"]
                else None
            ),
            "adjusted_relation_accuracy": (
                denominator["adjusted_relation_correct_cells"] / denominator["adjusted_relation_cells"]
                if denominator["adjusted_relation_cells"]
                else None
            ),
            "relation_accuracy": (
                sum(row.get("relation_correct") is True for row in scored_relation) / len(scored_relation)
                if scored_relation
                else None
            ),
            "exact_direction_and_gap_accuracy": (
                sum(row.get("gap_correct") is True and row.get("relation_correct") is True for row in exact_rows)
                / len(exact_rows)
                if exact_rows
                else None
            ),
            "error_class_counts": {
                error_class: sum(row.get("error_class") == error_class for row in rows)
                for error_class in sorted({str(row.get("error_class")) for row in rows})
            },
        },
        "rows": rows,
    }


def _relation_gap_compatible(relation: str, gap: str) -> bool:
    """Mirror the frozen GT invariant without importing validator internals."""
    if relation == "uncertain" or gap == "uncertain":
        return True
    if gap == "none":
        return relation == "tie"
    if relation == "tie":
        return False
    return relation in {"creator_better", "benchmark_better"}


def _overlaps(left: Any, right: Any) -> bool:
    left_range = parse_time_range_seconds(left, None)
    right_range = parse_time_range_seconds(right, None)
    if left_range is None or right_range is None:
        return False
    return min(left_range[1], right_range[1]) > max(left_range[0], right_range[0])


def _validate_key_event(event: dict[str, Any]) -> list[str]:
    """Validate only the fields needed to put a GT event in a denominator."""
    errors: list[str] = []
    role = str(event.get("role") or "").strip().lower()
    if role not in ROLE_NAMES:
        errors.append("invalid_role")
    stage = str(event.get("stage") or "").strip().upper()
    if stage not in STAGE_CODES:
        errors.append("invalid_stage")
    time_range = event.get("time_range")
    if not isinstance(time_range, (list, tuple)) or len(time_range) != 2:
        errors.append("invalid_time_range")
    else:
        try:
            start, end = float(time_range[0]), float(time_range[1])
        except (TypeError, ValueError):
            errors.append("invalid_time_range")
        else:
            if not all(map(math.isfinite, (start, end))) or start < 0 or end <= start:
                errors.append("invalid_time_range")
    expected_state = str(event.get("expected_state") or "present").strip().lower()
    if expected_state not in {"present", "absent"}:
        errors.append("invalid_expected_state")
    if expected_state == "absent":
        terms = event.get("terms_any")
        if not isinstance(terms, list) or not any(str(term).strip() for term in terms):
            errors.append("absent_event_missing_terms_any")
    return errors


def _event_matches_unit(
    event: dict[str, Any],
    unit: dict[str, Any],
    *,
    source_duration_seconds: float | None = None,
) -> bool:
    stage = str(event.get("stage") or "").strip().upper()
    if stage not in STAGE_CODES:
        return False
    functions = {
        str(function).strip().upper().split("_", 1)[0]
        for function in (unit.get("functions") or [])
        if isinstance(function, str)
    }
    unit_range = parse_time_range_seconds(unit.get("time_range"), source_duration_seconds)
    event_range = parse_time_range_seconds(event.get("time_range"), None)
    return bool(stage in functions and unit_range is not None and event_range is not None and _overlaps(unit_range, event_range))


def _event_terms_match_unit(event: dict[str, Any], unit: dict[str, Any]) -> bool:
    """Apply an absent-event forbidden-term guard to a candidate unit.

    terms_any is a semantic hint for negative checks: a same-stage unit in the
    same time window is not automatically a false positive when it is about a
    different fact. Legacy events without terms keep broad overlap behavior.
    """
    terms = event.get("terms_any")
    if not isinstance(terms, list):
        return True
    normalized_terms = [str(term).strip().casefold() for term in terms if str(term).strip()]
    if not normalized_terms:
        return True
    searchable = str(unit.get("information") or "").casefold()
    return any(term in searchable for term in normalized_terms)


def _quality_counts(units: list[dict[str, Any]], fields: tuple[str, ...]) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for field in fields:
        counts: dict[str, int] = {}
        for unit in units:
            quality = unit.get("fact_quality") if isinstance(unit.get("fact_quality"), dict) else {}
            value = str(quality.get(field) or "missing")
            counts[value] = counts.get(value, 0) + 1
        result[field] = dict(sorted(counts.items()))
    return result


def score_extraction(
    result: dict[str, Any] | None,
    sample: dict[str, Any],
    *,
    artifact_status: str,
    source_durations: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Score key-event coverage and expose S3/S4 evidence-quality signals."""
    events = sample.get("key_events") if isinstance(sample.get("key_events"), list) else []
    units_by_role = {
        role: result.get(f"{role}_evidence_units", []) if isinstance(result, dict) else []
        for role in ROLE_NAMES
    }
    units_by_role = {
        role: [unit for unit in units if isinstance(unit, dict)] if isinstance(units, list) else []
        for role, units in units_by_role.items()
    }
    matched_event_indexes: set[int] = set()
    matched_unit_keys: set[tuple[str, int]] = set()
    absent_false_positive_unit_keys: set[tuple[str, int]] = set()
    event_rows: list[dict[str, Any]] = []
    invalid_event_count = 0
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            invalid_event_count += 1
            event_rows.append(
                {
                    "event_index": index,
                    "event_id": None,
                    "role": None,
                    "stage": None,
                    "expected_state": None,
                    "evidence_found": None,
                    "matched": None,
                    "valid": False,
                    "invalid_reasons": ["event_not_object"],
                    "matching_unit_ids": [],
                }
            )
            continue
        invalid_reasons = _validate_key_event(event)
        if invalid_reasons:
            invalid_event_count += 1
            event_rows.append(
                {
                    "event_index": index,
                    "event_id": event.get("id"),
                    "role": event.get("role"),
                    "stage": event.get("stage"),
                    "expected_state": event.get("expected_state") or "present",
                    "evidence_found": None,
                    "matched": None,
                    "valid": False,
                    "invalid_reasons": invalid_reasons,
                    "matching_unit_ids": [],
                }
            )
            continue
        role = str(event.get("role") or "").strip().lower()
        candidates = units_by_role.get(role, [])
        matching = [
            (unit_index, unit)
            for unit_index, unit in enumerate(candidates)
            if _event_matches_unit(
                event,
                unit,
                source_duration_seconds=(source_durations or {}).get(role),
            )
        ]
        expected_state = str(event.get("expected_state") or "present").strip().lower()
        if expected_state == "absent":
            matching = [
                (unit_index, unit)
                for unit_index, unit in matching
                if _event_terms_match_unit(event, unit)
            ]
        evidence_found = bool(matching)
        # A present event is recalled when evidence exists. An absent event is
        # satisfied only when no model unit claims the forbidden event.
        matched = evidence_found if expected_state == "present" else not evidence_found
        if expected_state == "present" and evidence_found:
            matched_event_indexes.add(index)
            unit_index, _ = matching[0]
            matched_unit_keys.add((role, unit_index))
        elif expected_state == "absent":
            absent_false_positive_unit_keys.update((role, unit_index) for unit_index, _ in matching)
        event_rows.append(
            {
                "event_index": index,
                "event_id": event.get("id"),
                "role": role,
                "stage": str(event.get("stage") or ""),
                "expected_state": expected_state,
                "evidence_found": evidence_found,
                "matched": matched,
                "valid": True,
                "invalid_reasons": [],
                "matching_unit_ids": [unit.get("id") for _, unit in matching],
            }
        )
    valid_units = [
        (role, index, unit)
        for role, units in units_by_role.items()
        for index, unit in enumerate(units)
        if parse_time_range_seconds(unit.get("time_range"), (source_durations or {}).get(role)) is not None
    ]
    precision_denominator = len(valid_units)
    precision_numerator = len(matched_unit_keys)
    present_event_rows = [
        row for row in event_rows if row.get("valid") is True and row.get("expected_state") == "present"
    ]
    absent_event_rows = [
        row for row in event_rows if row.get("valid") is True and row.get("expected_state") == "absent"
    ]
    recall_denominator = len(present_event_rows)
    recall_numerator = len(matched_event_indexes)
    absence_denominator = len(absent_event_rows)
    absence_respected = sum(row.get("matched") is True for row in absent_event_rows)
    artifact_completed = artifact_status == "completed"
    stage_metrics: dict[str, Any] = {}
    for stage_code in STAGE_CODES:
        stage_events = [
            row
            for row in present_event_rows
            if str(row.get("stage") or "").upper().startswith(stage_code)
        ]
        stage_absence_events = [
            row
            for row in absent_event_rows
            if str(row.get("stage") or "").upper().startswith(stage_code)
        ]
        stage_units = [
            unit
            for role, units in units_by_role.items()
            for unit in units
            if stage_code in {
                str(function).strip().upper().split("_", 1)[0]
                for function in (unit.get("functions") or [])
                if isinstance(function, str)
            }
        ]
        quality_fields = (
            S3_QUALITY_FIELDS
            if stage_code == "S3"
            else S4_QUALITY_FIELDS
            if stage_code == "S4"
            else QUALITY_FIELDS
        )
        stage_metrics[stage_code] = {
            "required_event_count": len(stage_events),
            "scored_event_count": len(stage_events) if artifact_completed else 0,
            "matched_event_count": sum(row["matched"] for row in stage_events) if artifact_completed else 0,
            "absence_check_count": len(stage_absence_events) if artifact_completed else 0,
            "absence_respected_count": (
                sum(row["matched"] for row in stage_absence_events) if artifact_completed else 0
            ),
            "recall": (
                sum(row["matched"] for row in stage_events) / len(stage_events)
                if stage_events and artifact_completed
                else None
            ),
            "unit_count": len(stage_units),
            "quality_coverage": (
                sum(isinstance(unit.get("fact_quality"), dict) for unit in stage_units) / len(stage_units)
                if stage_units
                else 0.0
            ),
            "quality_counts": _quality_counts(
                stage_units,
                quality_fields,
            ),
            "quality_fields": list(quality_fields),
        }
    return {
        "artifact_status": artifact_status,
        "matching_method": "role + stage function + positive time-range overlap; semantic truth requires human review",
        "denominator": {
            "required_key_events": len(events),
            "present_key_events": recall_denominator,
            "invalid_key_events": invalid_event_count,
            "absence_checks": absence_denominator if artifact_completed else 0,
            "absence_respected": absence_respected if artifact_completed else 0,
            "absence_false_positive_units": (
                len(absent_false_positive_unit_keys) if artifact_completed else 0
            ),
            "scored_key_events": recall_denominator if artifact_completed else 0,
            "matched_key_events": recall_numerator if artifact_completed else 0,
            "valid_model_units": precision_denominator if artifact_completed else 0,
            "model_units_matching_key_events": precision_numerator if artifact_completed else 0,
            "model_failure_or_missing": int(artifact_status != "completed"),
        },
        "metrics": {
            "temporal_stage_recall_proxy": (
                recall_numerator / recall_denominator
                if recall_denominator and artifact_completed
                else None
            ),
            # Without a human key-event set there is no reference against
            # which model units can be classified as extra or matched.
            "temporal_stage_precision_proxy": (
                precision_numerator / precision_denominator
                if recall_denominator and precision_denominator and artifact_completed
                else None
            ),
            "absence_respected_rate": (
                absence_respected / absence_denominator
                if absence_denominator and artifact_completed
                else None
            ),
            "fact_quality_coverage": (
                sum(isinstance(unit.get("fact_quality"), dict) for units in units_by_role.values() for unit in units)
                / sum(len(units) for units in units_by_role.values())
                if sum(len(units) for units in units_by_role.values())
                else 0.0
            ),
        },
        "stage_metrics": stage_metrics,
        "key_event_rows": event_rows,
    }


__all__ = [
    "GT_GAPS",
    "QUALITY_FIELDS",
    "ROLE_NAMES",
    "S3_QUALITY_FIELDS",
    "S4_QUALITY_FIELDS",
    "SCORABLE_GAPS",
    "SCORABLE_RELATIONS",
    "STAGE_CODES",
    "_empty_denominator",
    "_event_matches_unit",
    "_event_terms_match_unit",
    "_human_stage_status",
    "_overlaps",
    "_quality_counts",
    "_relation_gap_compatible",
    "_stage_predictions",
    "_validate_key_event",
    "score_extraction",
    "score_judgment",
]
