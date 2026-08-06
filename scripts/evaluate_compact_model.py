#!/usr/bin/env python3
"""Run one bounded model comparison request against a frozen Flayr run.

The output is an isolated experiment artifact. It does not publish or modify
the production analysis result, reports, derive state, or success manifest.
"""

from __future__ import annotations

import argparse
import json
import sys
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm.compact_eval import (  # noqa: E402
    COMPACT_OUTPUT_BUDGET,
    CompactEvaluationError,
    build_s4_state_locked_bundle,
    EVALUATION_ROLES,
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one isolated Flayr model-contract evaluation variant.",
        allow_abbrev=False,
    )
    parser.add_argument("--run-dir", type=Path, required=True, help="Completed run containing frozen facts and frames.")
    parser.add_argument("--output-dir", type=Path, required=True, help="Dedicated experiment output directory.")
    parser.add_argument("--model", required=True, help="Model name to compare.")
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
        default="evidence_grounded",
        help="Use the same variant for every model in a comparison cohort.",
    )
    parser.add_argument(
        "--evaluation-role",
        choices=tuple(sorted(EVALUATION_ROLES)),
        default="model_calibration",
        help="Calibration/regression role; no isolated run is promotion-eligible.",
    )
    parser.add_argument("--api-key-env", default="DASHSCOPE_API_KEY", help="Environment variable holding the API key.")
    parser.add_argument("--keychain-service", default=None, help="Optional macOS Keychain service fallback.")
    parser.add_argument("--keychain-account", default="API_KEY")
    parser.add_argument("--sample-id", default="", help="Optional GT sample ID, for example are_xie.")
    parser.add_argument(
        "--gt-path",
        type=Path,
        default=ROOT / "references" / "ground-truth-labels.json",
        help="Ground-truth labels JSON.",
    )
    parser.add_argument("--output-budget", type=int, default=COMPACT_OUTPUT_BUDGET)
    parser.add_argument(
        "--output-budget-field",
        choices=("max_tokens", "max_completion_tokens"),
        default="max_completion_tokens",
    )
    parser.add_argument("--request-timeout-seconds", type=int, default=600)
    parser.add_argument(
        "--max-stage-evidence-ids",
        type=int,
        default=None,
        help="仅用于 evidence_grounded 的4→8单变量诊断；不改变生产默认值4。",
    )
    parser.add_argument(
        "--s4-state-path",
        type=Path,
        default=None,
        help="s4_judgment 必填：同一运行、同一模型生成的 s4_fact_state_evaluation.json。",
    )
    parser.add_argument(
        "--no-images",
        action="store_true",
        help="Judgment variants only: use frozen facts without stage-frame attachments; raw-video extraction ignores this flag.",
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
    if args.variant == "s4_judgment" and args.s4_state_path is None:
        raise SystemExit("--s4-state-path is required with --variant s4_judgment")
    try:
        if args.variant == "visual_extraction":
            bundle = load_frozen_video_bundle(args.run_dir)
        elif args.variant in {"s4_fact_state", "s4_judgment", "s5_audit"}:
            bundle = load_frozen_compact_bundle(args.run_dir, include_images=False)
        else:
            bundle = load_frozen_compact_bundle(args.run_dir, include_images=not args.no_images)
        gt_stages = (
            load_gt_stages(args.gt_path, args.sample_id)
            if args.sample_id and args.variant in {"evidence_grounded", "severity_only", "severity_scaffold"}
            else None
        )
        api_key_args = SimpleNamespace(
            llm_api_key_env=args.api_key_env,
            llm_api_key_keychain_service=args.keychain_service,
            llm_api_key_keychain_account=args.keychain_account,
        )
        common = {
            "model": args.model,
            "bundle": bundle,
            "output_dir": args.output_dir,
            "api_url": args.api_url,
            "api_key_args": api_key_args,
            "output_budget": args.output_budget,
            "output_budget_field": args.output_budget_field,
            "request_timeout_seconds": args.request_timeout_seconds,
            "evaluation_role": args.evaluation_role,
        }
        if args.variant == "evidence_grounded":
            result = run_compact_evaluation(
                **common,
                gt_stages=gt_stages,
                max_stage_evidence_ids=args.max_stage_evidence_ids,
            )
        elif args.variant == "severity_only":
            result = run_severity_only_evaluation(**common, gt_stages=gt_stages, scaffold=False)
        elif args.variant == "severity_scaffold":
            result = run_severity_only_evaluation(**common, gt_stages=gt_stages, scaffold=True)
        elif args.variant == "visual_extraction":
            result = run_visual_extraction_evaluation(**common)
        elif args.variant == "s4_fact_state":
            result = run_s4_fact_state_evaluation(**common)
        elif args.variant == "s5_audit":
            result = run_s5_audit_evaluation(**common)
        else:
            state_path = args.s4_state_path.expanduser().resolve()
            try:
                state_record = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CompactEvaluationError(f"invalid S4 state artifact: {state_path}: {exc}") from exc
            if not isinstance(state_record, dict) or state_record.get("status") != "completed":
                raise CompactEvaluationError("S4 state artifact must be a completed evaluation artifact")
            state_result = state_record.get("result")
            if not isinstance(state_result, dict):
                raise CompactEvaluationError("S4 state artifact has no result object")
            locked_bundle = build_s4_state_locked_bundle(
                bundle,
                state_result,
                state_artifact=str(state_path),
                state_source_digest=str(state_record.get("source_digest") or "") or None,
                state_model=str(state_record.get("model") or "") or None,
                expected_model=args.model,
            )
            result = run_s4_judgment_evaluation(**{**common, "bundle": locked_bundle})
    except CompactEvaluationError as exc:
        raise SystemExit(str(exc)) from exc
    print(result.get("status", "unknown"))
    if result.get("gt_score"):
        print(result["gt_score"])
    return 0 if result.get("status") == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
