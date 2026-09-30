"""Read-only lookup of explicitly selected, human-reviewed run stages."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCRIPTS_ROOT = Path(__file__).resolve().parent
if __package__:
    from scripts.flayr_core.human_review import (
        FINAL_DECISIONS,
        STAGE_CODES,
        HumanReviewError,
        analysis_stage_map,
        load_analysis,
        load_review,
        model_stage_values,
    )
else:
    if str(SCRIPTS_ROOT) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_ROOT))
    from flayr_core.human_review import (
        FINAL_DECISIONS,
        STAGE_CODES,
        HumanReviewError,
        analysis_stage_map,
        load_analysis,
        load_review,
        model_stage_values,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Look up final human-reviewed stages in explicitly selected runs."
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        action="append",
        required=True,
        help="Completed run directory; repeat for each run to inspect.",
    )
    parser.add_argument(
        "--stage", choices=STAGE_CODES, help="Limit results to one stage."
    )
    parser.add_argument(
        "--query", help="Case-sensitive literal substring filter over displayed fields."
    )
    return parser


def _render_case(
    run_dir: Path,
    code: str,
    stage: Mapping[str, Any],
    review_stage: Mapping[str, Any],
    review_mode: str,
) -> str:
    model = model_stage_values(stage)
    if model["not_applicable"]:
        model_judgment = "not_applicable"
    elif model["prediction_unavailable"]:
        model_judgment = "unavailable"
    else:
        model_judgment = f"{model['relation']} / {model['gap']}"

    decision = review_stage["decision"]
    relation = review_stage.get("relation")
    gap = review_stage.get("gap")
    if decision == "confirmed":
        human_judgment = f"{model['relation']} / {model['gap']} (confirmed original)"
    elif decision == "not_applicable":
        human_judgment = "not_applicable"
    elif decision == "insufficient_evidence":
        human_judgment = "unknown"
    else:
        human_judgment = f"{relation} / {gap}"
    evidence_ids = ", ".join(model["evidence_ids"]) or "none"
    return "\n".join(
        (
            f"Source run: {run_dir}",
            f"Review mode: {review_mode}",
            "Interpretation: nonblind; does not establish model accuracy",
            f"Stage: {code}",
            f"Model judgment: {model_judgment}",
            f"Model reason: {model['reason'] or 'none'}",
            f"Human decision: {decision}",
            f"Human relation / gap: {human_judgment}",
            f"Human note: {review_stage.get('note') or 'none'}",
            f"Evidence IDs: {evidence_ids}",
        )
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cases: list[str] = []
    for input_run_dir in args.run_dir:
        run_dir = input_run_dir.expanduser().resolve()
        try:
            review = load_review(run_dir)
            analysis = load_analysis(run_dir)
            stages = analysis_stage_map(analysis)
            for code in STAGE_CODES:
                if args.stage and args.stage != code:
                    continue
                review_stage = review["stages"][code]
                if review_stage["decision"] not in FINAL_DECISIONS:
                    continue
                rendered = _render_case(
                    run_dir,
                    code,
                    stages[code],
                    review_stage,
                    str(review["review_mode"]),
                )
                if args.query is None or args.query in rendered:
                    cases.append(rendered)
        except (HumanReviewError, OSError, TypeError, ValueError) as exc:
            print(f"Invalid run {run_dir}: {exc}", file=sys.stderr)
            return 2

    if cases:
        print("\n\n".join(cases))
    else:
        print("No final human-reviewed cases matched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
