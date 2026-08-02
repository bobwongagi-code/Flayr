#!/usr/bin/env python3
"""Run one bounded model comparison request against a frozen Flayr run.

The output is an isolated experiment artifact. It does not publish or modify
the production analysis result, reports, derive state, or success manifest.
"""

from __future__ import annotations

import argparse
import sys
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm.compact_eval import (  # noqa: E402
    COMPACT_OUTPUT_BUDGET,
    CompactEvaluationError,
    EVALUATION_ROLES,
    load_frozen_compact_bundle,
    load_frozen_video_bundle,
    load_gt_stages,
    run_compact_evaluation,
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
        choices=("evidence_grounded", "severity_only", "severity_scaffold", "visual_extraction"),
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
        "--no-images",
        action="store_true",
        help="Judgment variants only: use frozen facts without stage-frame attachments; raw-video extraction ignores this flag.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.request_timeout_seconds < 60 or args.request_timeout_seconds > 1800:
        raise SystemExit("--request-timeout-seconds must be between 60 and 1800")
    try:
        if args.variant == "visual_extraction":
            bundle = load_frozen_video_bundle(args.run_dir)
        else:
            bundle = load_frozen_compact_bundle(args.run_dir, include_images=not args.no_images)
        gt_stages = load_gt_stages(args.gt_path, args.sample_id) if args.sample_id else None
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
            result = run_compact_evaluation(**common, gt_stages=gt_stages)
        elif args.variant == "severity_only":
            result = run_severity_only_evaluation(**common, gt_stages=gt_stages, scaffold=False)
        elif args.variant == "severity_scaffold":
            result = run_severity_only_evaluation(**common, gt_stages=gt_stages, scaffold=True)
        else:
            result = run_visual_extraction_evaluation(**common)
    except CompactEvaluationError as exc:
        raise SystemExit(str(exc)) from exc
    print(result.get("status", "unknown"))
    if result.get("gt_score"):
        print(result["gt_score"])
    return 0 if result.get("status") == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
