#!/usr/bin/env python3
"""Materialize audited S4 qualification states for seen-sample mechanism tests."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm.compact_eval import (  # noqa: E402
    CompactEvaluationError,
    S4_FACT_STATE_SCHEMA_VERSION,
    load_model_owned_fact_artifact,
    s4_qualification_gradient_compression,
    validate_s4_qualification_review,
)
from flayr_core.report_metadata import current_code_commit  # noqa: E402
from flayr_core.utils import write_json  # noqa: E402


def _stable_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CompactEvaluationError(f"invalid S4 qualification manifest: {path}: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("cases"), list) or not value["cases"]:
        raise CompactEvaluationError("S4 qualification manifest must contain a non-empty cases list")
    if value.get("decision_scope") != "seen_mechanism_calibration_only":
        raise CompactEvaluationError("S4 qualification manifest is not limited to seen mechanism calibration")
    return value


def build_reviewed_states(
    manifest_path: Path,
    output_root: Path,
    *,
    judgment_model: str,
) -> dict[str, Any]:
    manifest_path = manifest_path.expanduser().resolve()
    manifest = _read_manifest(manifest_path)
    output_root = output_root.expanduser().resolve()
    manifest_root = manifest_path.parent
    fact_source_model = str(manifest.get("fact_source_model") or "").strip()
    if not fact_source_model:
        raise CompactEvaluationError("S4 qualification manifest is missing fact_source_model")

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, case in enumerate(manifest["cases"]):
        if not isinstance(case, dict):
            raise CompactEvaluationError(f"qualification case {index} must be an object")
        sample_id = str(case.get("sample_id") or "").strip()
        if not sample_id or sample_id in seen:
            raise CompactEvaluationError(f"qualification case has missing or duplicate sample_id: {sample_id!r}")
        seen.add(sample_id)
        raw_artifact = Path(str(case.get("fact_artifact") or ""))
        artifact_path = raw_artifact if raw_artifact.is_absolute() else manifest_root / raw_artifact
        bundle = load_model_owned_fact_artifact(
            artifact_path,
            expected_model=fact_source_model,
            expected_source_run=sample_id,
        )
        corrected_result = case.get("corrected_result")
        review = case.get("qualification_review")
        if not isinstance(review, dict) or review.get("sample_id") != sample_id:
            raise CompactEvaluationError(
                f"qualification review sample_id does not match case identity: {sample_id}"
            )
        errors = validate_s4_qualification_review(review, corrected_result, bundle)
        if errors:
            raise CompactEvaluationError(
                f"invalid reviewed qualification for {sample_id}: " + "; ".join(errors[:12])
            )
        review_digest = _stable_digest(review)
        result_digest = _stable_digest(corrected_result)
        protocol_hash = _stable_digest(
            {
                "schema_version": manifest.get("schema_version"),
                "qualification_contract": manifest.get("qualification_contract"),
                "review_digest": review_digest,
                "result_digest": result_digest,
            }
        )
        record = {
            "status": "completed",
            "variant": "s4_fact_state",
            "schema_version": S4_FACT_STATE_SCHEMA_VERSION,
            "state_owner": "human_review",
            "model": "human_review",
            "compatible_judgment_models": [judgment_model],
            "source_run": sample_id,
            "source_digest": bundle.source_digest,
            "source_commit": current_code_commit(),
            "protocol_hash": protocol_hash,
            "qualification_review_digest": review_digest,
            "qualification_result_digest": result_digest,
            "promotion_eligible": False,
            "decision_scope": "seen_mechanism_calibration_only",
            "comparison_sufficiency": review["comparison_sufficiency"],
            "gradient_compression_detected": s4_qualification_gradient_compression(
                review,
                corrected_result,
            ),
            "qualification_review": review,
            "result": corrected_result,
        }
        output_path = output_root / sample_id / judgment_model / "s4_fact_state_evaluation.json"
        write_json(output_path, record)
        rows.append(
            {
                "sample_id": sample_id,
                "status": "completed",
                "comparison_sufficiency": review["comparison_sufficiency"],
                "gradient_compression_detected": record["gradient_compression_detected"],
                "output": str(output_path),
            }
        )

    summary = {
        "status": "completed",
        "decision_scope": "seen_mechanism_calibration_only",
        "promotion_eligible": False,
        "manifest": str(manifest_path),
        "judgment_model": judgment_model,
        "sample_count": len(rows),
        "sufficient_count": sum(row["comparison_sufficiency"] == "sufficient" for row in rows),
        "gradient_compression_count": sum(row["gradient_compression_detected"] for row in rows),
        "rows": rows,
    }
    write_json(output_root / "qualification_summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--judgment-model", default="qwen3.7-plus")
    args = parser.parse_args()
    try:
        summary = build_reviewed_states(
            args.manifest,
            args.output_root,
            judgment_model=args.judgment_model,
        )
    except CompactEvaluationError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
