#!/usr/bin/env python3
"""Run the frozen protocol's model-independent judgment layer.

This runner consumes completed ``visual_extraction_evaluation.json`` artifacts
from the same model/sample cohort. It never loads human GT or human_initial,
and it does not modify production artifacts. The judgment call receives the
model's own locked visual facts, so extraction and judgment remain separately
auditable while the resulting artifact still represents that model's
end-to-end reasoning path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
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
    EVALUATION_ROLES,
    MODEL_INDEPENDENT_SCHEMA_VERSION,
    VISUAL_EXTRACTION_SCHEMA_VERSION,
    contract_limits_for_variant,
    build_model_owned_fact_bundle,
    frozen_raw_video_source_identity,
    load_frozen_visual_bundle,
    run_model_independent_evaluation,
    validate_visual_extraction_result,
)
from flayr_core.utils import write_json  # noqa: E402
from flayr_core.report_metadata import current_code_commit  # noqa: E402


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value).strip())
    return cleaned.strip("._") or "unnamed"


def _safe_component_map(values: list[str], *, label: str) -> dict[str, str]:
    """Reject sanitized-name collisions before reading or writing artifacts."""
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clear_judgment_outputs(output_dir: Path) -> None:
    """Remove stale outputs before writing a new judgment attempt."""
    for name in (
        "model_independent_evaluation.json",
        "model_independent_failure.json",
        "model_independent_blocked.json",
        "model_independent_input_metadata.json",
        "raw_model_response.json",
        ".compact-request.json",
    ):
        (output_dir / name).unlink(missing_ok=True)


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


def _read_extraction(path: Path) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if not path.is_file():
        return None, {"status": "missing", "artifact": str(path)}
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, {"status": "invalid_artifact", "artifact": str(path), "error": str(exc)[:500]}
    if not isinstance(record, dict):
        return None, {"status": "invalid_artifact", "artifact": str(path), "error": "root is not an object"}
    if record.get("status") != "completed" or not isinstance(record.get("result"), dict):
        return None, {
            "status": str(record.get("status") or "invalid"),
            "artifact": str(path),
            "error": str(record.get("error") or record.get("errors") or "no completed result")[:1000],
        }
    if (
        record.get("schema_version") != VISUAL_EXTRACTION_SCHEMA_VERSION
        or record["result"].get("schema_version") != VISUAL_EXTRACTION_SCHEMA_VERSION
    ):
        return None, {
            "status": "incompatible_schema",
            "artifact": str(path),
            "schema_version": record.get("schema_version"),
            "result_schema_version": record["result"].get("schema_version"),
            "expected_schema_version": VISUAL_EXTRACTION_SCHEMA_VERSION,
            "source_digest": record.get("source_digest"),
            "source_run": record.get("source_run"),
            "video_role_order": record.get("video_role_order"),
            "video_source_sha256": record.get("video_source_sha256"),
            "source_model": record.get("model"),
        }
    roles = record.get("video_role_order")
    durations = record.get("video_source_duration_seconds")
    metadata_errors: list[str] = []
    if (
        not isinstance(roles, list)
        or len(roles) != 2
        or any(not isinstance(role, str) for role in roles)
        or set(roles) != {"creator", "benchmark"}
    ):
        metadata_errors.append("video_role_order must contain creator and benchmark exactly once")
    if not isinstance(durations, list) or len(durations) != 2:
        metadata_errors.append("video_source_duration_seconds must contain two durations")
    source_durations: dict[str, float] = {}
    if not metadata_errors and isinstance(roles, list) and isinstance(durations, list):
        for role, raw_duration in zip(roles, durations):
            try:
                duration = float(raw_duration)
            except (TypeError, ValueError):
                metadata_errors.append(f"invalid duration for {role}")
                continue
            if not math.isfinite(duration) or duration <= 0:
                metadata_errors.append(f"duration for {role} must be finite and positive")
            else:
                source_durations[str(role)] = duration
    if metadata_errors:
        return None, {
            "status": "invalid_contract",
            "artifact": str(path),
            "schema_version": record.get("schema_version"),
            "result_schema_version": record["result"].get("schema_version"),
            "expected_schema_version": VISUAL_EXTRACTION_SCHEMA_VERSION,
            "contract_errors": metadata_errors[:20],
            "source_digest": record.get("source_digest"),
            "source_run": record.get("source_run"),
            "video_role_order": roles,
            "video_source_duration_seconds": durations,
            "video_source_sha256": record.get("video_source_sha256"),
            "source_model": record.get("model"),
        }
    contract_errors = validate_visual_extraction_result(
        record["result"],
        expected_roles=("creator", "benchmark"),
        source_durations=source_durations,
    )
    if contract_errors:
        return None, {
            "status": "invalid_contract",
            "artifact": str(path),
            "schema_version": record.get("schema_version"),
            "result_schema_version": record["result"].get("schema_version"),
            "expected_schema_version": VISUAL_EXTRACTION_SCHEMA_VERSION,
            "contract_errors": contract_errors[:20],
            "source_digest": record.get("source_digest"),
            "source_run": record.get("source_run"),
            "video_role_order": record.get("video_role_order"),
            "video_source_sha256": record.get("video_source_sha256"),
            "source_model": record.get("model"),
        }
    digest = _sha256(path)
    return record["result"], {
        "status": "completed",
        "artifact": str(path),
        "artifact_sha256": digest,
        "source_digest": record.get("source_digest"),
        "source_run": record.get("source_run"),
        "schema_version": record.get("schema_version"),
        "video_role_order": record.get("video_role_order"),
        "video_source_sha256": record.get("video_source_sha256"),
        "source_model": record.get("model"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run model-independent comparison judgments over locked model-owned facts.",
        allow_abbrev=False,
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True, help="Root containing visual extraction artifacts.")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--models", nargs="+", required=True)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--api-key-env", default="DASHSCOPE_API_KEY")
    parser.add_argument("--keychain-service", default=None)
    parser.add_argument("--keychain-account", default="API_KEY")
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
    parser.add_argument(
        "--max-stage-evidence-ids",
        type=int,
        default=None,
        help="仅用于4→8等单变量引用上限实验；默认使用合同值4。",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.request_timeout_seconds < 60 or args.request_timeout_seconds > 1800:
        raise SystemExit("--request-timeout-seconds must be between 60 and 1800")
    if args.max_stage_evidence_ids is not None and not 1 <= args.max_stage_evidence_ids <= 16:
        raise SystemExit("--max-stage-evidence-ids must be between 1 and 16")
    manifest = _read_manifest(args.manifest.expanduser().resolve())
    sample_components = _safe_component_map(
        [sample["sample_id"] for sample in manifest],
        label="sample_id",
    )
    model_components = _safe_component_map(args.models, label="model")
    raw_root = args.raw_root.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    contract_limits = contract_limits_for_variant("model_independent")
    if args.max_stage_evidence_ids is not None:
        contract_limits["max_stage_evidence_ids"] = args.max_stage_evidence_ids
    write_json(
        output_root / "protocol_metadata.json",
        {
            "schema_version": 1,
            "protocol": "two_layer_human_initial_model_independent",
            "evaluation_role": args.evaluation_role,
            "variant": "model_independent",
            "promotion_eligible": False,
            "human_initial_loaded": False,
            "gt_loaded": False,
            "models": list(args.models),
            "manifest": str(args.manifest.expanduser().resolve()),
            "raw_extraction_root": str(raw_root),
            "output_budget": args.output_budget,
            "output_budget_field": args.output_budget_field,
            "judgment_schema_version": MODEL_INDEPENDENT_SCHEMA_VERSION,
            "contract_limits": {**contract_limits, "output_budget": args.output_budget},
            "api_endpoint_identity": args.api_url,
            "source_commit": current_code_commit(),
        },
    )

    api_key_args = SimpleNamespace(
        llm_api_key_env=args.api_key_env,
        llm_api_key_keychain_service=args.keychain_service,
        llm_api_key_keychain_account=args.keychain_account,
    )
    preflight: list[tuple[dict[str, str], Any, dict[str, Any]]] = []
    for sample in manifest:
        base_bundle = load_frozen_visual_bundle(Path(sample["run_dir"]), include_images=False)
        source_identity = frozen_raw_video_source_identity(Path(sample["run_dir"]))
        preflight.append((sample, base_bundle, source_identity))

    summary: dict[str, Any] = {
        "schema_version": 1,
        "protocol": "two_layer_human_initial_model_independent",
        "evaluation_role": args.evaluation_role,
        "variant": "model_independent",
        "promotion_eligible": False,
        "human_initial_loaded": False,
        "gt_loaded": False,
        "models": list(args.models),
        "source_commit": current_code_commit(),
        "judgment_schema_version": MODEL_INDEPENDENT_SCHEMA_VERSION,
        "contract_limits": {**contract_limits, "output_budget": args.output_budget},
        "samples": [],
    }
    for sample, base_bundle, source_identity in preflight:
        sample_record: dict[str, Any] = {
            "sample_id": sample["sample_id"],
            "run_dir": sample["run_dir"],
            "base_source_digest": base_bundle.source_digest,
            "results": [],
        }
        for model in args.models:
            extraction_path = raw_root / sample_components[sample["sample_id"]] / model_components[model] / "visual_extraction_evaluation.json"
            extraction_result, extraction_meta = _read_extraction(extraction_path)
            output_dir = output_root / sample_components[sample["sample_id"]] / model_components[model]
            output_dir.mkdir(parents=True, exist_ok=True)
            _clear_judgment_outputs(output_dir)
            write_json(
                output_dir / "model_independent_input_metadata.json",
                {
                    "schema_version": 1,
                    "sample_id": sample["sample_id"],
                    "judgment_model": model,
                    "human_initial_loaded": False,
                    "gt_loaded": False,
                    "source_extraction": extraction_meta,
                    "base_source_digest": base_bundle.source_digest,
                    "expected_source_run": Path(sample["run_dir"]).name,
                    "expected_video_role_order": source_identity["video_role_order"],
                    "expected_video_source_sha256": source_identity["video_source_sha256"],
                },
            )
            expected_source_run = Path(sample["run_dir"]).name
            actual_video_identity = {
                "video_role_order": extraction_meta.get("video_role_order"),
                "video_source_sha256": extraction_meta.get("video_source_sha256"),
            }
            if extraction_result is not None and (
                extraction_meta.get("source_run") != expected_source_run
                or actual_video_identity != source_identity
            ):
                mismatch = {
                    "status": "blocked_source_identity_mismatch",
                    "sample_id": sample["sample_id"],
                    "model": model,
                    "human_initial_loaded": False,
                    "gt_loaded": False,
                    "expected_source_run": expected_source_run,
                    "actual_source_run": extraction_meta.get("source_run"),
                    "expected_video_identity": source_identity,
                    "actual_video_identity": actual_video_identity,
                    "source_extraction": extraction_meta,
                }
                write_json(output_dir / "model_independent_blocked.json", mismatch)
                sample_record["results"].append(
                    {
                        "model": model,
                        "status": mismatch["status"],
                        "source_extraction_status": extraction_meta.get("status"),
                    }
                )
                continue
            if extraction_result is None:
                blocked = {
                    "status": "blocked_upstream_extraction",
                    "sample_id": sample["sample_id"],
                    "model": model,
                    "human_initial_loaded": False,
                    "gt_loaded": False,
                    "source_extraction": extraction_meta,
                }
                write_json(output_dir / "model_independent_blocked.json", blocked)
                sample_record["results"].append(
                    {
                        "model": model,
                        "status": blocked["status"],
                        "source_extraction_status": extraction_meta.get("status"),
                    }
                )
                continue
            try:
                bundle = build_model_owned_fact_bundle(
                    base_bundle,
                    extraction_result,
                    extraction_artifact=str(extraction_path),
                )
                result = run_model_independent_evaluation(
                    model=model,
                    bundle=bundle,
                    output_dir=output_dir,
                    api_url=args.api_url,
                    api_key_args=api_key_args,
                    output_budget=args.output_budget,
                    output_budget_field=args.output_budget_field,
                    request_timeout_seconds=args.request_timeout_seconds,
                    evaluation_role=args.evaluation_role,
                    max_stage_evidence_ids=args.max_stage_evidence_ids,
                )
                sample_record["results"].append(
                    {
                        "model": model,
                        "status": result.get("status"),
                        "error": result.get("error"),
                        "errors": result.get("errors"),
                        "source_extraction_status": extraction_meta.get("status"),
                    }
                )
            except (CompactEvaluationError, OSError, json.JSONDecodeError) as exc:
                failure = {
                    "status": "model_independent_preflight_failed",
                    "sample_id": sample["sample_id"],
                    "model": model,
                    "human_initial_loaded": False,
                    "gt_loaded": False,
                    "error": str(exc)[:1000],
                    "source_extraction": extraction_meta,
                }
                write_json(output_dir / "model_independent_blocked.json", failure)
                sample_record["results"].append(
                    {
                        "model": model,
                        "status": failure["status"],
                        "error": failure["error"],
                        "source_extraction_status": extraction_meta.get("status"),
                    }
                )
        summary["samples"].append(sample_record)
    write_json(output_root / "cohort_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    statuses = [
        item["status"]
        for sample in summary["samples"]
        for item in sample["results"]
    ]
    return 0 if statuses and all(status == "completed" for status in statuses) else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CompactEvaluationError as exc:
        raise SystemExit(str(exc)) from exc
