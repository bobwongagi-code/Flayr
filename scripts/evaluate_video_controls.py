#!/usr/bin/env python3
"""Run frozen single/dual-video controls for raw visual extraction.

The source bundle is loaded and encoded exactly once. Every condition reuses
those encoded bytes, changing only which role blocks are present and their
order. Outputs are isolated experiment artifacts and are never promotion
eligible.
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
    EVALUATION_ROLES,
    CompactEvaluationError,
    compare_visual_extraction_units,
    load_frozen_video_bundle,
    run_visual_extraction_evaluation,
    select_frozen_video_bundle,
    summarize_visual_extraction_result,
)
from flayr_core.utils import write_json  # noqa: E402


CONDITIONS: dict[str, tuple[str, ...]] = {
    "benchmark_only": ("benchmark",),
    "creator_only": ("creator",),
    "dual_benchmark_creator": ("benchmark", "creator"),
    "dual_creator_benchmark": ("creator", "benchmark"),
}
CONDITION_ORDER = tuple(CONDITIONS)


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value).strip())
    return cleaned.strip("._") or "unnamed"


def _units(result: dict[str, Any] | None, role: str) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    value = result.get(f"{role}_evidence_units", [])
    return value if isinstance(value, list) else []


def _reference_alignment(
    dual_result: dict[str, Any],
    *,
    output_role: str,
    benchmark_reference: dict[str, Any] | None,
    creator_reference: dict[str, Any] | None,
) -> dict[str, Any]:
    output_units = _units(dual_result, output_role)
    comparisons: dict[str, dict[str, Any]] = {}
    for source_role, reference in (
        ("benchmark", benchmark_reference),
        ("creator", creator_reference),
    ):
        if reference is None:
            continue
        reference_role = source_role
        comparisons[source_role] = compare_visual_extraction_units(
            output_units,
            _units(reference, reference_role),
        )
    if not comparisons:
        return {
            "output_role": output_role,
            "status": "no_single_video_reference",
            "comparisons": comparisons,
        }
    ranked = sorted(
        comparisons.items(),
        key=lambda item: (
            item[1]["temporal_stage_match_rate"],
            item[1]["exact_signature_jaccard"],
            item[1]["information_jaccard"],
        ),
        reverse=True,
    )
    best_role, best = ranked[0]
    tied = [
        role
        for role, comparison in ranked
        if (
            comparison["temporal_stage_match_rate"],
            comparison["exact_signature_jaccard"],
            comparison["information_jaccard"],
        )
        == (
            best["temporal_stage_match_rate"],
            best["exact_signature_jaccard"],
            best["information_jaccard"],
        )
    ]
    best_temporal = best["temporal_stage_match_rate"]
    second_temporal = ranked[1][1]["temporal_stage_match_rate"] if len(ranked) > 1 else 0.0
    if best_temporal < 0.5:
        status = "no_reference_match"
        source_role = None
    elif len(tied) > 1 or best_temporal - second_temporal < 0.2:
        status = "reference_tie"
        source_role = None
    else:
        status = "matched_single_video_reference"
        source_role = best_role
    return {
        "output_role": output_role,
        "status": status,
        "inferred_source_role": source_role,
        "comparisons": comparisons,
    }


def _dual_repeat_diagnostic(
    *,
    order: tuple[str, ...],
    dual_result: dict[str, Any] | None,
    benchmark_reference: dict[str, Any] | None,
    creator_reference: dict[str, Any] | None,
) -> dict[str, Any]:
    if dual_result is None:
        return {"status": "dual_result_unavailable", "input_order": list(order)}
    alignments = [
        _reference_alignment(
            dual_result,
            output_role=output_role,
            benchmark_reference=benchmark_reference,
            creator_reference=creator_reference,
        )
        for output_role in ("creator", "benchmark")
    ]
    inferred = [item.get("inferred_source_role") for item in alignments]
    status = "unclassified"
    copied_source = None
    copied_position = None
    if all(source is not None for source in inferred):
        if inferred[0] == "creator" and inferred[1] == "benchmark":
            status = "role_bound"
        elif inferred[0] == inferred[1]:
            status = "both_outputs_copy_one_source"
            copied_source = inferred[0]
            copied_position = order.index(copied_source) + 1 if copied_source in order else None
        else:
            status = "cross_role_mismatch"
    return {
        "status": status,
        "input_order": list(order),
        "copied_source_role": copied_source,
        "copied_source_position": copied_position,
        "alignments": alignments,
    }


def _aggregate_dual_diagnostics(
    *,
    order: tuple[str, ...],
    repeats: list[dict[str, Any]],
) -> dict[str, Any]:
    outcomes = [str(item.get("status")) for item in repeats]
    classified = [
        item
        for item in repeats
        if item.get("status") not in {"dual_result_unavailable", "unclassified"}
    ]
    unavailable_count = sum(item.get("status") == "dual_result_unavailable" for item in repeats)
    unique_classified_outcomes = sorted({str(item.get("status")) for item in classified})
    if not classified:
        classification = "no_completed_classifiable_dual_runs"
    elif unavailable_count:
        classification = "incomplete_due_to_contract_or_request_failure"
    elif len(unique_classified_outcomes) > 1:
        classification = "same_order_is_unstable_or_random"
    elif classified[0].get("status") == "role_bound":
        classification = "role_binding_consistent"
    elif classified[0].get("status") == "both_outputs_copy_one_source":
        positions = {item.get("copied_source_position") for item in classified}
        if positions == {1}:
            classification = "both_outputs_copy_first_video"
        elif positions == {2}:
            classification = "both_outputs_copy_second_video"
        else:
            classification = "both_outputs_copy_one_source_position_varies"
    else:
        classification = "dual_binding_needs_review"
    return {
        "input_order": list(order),
        "repeat_count": len(repeats),
        "outcomes": outcomes,
        "unique_outcomes": sorted(set(outcomes)),
        "classifiable_outcomes": unique_classified_outcomes,
        "unavailable_repeat_count": unavailable_count,
        "classification": classification,
        "repeat_diagnostics": repeats,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run frozen single/dual-video raw extraction controls.",
        allow_abbrev=False,
    )
    parser.add_argument("--run-dir", type=Path, required=True, help="Completed run containing both source videos.")
    parser.add_argument("--output-root", type=Path, required=True, help="Dedicated control-experiment output root.")
    parser.add_argument(
        "--frozen-video-cache",
        type=Path,
        default=None,
        help="Persistent cache for exact bounded MP4 bytes shared across control processes.",
    )
    parser.add_argument("--models", nargs="+", required=True, help="Models to run under every selected condition.")
    parser.add_argument("--api-url", required=True, help="Approved Chat Completions endpoint.")
    parser.add_argument("--api-key-env", default="DASHSCOPE_API_KEY")
    parser.add_argument("--keychain-service", default=None)
    parser.add_argument("--keychain-account", default="API_KEY")
    parser.add_argument("--sample-id", default="are_xie")
    parser.add_argument(
        "--conditions",
        nargs="+",
        choices=CONDITION_ORDER,
        default=CONDITION_ORDER,
        help="Control conditions. Defaults to both singles and both dual orders.",
    )
    parser.add_argument("--single-repeats", type=int, default=1)
    parser.add_argument("--dual-repeats", type=int, default=3)
    parser.add_argument(
        "--evaluation-role",
        choices=tuple(sorted(EVALUATION_ROLES)),
        default="model_calibration",
    )
    parser.add_argument("--output-budget", type=int, default=COMPACT_OUTPUT_BUDGET)
    parser.add_argument(
        "--output-budget-field",
        choices=("max_tokens", "max_completion_tokens"),
        default="max_completion_tokens",
    )
    parser.add_argument("--request-timeout-seconds", type=int, default=600)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.single_repeats < 1 or args.single_repeats > 3:
        raise SystemExit("--single-repeats must be between 1 and 3")
    if args.dual_repeats < 1 or args.dual_repeats > 5:
        raise SystemExit("--dual-repeats must be between 1 and 5")
    if args.request_timeout_seconds < 60 or args.request_timeout_seconds > 1800:
        raise SystemExit("--request-timeout-seconds must be between 60 and 1800")
    output_root = args.output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    source_run = args.run_dir.expanduser().resolve()
    cache_dir = (
        args.frozen_video_cache.expanduser().resolve()
        if args.frozen_video_cache is not None
        else source_run.parent / f".{source_run.name}.control-video-cache"
    )
    base_bundle = load_frozen_video_bundle(source_run, cache_dir=cache_dir)
    api_key_args = SimpleNamespace(
        llm_api_key_env=args.api_key_env,
        llm_api_key_keychain_service=args.keychain_service,
        llm_api_key_keychain_account=args.keychain_account,
    )
    summary: dict[str, Any] = {
        "schema_version": 1,
        "sample_id": args.sample_id,
        "source_run": str(base_bundle.run_dir),
        "base_source_digest": base_bundle.source_digest,
        "frozen_video_cache_dir": str(cache_dir),
        "evaluation_role": args.evaluation_role,
        "promotion_eligible": False,
        "promotion_note": "control diagnostics are calibration artifacts, not a model-selection decision",
        "models": list(args.models),
        "conditions": list(args.conditions),
        "frozen_video_inputs": [
            {
                "role": item["role"],
                "path": item["path"],
                "source_sha256": item["sha256"],
                "data_url_sha256": item["data_url_sha256"],
            }
            for item in base_bundle.video_inputs
        ],
        "cta_followup_policy": {
            "required_additional_samples": 3,
            "flag_if_late_cta_without_s6": 2,
            "status": "not_run_in_this_single_sample_control",
        },
        "runs": [],
        "model_diagnostics": {},
    }
    outcomes: dict[tuple[str, str, int], dict[str, Any]] = {}
    for condition in args.conditions:
        roles = CONDITIONS[condition]
        bundle = select_frozen_video_bundle(base_bundle, roles)
        repeat_count = args.single_repeats if len(roles) == 1 else args.dual_repeats
        for model in args.models:
            for repeat in range(1, repeat_count + 1):
                output_dir = output_root / _safe_component(condition) / _safe_component(model) / f"repeat-{repeat:02d}"
                result = run_visual_extraction_evaluation(
                    model=model,
                    bundle=bundle,
                    output_dir=output_dir,
                    api_url=args.api_url,
                    api_key_args=api_key_args,
                    output_budget=args.output_budget,
                    output_budget_field=args.output_budget_field,
                    request_timeout_seconds=args.request_timeout_seconds,
                    evaluation_role=args.evaluation_role,
                )
                parsed = result.get("result") if result.get("status") == "completed" else None
                resource_used = result.get("resource_budget", {}).get("used", {})
                record = {
                    "condition": condition,
                    "model": model,
                    "repeat": repeat,
                    "status": result.get("status"),
                    "output_dir": str(output_dir),
                    "input_order": list(roles),
                    "source_digest": bundle.source_digest,
                    "video_source_sha256": [item["sha256"] for item in bundle.video_inputs],
                    "metrics": summarize_visual_extraction_result(parsed, bundle) if parsed else None,
                    "resource": {
                        "elapsed_seconds": resource_used.get("elapsed_seconds"),
                        "llm_calls": resource_used.get("llm_calls"),
                        "uploaded_bytes": resource_used.get("total_uploaded_bytes"),
                        "downloaded_bytes": resource_used.get("total_downloaded_bytes"),
                        "cost_estimate": resource_used.get("cost_estimate"),
                    },
                    "error": result.get("error"),
                }
                summary["runs"].append(record)
                outcomes[(condition, model, repeat)] = {"result": parsed, "record": record}

    for model in args.models:
        benchmark_reference = next(
            (
                outcomes[("benchmark_only", model, 1)]["result"]
                for condition in ("benchmark_only",)
                if (condition, model, 1) in outcomes
                and outcomes[(condition, model, 1)]["result"] is not None
            ),
            None,
        )
        creator_reference = next(
            (
                outcomes[("creator_only", model, 1)]["result"]
                for condition in ("creator_only",)
                if (condition, model, 1) in outcomes
                and outcomes[(condition, model, 1)]["result"] is not None
            ),
            None,
        )
        model_diagnostics: dict[str, Any] = {
            "single_video_reference_status": {
                "benchmark_only": "completed" if benchmark_reference else "unavailable",
                "creator_only": "completed" if creator_reference else "unavailable",
            },
            "dual_conditions": {},
        }
        for condition in ("dual_benchmark_creator", "dual_creator_benchmark"):
            if condition not in args.conditions:
                continue
            order = CONDITIONS[condition]
            repeat_diagnostics = []
            for repeat in range(1, args.dual_repeats + 1):
                parsed = outcomes.get((condition, model, repeat), {}).get("result")
                repeat_diagnostics.append(
                    _dual_repeat_diagnostic(
                        order=order,
                        dual_result=parsed,
                        benchmark_reference=benchmark_reference,
                        creator_reference=creator_reference,
                    )
                )
            model_diagnostics["dual_conditions"][condition] = _aggregate_dual_diagnostics(
                order=order,
                repeats=repeat_diagnostics,
            )
        summary["model_diagnostics"][model] = model_diagnostics

    write_json(output_root / "control_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if all(record["status"] == "completed" for record in summary["runs"]) else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CompactEvaluationError as exc:
        raise SystemExit(str(exc)) from exc
