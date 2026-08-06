#!/usr/bin/env python3
"""Summarize isolated S4 structure experiments against human gap labels.

This is intentionally an offline scorer.  It never calls a model and keeps
semantic gap scoring, missing artifacts, contract failures, and unavailable
human direction labels separate.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm.compact_eval import load_gt_stage_labels  # noqa: E402
from flayr_core.report_metadata import current_code_commit  # noqa: E402
from flayr_core.utils import write_json  # noqa: E402


SCORABLE_GAPS = frozenset({"none", "small", "medium", "large"})
SCORABLE_RELATIONS = frozenset({"benchmark_better", "creator_better", "tie"})
VARIANT_ARTIFACTS = {
    "single_pass": ("s4_single_pass_evaluation.json", "s4_single_pass_failure.json"),
    "free_text_steps": ("s4_free_text_steps_evaluation.json", "s4_free_text_steps_failure.json"),
    "locked_state": ("s4_judgment_evaluation.json", "s4_judgment_failure.json"),
}


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value).strip())
    return cleaned.strip("._") or "unnamed"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _read_sample_ids(manifest_path: Path) -> list[str]:
    data = _read_json(manifest_path)
    rows = data.get("samples")
    if not isinstance(rows, list) or not rows:
        raise ValueError("manifest must contain a non-empty samples list")
    sample_ids: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"manifest sample {index} must be an object")
        sample_id = str(row.get("sample_id") or "").strip()
        if not sample_id:
            raise ValueError(f"manifest sample {index} is missing sample_id")
        sample_ids.append(sample_id)
    if len(set(sample_ids)) != len(sample_ids):
        raise ValueError("manifest contains duplicate sample_id values")
    return sample_ids


def _read_variant_root(value: str) -> tuple[str, Path]:
    name, separator, raw_path = value.partition("=")
    if separator != "=" or not name.strip() or not raw_path.strip():
        raise ValueError("--variant-root must use NAME=PATH")
    name = name.strip()
    if name not in VARIANT_ARTIFACTS:
        raise ValueError(f"unsupported S4 variant: {name}")
    return name, Path(raw_path).expanduser().resolve()


def _artifact_paths(root: Path, sample_id: str, model: str, variant: str) -> tuple[Path, Path]:
    success_name, failure_name = VARIANT_ARTIFACTS[variant]
    base = root / _safe_component(sample_id) / _safe_component(model)
    return base / success_name, base / failure_name


def _load_artifact(root: Path, sample_id: str, model: str, variant: str) -> tuple[dict[str, Any] | None, Path | None]:
    success_path, failure_path = _artifact_paths(root, sample_id, model, variant)
    for path in (success_path, failure_path):
        if path.is_file():
            return _read_json(path), path
    return None, None


def _load_fact_state_artifact(root: Path, sample_id: str, model: str) -> tuple[dict[str, Any] | None, Path | None]:
    base = root / _safe_component(sample_id) / _safe_component(model)
    for name in ("s4_fact_state_evaluation.json", "s4_fact_state_failure.json"):
        path = base / name
        if path.is_file():
            return _read_json(path), path
    return None, None


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def summarize_variant(
    *,
    sample_ids: list[str],
    gt_path: Path,
    root: Path,
    model: str,
    variant: str,
    fact_state_root: Path | None = None,
) -> dict[str, Any]:
    """Score one saved S4 variant without hiding operational failures."""

    if variant not in VARIANT_ARTIFACTS:
        raise ValueError(f"unsupported S4 variant: {variant}")
    rows: list[dict[str, Any]] = []
    labelled = completed = scored_gap = correct_gap = 0
    gt_large = detected_large = 0
    relation_labelled = relation_scored = relation_correct = 0
    missing_artifact = contract_failed = request_failed = other_failed = blocked_fact_state = uncertain_prediction = 0

    for sample_id in sample_ids:
        labels = load_gt_stage_labels(gt_path, sample_id)
        label = labels["S4"]
        gt_gap = label.get("gap_magnitude")
        gt_status = label.get("status")
        gt_relation = label.get("relation")
        artifact, artifact_path = _load_artifact(root, sample_id, model, variant)
        artifact_status = str(artifact.get("status") or "") if artifact else "missing"
        fact_state_artifact = None
        fact_state_path = None
        if artifact is None and variant == "locked_state" and fact_state_root is not None:
            fact_state_artifact, fact_state_path = _load_fact_state_artifact(fact_state_root, sample_id, model)
            if isinstance(fact_state_artifact, dict) and fact_state_artifact.get("status") != "completed":
                artifact_status = "blocked_fact_state"
        result = artifact.get("result") if isinstance(artifact, dict) else None
        result = result if isinstance(result, dict) else {}
        predicted_gap = str(result.get("gap_magnitude") or "").strip().lower() or None
        predicted_relation = str(result.get("relation") or "").strip().lower() or None

        if gt_status == "labeled" and gt_gap in SCORABLE_GAPS:
            labelled += 1
            if gt_gap == "large":
                gt_large += 1
        if artifact_status == "completed":
            completed += 1
        elif artifact_status == "blocked_fact_state":
            blocked_fact_state += 1
        elif artifact_status == "missing":
            missing_artifact += 1
        elif artifact_status == "contract_failed":
            contract_failed += 1
        elif artifact_status == "request_failed":
            request_failed += 1
        else:
            other_failed += 1

        gap_is_scorable = artifact_status == "completed" and predicted_gap in SCORABLE_GAPS
        gap_correct = False
        if gt_status == "labeled" and gt_gap in SCORABLE_GAPS and gap_is_scorable:
            scored_gap += 1
            gap_correct = predicted_gap == gt_gap
            correct_gap += int(gap_correct)
            if gt_gap == "large" and predicted_gap == "large":
                detected_large += 1
        elif artifact_status == "completed" and predicted_gap == "uncertain":
            uncertain_prediction += 1

        relation_is_scorable = artifact_status == "completed" and predicted_relation in SCORABLE_RELATIONS
        if gt_relation in SCORABLE_RELATIONS:
            relation_labelled += 1
            if relation_is_scorable:
                relation_scored += 1
                relation_correct += int(predicted_relation == gt_relation)

        rows.append(
            {
                "sample_id": sample_id,
                "gt_status": gt_status,
                "gt_gap_magnitude": gt_gap,
                "gt_relation": gt_relation,
                "artifact_status": artifact_status,
                "artifact_path": str(artifact_path) if artifact_path else None,
                "failure_class": artifact.get("failure_class") if artifact else None,
                "fact_state_artifact_path": str(fact_state_path) if fact_state_path else None,
                "fact_state_status": fact_state_artifact.get("status") if fact_state_artifact else None,
                "fact_state_failure_class": fact_state_artifact.get("failure_class") if fact_state_artifact else None,
                "predicted_gap_magnitude": predicted_gap,
                "predicted_relation": predicted_relation,
                "gap_scorable": gap_is_scorable,
                "gap_correct": gap_correct if gap_is_scorable else None,
            }
        )

    return {
        "variant": variant,
        "artifact_root": str(root),
        "model": model,
        "metrics": {
            "human_labeled_s4_cells": labelled,
            "completed_artifacts": completed,
            "operational_success_rate": _rate(completed, len(sample_ids)),
            "scored_gap_cells": scored_gap,
            "correct_gap_cells": correct_gap,
            "gap_accuracy_among_scorable_outputs": _rate(correct_gap, scored_gap),
            "end_to_end_gap_accuracy": _rate(correct_gap, labelled),
            "gt_large_cells": gt_large,
            "correctly_detected_large_cells": detected_large,
            "end_to_end_gt_large_recall": _rate(detected_large, gt_large),
            "relation_labeled_cells": relation_labelled,
            "relation_scored_cells": relation_scored,
            "relation_accuracy": _rate(relation_correct, relation_scored),
            "missing_artifacts": missing_artifact,
            "blocked_fact_state_artifacts": blocked_fact_state,
            "contract_failed_artifacts": contract_failed,
            "request_failed_artifacts": request_failed,
            "other_failed_artifacts": other_failed,
            "uncertain_predictions": uncertain_prediction,
        },
        "notes": {
            "relation_accuracy": (
                "unavailable because this human_initial GT has no frozen stage_relations"
                if relation_labelled == 0
                else "computed only where a frozen human stage relation exists"
            ),
            "gap_accuracy": "completed predictions of uncertain are not treated as correct or silently excluded from the human-label denominator",
            "scope": "calibration-only mechanism experiment; not a blind-validation or promotion result",
        },
        "rows": rows,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Summarize saved S4 structure-control artifacts without API calls.",
        allow_abbrev=False,
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--gt-path", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--variant-root",
        action="append",
        required=True,
        metavar="NAME=PATH",
        help="Repeat for single_pass, free_text_steps, and locked_state.",
    )
    parser.add_argument(
        "--locked-state-fact-root",
        type=Path,
        default=None,
        help="Optional s4_fact_state artifact root; classifies blocked locked-state rows separately from missing outputs.",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    sample_ids = _read_sample_ids(args.manifest.expanduser().resolve())
    gt_path = args.gt_path.expanduser().resolve()
    roots = dict(_read_variant_root(value) for value in args.variant_root)
    if len(roots) != len(args.variant_root):
        raise SystemExit("--variant-root names must be unique")
    summary = {
        "schema_version": 1,
        "evaluation_role": "model_calibration",
        "decision_scope": "calibration_only",
        "promotion_eligible": False,
        "promotion_note": "This is a seen human_initial calibration experiment, not a model-promotion decision.",
        "source_commit": current_code_commit(),
        "manifest": str(args.manifest.expanduser().resolve()),
        "gt_path": str(gt_path),
        "sample_ids": sample_ids,
        "model": args.model,
        "variants": [
            summarize_variant(
                sample_ids=sample_ids,
                gt_path=gt_path,
                root=root,
                model=args.model,
                variant=name,
                fact_state_root=(args.locked_state_fact_root.expanduser().resolve() if args.locked_state_fact_root else None),
            )
            for name, root in roots.items()
        ],
    }
    output = args.output.expanduser().resolve()
    write_json(output, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
