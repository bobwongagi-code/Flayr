#!/usr/bin/env python3
"""Run one isolated compact-evaluation variant over a declared sample cohort.

Manifest format::

    {
      "samples": [
        {"sample_id": "are_xie", "run_dir": "/path/to/completed-run"}
      ]
    }

The runner performs a local preflight for every sample before making an API
request. Each sample is loaded once and reused for every model, so the model
comparison within a sample has one source digest and one visual input set.
Outputs are calibration/regression artifacts only; this command has no model
promotion path.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm.compact_eval import (  # noqa: E402
    COMPACT_OUTPUT_BUDGET,
    CompactEvaluationError,
    build_s4_state_locked_bundle,
    EVALUATION_ROLES,
    contract_limits_for_variant,
    load_frozen_compact_bundle,
    load_frozen_video_bundle,
    load_gt_stages,
    run_compact_evaluation,
    run_s4_fact_state_evaluation,
    run_s4_judgment_evaluation,
    run_s5_audit_evaluation,
    run_severity_only_evaluation,
    run_visual_extraction_evaluation,
)
from flayr_core.utils import write_json  # noqa: E402
from flayr_core.report_metadata import current_code_commit  # noqa: E402


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value).strip())
    return cleaned.strip("._") or "unnamed"


def _safe_component_map(values: list[str], *, label: str) -> dict[str, str]:
    """Reject sanitized-name collisions before artifacts can overwrite each other."""
    safe_to_value: dict[str, str] = {}
    value_to_safe: dict[str, str] = {}
    for value in values:
        if value in value_to_safe:
            raise CompactEvaluationError(f"{label} contains duplicate value: {value!r}")
        safe = _safe_component(value)
        previous = safe_to_value.get(safe)
        if previous is not None and previous != value:
            raise CompactEvaluationError(
                f"{label} values {previous!r} and {value!r} share output component {safe!r}"
            )
        safe_to_value[safe] = value
        value_to_safe[value] = safe
    return value_to_safe


def _read_manifest(path: Path) -> list[dict[str, str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CompactEvaluationError(f"invalid cohort manifest: {path}: {exc}") from exc
    rows = data.get("samples") if isinstance(data, dict) else data
    if not isinstance(rows, list) or not rows:
        raise CompactEvaluationError("cohort manifest must contain a non-empty samples list")
    manifest_root = path.parent.resolve()
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise CompactEvaluationError(f"cohort manifest sample {index} must be an object")
        sample_id = str(row.get("sample_id") or "").strip()
        raw_run_dir = str(row.get("run_dir") or "").strip()
        if not sample_id or not raw_run_dir:
            raise CompactEvaluationError(f"cohort manifest sample {index} needs sample_id and run_dir")
        if sample_id in seen:
            raise CompactEvaluationError(f"cohort manifest contains duplicate sample_id: {sample_id}")
        seen.add(sample_id)
        run_dir = Path(raw_run_dir).expanduser()
        if not run_dir.is_absolute():
            run_dir = manifest_root / run_dir
        result.append({"sample_id": sample_id, "run_dir": str(run_dir.resolve())})
    return result


def _run_variant(
    *,
    variant: str,
    model: str,
    bundle: Any,
    output_dir: Path,
    args: argparse.Namespace,
    api_key_args: Any,
    gt_stages: dict[str, str] | None,
) -> dict[str, Any]:
    common = {
        "model": model,
        "bundle": bundle,
        "output_dir": output_dir,
        "api_url": args.api_url,
        "api_key_args": api_key_args,
        "output_budget": args.output_budget,
        "output_budget_field": args.output_budget_field,
        "request_timeout_seconds": args.request_timeout_seconds,
        "evaluation_role": args.evaluation_role,
    }
    if variant == "evidence_grounded":
        return run_compact_evaluation(
            **common,
            gt_stages=gt_stages,
            max_stage_evidence_ids=args.max_stage_evidence_ids,
        )
    if variant == "severity_only":
        return run_severity_only_evaluation(**common, gt_stages=gt_stages, scaffold=False)
    if variant == "severity_scaffold":
        return run_severity_only_evaluation(**common, gt_stages=gt_stages, scaffold=True)
    if variant == "visual_extraction":
        return run_visual_extraction_evaluation(**common)
    if variant == "s4_fact_state":
        return run_s4_fact_state_evaluation(**common)
    if variant == "s4_judgment":
        return run_s4_judgment_evaluation(**common)
    return run_s5_audit_evaluation(**common)


def _load_s4_state_locked_bundle(
    state_root: Path,
    *,
    sample_id: str,
    model: str,
    base_bundle: Any,
) -> tuple[Any, Path]:
    path = (
        state_root
        / _safe_component(sample_id)
        / _safe_component(model)
        / "s4_fact_state_evaluation.json"
    )
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CompactEvaluationError(f"invalid S4 state artifact: {path}: {exc}") from exc
    if not isinstance(record, dict) or record.get("status") != "completed":
        raise CompactEvaluationError(f"S4 state artifact is not completed: {path}")
    state_result = record.get("result")
    if not isinstance(state_result, dict):
        raise CompactEvaluationError(f"S4 state artifact has no result object: {path}")
    locked = build_s4_state_locked_bundle(
        base_bundle,
        state_result,
        state_artifact=str(path),
        state_source_digest=str(record.get("source_digest") or "") or None,
        state_model=str(record.get("model") or "") or None,
        expected_model=model,
    )
    return locked, path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one isolated compact-evaluation variant over a declared cohort.",
        allow_abbrev=False,
    )
    parser.add_argument("--manifest", type=Path, required=True, help="JSON manifest with sample_id and run_dir rows.")
    parser.add_argument("--output-root", type=Path, required=True, help="Dedicated experiment output root.")
    parser.add_argument("--models", nargs="+", required=True, help="All models to compare under the same variant.")
    parser.add_argument("--api-url", required=True, help="Approved Chat Completions endpoint.")
    parser.add_argument(
        "--variant",
        choices=(
            "evidence_grounded",
            "severity_only",
            "severity_scaffold",
            "visual_extraction",
            "s4_fact_state",
            "s4_judgment",
            "s5_audit",
        ),
        default="severity_only",
    )
    parser.add_argument(
        "--evaluation-role",
        choices=tuple(sorted(EVALUATION_ROLES)),
        default="model_calibration",
    )
    parser.add_argument("--api-key-env", default="DASHSCOPE_API_KEY")
    parser.add_argument("--keychain-service", default=None)
    parser.add_argument("--keychain-account", default="API_KEY")
    parser.add_argument("--gt-path", type=Path, default=ROOT / "references" / "ground-truth-labels.json")
    parser.add_argument("--output-budget", type=int, default=COMPACT_OUTPUT_BUDGET)
    parser.add_argument(
        "--output-budget-field",
        choices=("max_tokens", "max_completion_tokens"),
        default="max_completion_tokens",
    )
    parser.add_argument("--request-timeout-seconds", type=int, default=600)
    parser.add_argument(
        "--s4-state-root",
        type=Path,
        default=None,
        help="s4_judgment 必填：按 sample_id/model 保存 s4_fact_state_evaluation.json 的目录。",
    )
    parser.add_argument(
        "--max-stage-evidence-ids",
        type=int,
        default=None,
        help="仅用于 evidence_grounded 的单变量上限实验；默认使用合同值4。",
    )
    parser.add_argument(
        "--no-images",
        action="store_true",
        help="Judgment variants only: omit stage-frame attachments; raw-video extraction uses original videos.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.request_timeout_seconds < 60 or args.request_timeout_seconds > 1800:
        raise SystemExit("--request-timeout-seconds must be between 60 and 1800")
    if args.max_stage_evidence_ids is not None and not 1 <= args.max_stage_evidence_ids <= 16:
        raise SystemExit("--max-stage-evidence-ids must be between 1 and 16")
    if args.max_stage_evidence_ids is not None and args.variant != "evidence_grounded":
        raise SystemExit("--max-stage-evidence-ids is only valid with --variant evidence_grounded")
    if args.variant == "s4_judgment" and args.s4_state_root is None:
        raise SystemExit("--s4-state-root is required with --variant s4_judgment")
    manifest = _read_manifest(args.manifest.expanduser().resolve())
    sample_components = _safe_component_map(
        [sample["sample_id"] for sample in manifest],
        label="sample_id",
    )
    model_components = _safe_component_map(args.models, label="model")
    api_key_args = SimpleNamespace(
        llm_api_key_env=args.api_key_env,
        llm_api_key_keychain_service=args.keychain_service,
        llm_api_key_keychain_account=args.keychain_account,
    )
    output_root = args.output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    # Preflight all source inputs before the first request. This prevents a
    # partial cohort caused by a missing later sample or missing GT.
    preflight: list[tuple[dict[str, str], Any, dict[str, str] | None]] = []
    for sample in manifest:
        run_dir = Path(sample["run_dir"])
        if args.variant == "visual_extraction":
            bundle = load_frozen_video_bundle(run_dir)
            gt_stages = None
        elif args.variant in {"s4_fact_state", "s4_judgment", "s5_audit"}:
            bundle = load_frozen_compact_bundle(run_dir, include_images=False)
            gt_stages = None
        else:
            bundle = load_frozen_compact_bundle(run_dir, include_images=not args.no_images)
            gt_stages = load_gt_stages(args.gt_path, sample["sample_id"])
        preflight.append((sample, bundle, gt_stages))
    locked_state_bundles: dict[tuple[str, str], tuple[Any, Path]] = {}
    if args.variant == "s4_judgment":
        state_root = args.s4_state_root.expanduser().resolve()
        for sample, bundle, _ in preflight:
            for model in args.models:
                locked_state_bundles[(sample["sample_id"], model)] = _load_s4_state_locked_bundle(
                    state_root,
                    sample_id=sample["sample_id"],
                    model=model,
                    base_bundle=bundle,
                )

    contract_limits = contract_limits_for_variant(
        "visual_extraction" if args.variant == "visual_extraction" else args.variant
    )
    if args.max_stage_evidence_ids is not None and args.variant == "evidence_grounded":
        contract_limits["max_stage_evidence_ids"] = args.max_stage_evidence_ids
    if args.max_stage_evidence_ids is not None:
        contract_limits["experiment"] = {
            "name": "max_stage_evidence_ids",
            "baseline": 4,
            "candidate": args.max_stage_evidence_ids,
            "single_variable": True,
            "production_default_unchanged": True,
        }
    summary: dict[str, Any] = {
        "schema_version": 1,
        "evaluation_role": args.evaluation_role,
        "variant": args.variant,
        "promotion_eligible": False,
        "decision_scope": {
            "model_calibration": "calibration_only",
            "mechanism_regression": "mechanism_regression_only",
            "blind_validation": "blind_validation_only",
        }[args.evaluation_role],
        "models": list(args.models),
        "source_commit": current_code_commit(),
        "contract_limits": {**contract_limits, "output_budget": args.output_budget},
        "samples": [],
    }
    for sample, bundle, gt_stages in preflight:
        sample_record: dict[str, Any] = {
            "sample_id": sample["sample_id"],
            "run_dir": sample["run_dir"],
            "source_digest": bundle.source_digest,
            "results": [],
        }
        for model in args.models:
            run_bundle = bundle
            state_artifact = None
            if args.variant == "s4_judgment":
                run_bundle, state_path = locked_state_bundles[(sample["sample_id"], model)]
                state_artifact = str(state_path)
            result = _run_variant(
                variant=args.variant,
                model=model,
                bundle=run_bundle,
                output_dir=output_root / sample_components[sample["sample_id"]] / model_components[model],
                args=args,
                api_key_args=api_key_args,
                gt_stages=gt_stages,
            )
            sample_record["results"].append(
                {
                    "model": model,
                    "status": result.get("status"),
                    "gt_score": result.get("gt_score"),
                    "errors": result.get("errors"),
                    "error": result.get("error"),
                    "state_artifact": state_artifact,
                }
            )
        summary["samples"].append(sample_record)
    write_json(output_root / "cohort_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if all(item["status"] == "completed" for sample in summary["samples"] for item in sample["results"]) else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CompactEvaluationError as exc:
        raise SystemExit(str(exc)) from exc
