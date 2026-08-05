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
GT_GAPS = frozenset({"none", "small", "medium", "large", "uncertain"})
SCORABLE_GAPS = frozenset({"none", "small", "medium", "large"})
SCORABLE_RELATIONS = frozenset({"benchmark_better", "creator_better", "tie"})


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
        "scored_gap_cells": 0,
        "scored_relation_cells": 0,
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
            if relation in SCORABLE_RELATIONS:
                pass
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
        gap_available = predicted_gap in SCORABLE_GAPS
        relation_available = (
            relation not in SCORABLE_RELATIONS or predicted_relation in SCORABLE_RELATIONS
        )
        if predicted_gap in SCORABLE_GAPS and gap in SCORABLE_GAPS:
            denominator["scored_gap_cells"] += 1
        gap_correct = predicted_gap == gap
        if relation in SCORABLE_RELATIONS:
            if predicted_relation in SCORABLE_RELATIONS:
                denominator["scored_relation_cells"] += 1
            relation_correct = predicted_relation == relation
        else:
            relation_correct = None
        if prediction.get("legacy_severity_only") and gap == "none":
            error_class = "contract_representation_gap"
        elif not gap_available or not relation_available:
            error_class = "prediction_unavailable"
        elif relation in SCORABLE_RELATIONS and not relation_correct and not gap_correct:
            error_class = "direction_and_magnitude_error"
        elif relation in SCORABLE_RELATIONS and not relation_correct:
            error_class = "direction_error"
        elif not gap_correct:
            error_class = "magnitude_error"
        elif relation in SCORABLE_RELATIONS and prediction.get("relation") not in SCORABLE_RELATIONS:
            error_class = "direction_unavailable"
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
                "gap_correct": gap_correct,
                "relation_correct": relation_correct,
                "error_class": error_class,
            }
        )
    scored_gap = [
        row
        for row in rows
        if row.get("status") == "labeled" and row.get("predicted_gap_magnitude") in SCORABLE_GAPS
    ]
    scored_relation = [row for row in rows if row.get("relation_correct") is not None]
    return {
        "artifact_status": artifact_status,
        "denominator": denominator,
        "metrics": {
            "gap_accuracy": (
                sum(row.get("gap_correct") is True for row in scored_gap) / len(scored_gap)
                if scored_gap
                else None
            ),
            "relation_accuracy": (
                sum(row.get("relation_correct") is True for row in scored_relation) / len(scored_relation)
                if scored_relation
                else None
            ),
            "exact_direction_and_gap_accuracy": (
                sum(row.get("gap_correct") is True and row.get("relation_correct") is True for row in scored_relation)
                / len(scored_relation)
                if scored_relation
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
    event_rows: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
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
        matched = bool(matching)
        if matched:
            matched_event_indexes.add(index)
            unit_index, _ = matching[0]
            matched_unit_keys.add((role, unit_index))
        event_rows.append(
            {
                "event_index": index,
                "event_id": event.get("id"),
                "role": role,
                "stage": str(event.get("stage") or ""),
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
    recall_denominator = len(events)
    recall_numerator = len(matched_event_indexes)
    artifact_completed = artifact_status == "completed"
    stage_metrics: dict[str, Any] = {}
    for stage_code in STAGE_CODES:
        stage_events = [row for row in event_rows if str(row.get("stage") or "").upper().startswith(stage_code)]
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
        stage_metrics[stage_code] = {
            "required_event_count": len(stage_events),
            "scored_event_count": len(stage_events) if artifact_completed else 0,
            "matched_event_count": sum(row["matched"] for row in stage_events) if artifact_completed else 0,
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
                ("subject", "visibility", "composition", "completion", "proof", "causal_link"),
            ),
        }
    return {
        "artifact_status": artifact_status,
        "matching_method": "role + stage function + positive time-range overlap; semantic truth requires human review",
        "denominator": {
            "required_key_events": recall_denominator,
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
        if row.get("status") == "labeled" and row.get("predicted_gap_magnitude") in SCORABLE_GAPS
    ]
    relation_rows = [row for row in gap_rows if row.get("relation_correct") is not None]
    extraction_denominator = {
        key: sum(int(score["denominator"][key]) for score in extraction_rows)
        for key in (
            "required_key_events",
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
            "unit_count": units,
            "quality_coverage": quality_coverage_numerator / units if units else 0.0,
        }
    return {
        "model": model,
        "sample_count": len(selected),
        "judgment": {
            "denominator": judgment_denominator,
            "gap_accuracy": sum(row.get("gap_correct") is True for row in gap_rows) / len(gap_rows) if gap_rows else None,
            "relation_accuracy": sum(row.get("relation_correct") is True for row in relation_rows) / len(relation_rows) if relation_rows else None,
            "exact_direction_and_gap_accuracy": (
                sum(row.get("gap_correct") is True and row.get("relation_correct") is True for row in relation_rows)
                / len(relation_rows)
                if relation_rows
                else None
            ),
            "error_class_counts": {
                error_class: sum(row.get("error_class") == error_class for score in judgment_rows for row in score["rows"])
                for error_class in sorted({str(row.get("error_class")) for score in judgment_rows for row in score["rows"]})
            },
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
            "stage_metrics": stage_metrics,
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
        "schema_version": 1,
        "protocol": "human_model_alignment_v1",
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
