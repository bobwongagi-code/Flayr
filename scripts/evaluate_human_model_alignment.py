#!/usr/bin/env python3
"""Score saved model artifacts against human labels without making API calls.

This evaluator is deliberately outside the model request path. It keeps the
two layers separate:

* extraction is compared with human ``key_events`` using a stage-and-time
  overlap proxy, never with generated evidence IDs;
* judgment is compared with human gap magnitude and, when present, direction;
* unavailable, not-applicable, uncertain, and failed cells remain explicit in
  the denominator metadata.

The extraction precision metric is named ``temporal_stage_precision_proxy`` on
purpose. A human must still verify semantic truth; stage/time overlap alone
cannot prove that the text describes the right visual fact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.artifacts import parse_time_range_seconds  # noqa: E402
from flayr_core.llm.compact_eval import (  # noqa: E402
    load_gt_stage_labels,
)
from flayr_core.report_metadata import current_code_commit  # noqa: E402
from flayr_core.stage_catalog import DEFAULT_STAGES  # noqa: E402
from flayr_core.utils import write_json  # noqa: E402


STAGE_CODES = tuple(stage.code for stage in DEFAULT_STAGES)
ROLE_NAMES = ("creator", "benchmark")
ALIGNMENT_SCHEMA_VERSION = 2
ALIGNMENT_PROTOCOL = "human_model_alignment_v2"
GT_GAPS = frozenset({"none", "small", "medium", "large", "uncertain"})
SCORABLE_GAPS = frozenset({"none", "small", "medium", "large"})
SCORABLE_RELATIONS = frozenset({"benchmark_better", "creator_better", "tie"})
QUALITY_FIELDS = ("subject", "visibility", "composition", "completion", "proof", "causal_link")
S3_QUALITY_FIELDS = ("subject", "visibility", "composition", "completion")
S4_QUALITY_FIELDS = ("visibility", "proof", "causal_link")

ALIGNMENT_METRIC_DEFINITIONS = {
    "gap_accuracy": "语义差距准确率；排除 legacy severity-only 无法表达 GT=none 的合同表达缺口",
    "contract_aware_gap_accuracy": "合同感知差距准确率；将 legacy severity-only 对 GT=none 的表达缺口计为不可表达错误",
    "contract_representation_gap_rate": "GT 有效格中，模型旧 severity-only 合同无法表达 none 的比例",
    "relation_accuracy": "仅在 GT relation 与模型 relation 均可解析时计算的方向准确率",
    "exact_direction_and_gap_accuracy": "方向和差距大小同时正确的准确率",
    "gt_large_recall": "GT=large 且模型产物可用于语义比较的格中，模型正确识别 large 的比例",
    "temporal_stage_recall_proxy": "人工 key_events 与模型 evidence_units 按角色、阶段和时间重叠匹配的召回代理",
    "temporal_stage_precision_proxy": "模型有效 evidence_units 中与人工 key_events 匹配的精确率代理；不代表语义真实",
}


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value).strip())
    return cleaned.strip("._") or "unnamed"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sample_ids(gt_path: Path, manifest_path: Path | None) -> list[str]:
    if manifest_path is not None:
        data = _read_json(manifest_path)
        rows = data.get("samples") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise ValueError("manifest must contain a samples list")
        sample_ids = [str(row.get("sample_id") or "").strip() for row in rows if isinstance(row, dict)]
        if not sample_ids or any(not item for item in sample_ids):
            raise ValueError("manifest samples must contain non-empty sample_id values")
        if len(set(sample_ids)) != len(sample_ids):
            raise ValueError("manifest contains duplicate sample_id values")
        return sample_ids
    data = _read_json(gt_path)
    samples = data.get("samples") if isinstance(data, dict) else None
    if not isinstance(samples, dict) or not samples:
        raise ValueError("GT must contain a non-empty samples object")
    return sorted(
        str(sample_id)
        for sample_id, sample in samples.items()
        if isinstance(sample, dict)
        and (isinstance(sample.get("stages"), dict) or isinstance(sample.get("human_gap"), dict))
    )


def _artifact_source_durations(record: dict[str, Any]) -> dict[str, float]:
    roles = record.get("video_role_order")
    durations = record.get("video_source_duration_seconds")
    if not isinstance(roles, list) or not isinstance(durations, list) or len(roles) != len(durations):
        return {}
    result: dict[str, float] = {}
    for role, raw_duration in zip(roles, durations):
        try:
            duration = float(raw_duration)
        except (TypeError, ValueError):
            continue
        if role in ROLE_NAMES and duration > 0:
            result[str(role)] = duration
    return result


def _read_result_artifact(path: Path, result_key: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if not path.is_file():
        return None, {"status": "missing", "artifact": str(path)}
    try:
        record = _read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        return None, {"status": "invalid_artifact", "artifact": str(path), "error": str(exc)[:500]}
    if not isinstance(record, dict):
        return None, {"status": "invalid_artifact", "artifact": str(path), "error": "root is not an object"}
    result = record.get(result_key)
    if record.get("status") != "completed" or not isinstance(result, dict):
        return None, {
            "status": str(record.get("status") or "invalid"),
            "artifact": str(path),
            "failure_class": record.get("failure_class"),
            "contract_error_codes": record.get("contract_error_codes", []),
            "error": str(record.get("error") or record.get("errors") or "no completed result")[:1000],
        }
    return result, {
        "status": "completed",
        "artifact": str(path),
        "artifact_sha256": _sha256(path),
        "schema_version": record.get("schema_version"),
        "source_commit": record.get("source_commit"),
        "source_digest": record.get("source_digest"),
        "source_durations": _artifact_source_durations(record),
        "failure_class": None,
    }


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
        "prediction_unavailable_gap_cells": 0,
        "prediction_unavailable_relation_cells": 0,
        "gt_relation_missing_cells": 0,
        "gt_relation_uncertain_cells": 0,
        "gt_relation_invalid_cells": 0,
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
        predictions[stage_code] = {
            "gap_magnitude": str(gap).strip().lower() if isinstance(gap, str) else None,
            "relation": str(relation).strip().lower() if isinstance(relation, str) else None,
            "confidence": row.get("confidence"),
            "legacy_severity_only": "gap_magnitude" not in row,
        }
    return predictions


def score_judgment(
    result: dict[str, Any] | None,
    labels: dict[str, dict[str, Any]],
    *,
    artifact_status: str,
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
        if status == "not_applicable":
            denominator["gt_not_applicable_cells"] += 1
        elif status == "uncertain" or gap in {"uncertain", "unknown"}:
            denominator["gt_uncertain_cells"] += 1
        elif status == "missing":
            denominator["gt_missing_cells"] += 1
        elif status == "invalid" or gap not in GT_GAPS:
            denominator["gt_invalid_cells"] += 1
        else:
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

        prediction = predictions.get(stage_code)
        if status != "labeled":
            rows.append(
                {
                    "stage": stage_code,
                    "status": status,
                    "gt_gap_magnitude": gap,
                    "gt_relation": relation,
                    "prediction": prediction,
                    "error_class": "gt_not_scored",
                }
            )
            continue
        if artifact_status != "completed" or prediction is None:
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


def _overlaps(left: Any, right: Any) -> bool:
    left_range = parse_time_range_seconds(left, None)
    right_range = parse_time_range_seconds(right, None)
    if left_range is None or right_range is None:
        return False
    return min(left_range[1], right_range[1]) > max(left_range[0], right_range[0])


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
        for function in unit.get("functions", [])
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
    present_event_rows = [row for row in event_rows if row.get("expected_state") == "present"]
    absent_event_rows = [row for row in event_rows if row.get("expected_state") == "absent"]
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
                for function in unit.get("functions", [])
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


def _sample_record(
    sample_id: str,
    gt_labels: dict[str, dict[str, Any]],
    gt_sample: dict[str, Any],
    extraction_root: Path | None,
    judgment_root: Path | None,
    model: str,
) -> dict[str, Any]:
    safe_sample = _safe_component(sample_id)
    safe_model = _safe_component(model)
    extraction_path = (
        extraction_root / safe_sample / safe_model / "visual_extraction_evaluation.json"
        if extraction_root is not None
        else Path("__not_requested__")
    )
    judgment_path = (
        judgment_root / safe_sample / safe_model / "model_independent_evaluation.json"
        if judgment_root is not None
        else Path("__not_requested__")
    )
    extraction_result, extraction_meta = _read_result_artifact(extraction_path, "result") if extraction_root else (None, {"status": "not_requested"})
    judgment_result, judgment_meta = _read_result_artifact(judgment_path, "result") if judgment_root else (None, {"status": "not_requested"})
    return {
        "sample_id": sample_id,
        "model": model,
        "extraction": {
            "artifact": extraction_meta,
            "score": score_extraction(
                extraction_result,
                gt_sample,
                artifact_status=extraction_meta["status"],
                source_durations=extraction_meta.get("source_durations"),
            ),
        },
        "judgment": {
            "artifact": judgment_meta,
            "score": score_judgment(
                judgment_result,
                gt_labels,
                artifact_status=judgment_meta["status"],
            ),
        },
    }


def _operational_summary(selected: list[dict[str, Any]], key: str) -> dict[str, Any]:
    """Summarize artifact availability without treating it as semantic quality."""
    metadata = [
        record.get(key, {}).get("artifact", {})
        for record in selected
        if isinstance(record.get(key), dict)
        and isinstance(record.get(key, {}).get("artifact"), dict)
    ]
    requested = [item for item in metadata if item.get("status") != "not_requested"]
    status_counts: dict[str, int] = {}
    failure_class_counts: dict[str, int] = {}
    contract_error_code_counts: dict[str, int] = {}
    for item in requested:
        status = str(item.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        if status != "completed":
            failure_class = str(item.get("failure_class") or "unspecified")
            failure_class_counts[failure_class] = failure_class_counts.get(failure_class, 0) + 1
        for code in item.get("contract_error_codes", []) if isinstance(item.get("contract_error_codes"), list) else []:
            normalized = str(code).strip()
            if normalized:
                contract_error_code_counts[normalized] = contract_error_code_counts.get(normalized, 0) + 1
    return {
        "requested_artifacts": len(requested),
        "completed_artifacts": sum(item.get("status") == "completed" for item in requested),
        "failed_or_missing_artifacts": sum(item.get("status") != "completed" for item in requested),
        "status_counts": dict(sorted(status_counts.items())),
        "failure_class_counts": dict(sorted(failure_class_counts.items())),
        "contract_error_code_counts": dict(sorted(contract_error_code_counts.items())),
    }


def _aggregate_judgment_stage_metrics(judgment_rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for stage_code in STAGE_CODES:
        rows = [
            row
            for score in judgment_rows
            for row in score.get("rows", [])
            if row.get("stage") == stage_code
        ]
        semantic_gap_rows = [row for row in rows if row.get("semantic_gap_comparable")]
        contract_aware_gap_rows = [
            row
            for row in rows
            if row.get("gt_gap_magnitude") in SCORABLE_GAPS
            and row.get("predicted_gap_magnitude") in SCORABLE_GAPS
        ]
        relation_rows = [row for row in rows if row.get("relation_correct") is not None]
        exact_rows = [row for row in semantic_gap_rows if row.get("relation_correct") is not None]
        large_rows = [row for row in rows if row.get("gt_gap_magnitude") == "large"]
        large_scored_rows = [row for row in large_rows if row.get("semantic_gap_comparable")]
        status_counts: dict[str, int] = {}
        error_counts: dict[str, int] = {}
        for row in rows:
            status = str(row.get("status") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
            error_class = str(row.get("error_class") or "unknown")
            error_counts[error_class] = error_counts.get(error_class, 0) + 1
        result[stage_code] = {
            "cell_count": len(rows),
            "status_counts": dict(sorted(status_counts.items())),
            "contract_aware_gap_cells": len(contract_aware_gap_rows),
            "contract_aware_gap_correct_cells": sum(row.get("gap_correct") is True for row in contract_aware_gap_rows),
            "contract_aware_gap_accuracy": (
                sum(row.get("gap_correct") is True for row in contract_aware_gap_rows)
                / len(contract_aware_gap_rows)
                if contract_aware_gap_rows
                else None
            ),
            "semantic_gap_cells": len(semantic_gap_rows),
            "semantic_gap_correct_cells": sum(row.get("gap_correct") is True for row in semantic_gap_rows),
            "semantic_gap_accuracy": (
                sum(row.get("gap_correct") is True for row in semantic_gap_rows)
                / len(semantic_gap_rows)
                if semantic_gap_rows
                else None
            ),
            "contract_representation_gap_cells": sum(
                row.get("error_class") == "contract_representation_gap" for row in rows
            ),
            "relation_cells": len(relation_rows),
            "relation_correct_cells": sum(row.get("relation_correct") is True for row in relation_rows),
            "relation_accuracy": (
                sum(row.get("relation_correct") is True for row in relation_rows) / len(relation_rows)
                if relation_rows
                else None
            ),
            "exact_direction_and_gap_cells": len(exact_rows),
            "exact_direction_and_gap_correct_cells": sum(
                row.get("gap_correct") is True and row.get("relation_correct") is True
                for row in exact_rows
            ),
            "exact_direction_and_gap_accuracy": (
                sum(row.get("gap_correct") is True and row.get("relation_correct") is True for row in exact_rows)
                / len(exact_rows)
                if exact_rows
                else None
            ),
            "gt_large_cells": len(large_rows),
            "gt_large_scored_cells": len(large_scored_rows),
            "gt_large_unavailable_cells": len(large_rows) - len(large_scored_rows),
            "gt_large_correct_cells": sum(row.get("gap_correct") is True for row in large_scored_rows),
            "gt_large_missed_cells": sum(row.get("gap_correct") is not True for row in large_scored_rows),
            "gt_large_recall": (
                sum(row.get("gap_correct") is True for row in large_scored_rows) / len(large_scored_rows)
                if large_scored_rows
                else None
            ),
            "error_class_counts": dict(sorted(error_counts.items())),
        }
    return result


def _merge_stage_quality_counts(stage_scores: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    merged: dict[str, dict[str, int]] = {field: {} for field in QUALITY_FIELDS}
    for score in stage_scores:
        source = score.get("quality_counts") if isinstance(score, dict) else None
        if not isinstance(source, dict):
            continue
        for field, values in source.items():
            if field not in merged or not isinstance(values, dict):
                continue
            for value, count in values.items():
                merged[field][str(value)] = merged[field].get(str(value), 0) + int(count)
    return {field: dict(sorted(values.items())) for field, values in merged.items() if values}


def aggregate_model(records: list[dict[str, Any]], model: str) -> dict[str, Any]:
    selected = [record for record in records if record.get("model") == model]
    judgment_rows = [record["judgment"]["score"] for record in selected]
    extraction_rows = [record["extraction"]["score"] for record in selected]
    judgment_denominator = _empty_denominator()
    for score in judgment_rows:
        for key, value in score["denominator"].items():
            judgment_denominator[key] += int(value)
    gap_rows = [
        row
        for score in judgment_rows
        for row in score["rows"]
        if row.get("status") == "labeled"
        and row.get("gt_gap_magnitude") in SCORABLE_GAPS
        and row.get("predicted_gap_magnitude") in SCORABLE_GAPS
    ]
    semantic_gap_rows = [row for row in gap_rows if row.get("semantic_gap_comparable")]
    relation_rows = [
        row
        for score in judgment_rows
        for row in score["rows"]
        if row.get("relation_correct") is not None
    ]
    exact_rows = [row for row in gap_rows if row.get("relation_correct") is not None]
    extraction_denominator = {
        key: sum(int(score["denominator"][key]) for score in extraction_rows)
        for key in (
            "required_key_events",
            "present_key_events",
            "invalid_key_events",
            "absence_checks",
            "absence_respected",
            "absence_false_positive_units",
            "scored_key_events",
            "matched_key_events",
            "valid_model_units",
            "model_units_matching_key_events",
            "model_failure_or_missing",
        )
    }
    stage_metrics: dict[str, Any] = {}
    for stage_code in STAGE_CODES:
        stage_scores = [score["stage_metrics"][stage_code] for score in extraction_rows]
        events = sum(score["required_event_count"] for score in stage_scores)
        absence_checks = sum(score["absence_check_count"] for score in stage_scores)
        absence_respected = sum(score["absence_respected_count"] for score in stage_scores)
        scored_events = sum(score["scored_event_count"] for score in stage_scores)
        matched = sum(score["matched_event_count"] for score in stage_scores)
        units = sum(score["unit_count"] for score in stage_scores)
        quality_coverage_numerator = sum(
            score["quality_coverage"] * score["unit_count"] for score in stage_scores
        )
        stage_metrics[stage_code] = {
            "required_event_count": events,
            "scored_event_count": scored_events,
            "matched_event_count": matched,
            "recall_proxy": matched / scored_events if scored_events else None,
            "absence_check_count": absence_checks,
            "absence_respected_count": absence_respected,
            "absence_respected_rate": (
                absence_respected / absence_checks if absence_checks else None
            ),
            "unit_count": units,
            "quality_coverage": quality_coverage_numerator / units if units else 0.0,
            "quality_counts": _merge_stage_quality_counts(stage_scores),
        }
    judgment_stage_metrics = _aggregate_judgment_stage_metrics(judgment_rows)
    error_class_counts = {
        error_class: sum(
            row.get("error_class") == error_class
            for score in judgment_rows
            for row in score["rows"]
        )
        for error_class in sorted(
            {
                str(row.get("error_class"))
                for score in judgment_rows
                for row in score["rows"]
            }
        )
    }
    return {
        "model": model,
        "sample_count": len(selected),
        "judgment": {
            "denominator": judgment_denominator,
            "gap_accuracy": (
                sum(row.get("gap_correct") is True for row in semantic_gap_rows) / len(semantic_gap_rows)
                if semantic_gap_rows
                else None
            ),
            "contract_aware_gap_accuracy": (
                sum(row.get("gap_correct") is True for row in gap_rows) / len(gap_rows)
                if gap_rows
                else None
            ),
            "semantic_gap_accuracy_excluding_representation_gap": (
                sum(row.get("gap_correct") is True for row in semantic_gap_rows) / len(semantic_gap_rows)
                if semantic_gap_rows
                else None
            ),
            "contract_representation_gap_rate": (
                sum(row.get("contract_representation_gap") is True for score in judgment_rows for row in score["rows"])
                / judgment_denominator["gt_labeled_cells"]
                if judgment_denominator["gt_labeled_cells"]
                else None
            ),
            "relation_accuracy": sum(row.get("relation_correct") is True for row in relation_rows) / len(relation_rows) if relation_rows else None,
            "exact_direction_and_gap_accuracy": (
                sum(row.get("gap_correct") is True and row.get("relation_correct") is True for row in exact_rows if row.get("semantic_gap_comparable"))
                / sum(row.get("semantic_gap_comparable") is True for row in exact_rows)
                if any(row.get("semantic_gap_comparable") for row in exact_rows)
                else None
            ),
            "stage_metrics": judgment_stage_metrics,
            "error_class_counts": error_class_counts,
            "operational": _operational_summary(selected, "judgment"),
        },
        "extraction": {
            "denominator": extraction_denominator,
            "temporal_stage_recall_proxy": (
                extraction_denominator["matched_key_events"] / extraction_denominator["scored_key_events"]
                if extraction_denominator["scored_key_events"]
                else None
            ),
            "temporal_stage_precision_proxy": (
                extraction_denominator["model_units_matching_key_events"] / extraction_denominator["valid_model_units"]
                if extraction_denominator["scored_key_events"] and extraction_denominator["valid_model_units"]
                else None
            ),
            "absence_respected_rate": (
                extraction_denominator["absence_respected"] / extraction_denominator["absence_checks"]
                if extraction_denominator["absence_checks"]
                else None
            ),
            "stage_metrics": stage_metrics,
            "operational": _operational_summary(selected, "extraction"),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Score saved Flayr extraction/judgment artifacts against human GT without API calls.",
        allow_abbrev=False,
    )
    parser.add_argument("--gt-path", type=Path, required=True)
    parser.add_argument("--extraction-root", type=Path, default=None)
    parser.add_argument("--judgment-root", type=Path, default=None)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--models", nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--evaluation-role",
        choices=("model_calibration", "mechanism_regression", "blind_validation"),
        default="model_calibration",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.extraction_root is None and args.judgment_root is None:
        raise SystemExit("at least one of --extraction-root or --judgment-root is required")
    gt_path = args.gt_path.expanduser().resolve()
    gt_data = _read_json(gt_path)
    samples = gt_data.get("samples") if isinstance(gt_data, dict) else None
    if not isinstance(samples, dict):
        raise SystemExit("GT must contain a samples object")
    sample_ids = _sample_ids(gt_path, args.manifest.expanduser().resolve() if args.manifest else None)
    extraction_root = args.extraction_root.expanduser().resolve() if args.extraction_root else None
    judgment_root = args.judgment_root.expanduser().resolve() if args.judgment_root else None
    records: list[dict[str, Any]] = []
    for sample_id in sample_ids:
        sample = samples.get(sample_id)
        if not isinstance(sample, dict):
            raise SystemExit(f"GT sample is missing or invalid: {sample_id}")
        labels = load_gt_stage_labels(gt_path, sample_id)
        for model in args.models:
            records.append(_sample_record(sample_id, labels, sample, extraction_root, judgment_root, model))
    output = {
        "schema_version": ALIGNMENT_SCHEMA_VERSION,
        "protocol": ALIGNMENT_PROTOCOL,
        "evaluation_role": args.evaluation_role,
        "promotion_eligible": False,
        "gt_loaded": True,
        "model_results_are_prompt_gt_free": True,
        "source_commit": current_code_commit(),
        "gt_path": str(gt_path),
        "gt_sha256": _sha256(gt_path),
        "sample_ids": sample_ids,
        "population": {
            "sample_count": len(sample_ids),
            "stage_count_per_sample": len(STAGE_CODES),
            "stage_cell_count": len(sample_ids) * len(STAGE_CODES),
            "denominator_rule": "exclude not_applicable, uncertain, missing, and invalid GT cells from semantic accuracy; count model failures separately",
            "extraction_matching_rule": "role + stage function + positive time overlap; semantic truth is not proven by this proxy",
        },
        "metric_definitions": ALIGNMENT_METRIC_DEFINITIONS,
        "models": list(args.models),
        "records": records,
        "aggregate": [aggregate_model(records, model) for model in args.models],
    }
    output_path = args.output.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(output_path, output)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
