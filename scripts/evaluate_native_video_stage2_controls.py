#!/usr/bin/env python3
"""Compare Stage2 judgment with fixed facts plus frames versus fixed facts plus video.

This is a calibration-only control. The model-produced facts are loaded once
from a completed current extraction run and are identical in both conditions.
Only the additional visual representation is changed. No human GT is sent to
the model and no production artifact is written.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm.compact_eval import (  # noqa: E402
    CompactEvaluationError,
    FrozenCompactBundle,
    build_model_owned_fact_bundle,
    frozen_raw_video_source_identity,
    load_frozen_video_bundle,
    load_frozen_visual_bundle,
    run_model_independent_evaluation,
    validate_visual_extraction_result,
    _stable_digest,
)
from flayr_core.utils import write_json  # noqa: E402


def _safe_component(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "._-" else "_" for char in str(value).strip())
    return cleaned.strip("._") or "unnamed"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CompactEvaluationError(f"invalid JSON artifact: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CompactEvaluationError(f"JSON artifact root must be an object: {path}")
    return value


def _read_manifest(path: Path) -> list[dict[str, str]]:
    data = _read_json(path)
    rows = data.get("samples")
    if not isinstance(rows, list) or not rows:
        raise CompactEvaluationError("manifest must contain a non-empty samples list")
    root = path.parent.resolve()
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise CompactEvaluationError(f"manifest sample {index} must be an object")
        sample_id = str(row.get("sample_id") or "").strip()
        raw_run_dir = str(row.get("run_dir") or "").strip()
        if not sample_id or not raw_run_dir:
            raise CompactEvaluationError(f"manifest sample {index} needs sample_id and run_dir")
        if sample_id in seen:
            raise CompactEvaluationError(f"manifest contains duplicate sample_id: {sample_id}")
        seen.add(sample_id)
        run_dir = Path(raw_run_dir).expanduser()
        if not run_dir.is_absolute():
            run_dir = root / run_dir
        result.append({"sample_id": sample_id, "run_dir": str(run_dir.resolve())})
    return result


def _load_current_facts(
    *,
    extraction_root: Path,
    sample_id: str,
    model: str,
    representation_run: Path,
) -> dict[str, Any]:
    artifact = (
        extraction_root
        / _safe_component(sample_id)
        / _safe_component(model)
        / "visual_extraction_evaluation.json"
    )
    record = _read_json(artifact)
    if record.get("status") != "completed":
        raise CompactEvaluationError(f"current extraction is not completed: {artifact}")
    if record.get("model") != model:
        raise CompactEvaluationError(f"current extraction model mismatch: {artifact}")
    result = record.get("result")
    if not isinstance(result, dict):
        raise CompactEvaluationError(f"current extraction has no result object: {artifact}")
    source_identity = frozen_raw_video_source_identity(representation_run)
    if record.get("video_role_order") != source_identity["video_role_order"]:
        raise CompactEvaluationError(f"current extraction role order mismatch: {artifact}")
    if record.get("video_source_sha256") != source_identity["video_source_sha256"]:
        raise CompactEvaluationError(f"current extraction video identity mismatch: {artifact}")
    durations: dict[str, float] = {}
    roles = record.get("video_role_order")
    raw_durations = record.get("video_source_duration_seconds")
    if not isinstance(roles, list) or not isinstance(raw_durations, list) or len(roles) != len(raw_durations):
        raise CompactEvaluationError(f"current extraction duration metadata is invalid: {artifact}")
    for role, raw_duration in zip(roles, raw_durations):
        try:
            durations[str(role)] = float(raw_duration)
        except (TypeError, ValueError) as exc:
            raise CompactEvaluationError(f"current extraction duration is invalid: {artifact}") from exc
    errors = validate_visual_extraction_result(
        result,
        expected_roles=("creator", "benchmark"),
        source_durations=durations,
    )
    if errors:
        raise CompactEvaluationError(
            f"current extraction fails the current contract: {artifact}: " + "; ".join(errors[:8])
        )
    return result


def _attach_representation(
    fact_bundle: FrozenCompactBundle,
    representation_bundle: FrozenCompactBundle,
    *,
    mode: str,
) -> FrozenCompactBundle:
    if mode not in {"model_owned_facts_and_frames", "model_owned_facts_and_video"}:
        raise CompactEvaluationError(f"unsupported Stage2 representation mode: {mode}")
    context = dict(fact_bundle.context)
    context["experiment_boundary"] = (
        str(context.get("experiment_boundary") or "")
        + " 本次只改变判断调用附带的视觉表征；锁定事实包、模型、合同和GT隔离规则不变。"
    )
    context["representation_control"] = {
        "mode": mode,
        "representation_source_digest": representation_bundle.source_digest,
        "facts_source_digest": fact_bundle.source_digest,
        "human_initial_loaded": False,
        "gt_loaded": False,
    }
    source_identity = {
        "base_facts_digest": fact_bundle.source_digest,
        "representation_digest": representation_bundle.source_digest,
        "input_mode": mode,
        "visual_inputs": [
            {key: item.get(key) for key in ("label", "path", "data_url_sha256")}
            for item in representation_bundle.visual_inputs
        ],
        "video_inputs": [
            {key: item.get(key) for key in ("role", "label", "path", "sha256", "data_url_sha256", "duration_seconds")}
            for item in representation_bundle.video_inputs
        ],
    }
    return replace(
        fact_bundle,
        context=context,
        source_digest=_stable_digest(source_identity),
        input_mode=mode,
        visual_inputs=representation_bundle.visual_inputs,
        video_inputs=representation_bundle.video_inputs,
    )


def _preflight(
    *,
    manifest: list[dict[str, str]],
    extraction_root: Path,
    representation_root: Path,
    model: str,
    video_cache_root: Path,
) -> list[tuple[str, FrozenCompactBundle, FrozenCompactBundle]]:
    prepared: list[tuple[str, FrozenCompactBundle, FrozenCompactBundle]] = []
    for row in manifest:
        sample_id = row["sample_id"]
        representation_run = representation_root / _safe_component(sample_id)
        if not representation_run.is_dir():
            raise CompactEvaluationError(f"representation run is missing: {representation_run}")
        base = load_frozen_visual_bundle(representation_run, include_images=False)
        extraction = _load_current_facts(
            extraction_root=extraction_root,
            sample_id=sample_id,
            model=model,
            representation_run=representation_run,
        )
        facts = build_model_owned_fact_bundle(
            base,
            extraction,
            extraction_artifact=str(
                extraction_root
                / _safe_component(sample_id)
                / _safe_component(model)
                / "visual_extraction_evaluation.json"
            ),
        )
        frames = load_frozen_visual_bundle(representation_run, include_images=True)
        video = load_frozen_video_bundle(
            representation_run,
            cache_dir=video_cache_root / _safe_component(sample_id),
        )
        prepared.append(
            (
                sample_id,
                _attach_representation(facts, frames, mode="model_owned_facts_and_frames"),
                _attach_representation(facts, video, mode="model_owned_facts_and_video"),
            )
        )
        print(
            f"preflight {sample_id}: facts={facts.source_digest[:12]} "
            f"frames={len(frames.visual_inputs)} video={len(video.video_inputs)}"
        )
    return prepared


def _completed_result(output_dir: Path) -> dict[str, Any] | None:
    """Return a prior completed result so a resumed run never spends a call twice."""
    path = output_dir / "model_independent_evaluation.json"
    if not path.is_file():
        return None
    record = _read_json(path)
    if record.get("status") != "completed" or not isinstance(record.get("result"), dict):
        return None
    return record


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--extraction-root", type=Path, required=True)
    parser.add_argument("--representation-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--model", default="qwen3.7-plus")
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--api-key-env", default="DASHSCOPE_API_KEY")
    parser.add_argument("--output-budget", type=int, default=8192)
    parser.add_argument("--output-budget-field", choices=("max_tokens", "max_completion_tokens"), default="max_completion_tokens")
    parser.add_argument("--request-timeout-seconds", type=int, default=900)
    parser.add_argument("--preflight-only", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest = _read_manifest(args.manifest.expanduser().resolve())
    extraction_root = args.extraction_root.expanduser().resolve()
    representation_root = args.representation_root.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    video_cache_root = output_root / "video-cache"
    prepared = _preflight(
        manifest=manifest,
        extraction_root=extraction_root,
        representation_root=representation_root,
        model=args.model,
        video_cache_root=video_cache_root,
    )
    if args.preflight_only:
        print(f"preflight complete: {len(prepared)} samples, 2 conditions each")
        return 0
    api_key_args = SimpleNamespace(
        llm_api_key_env=args.api_key_env,
        llm_api_key_keychain_service=None,
        llm_api_key_keychain_account="API_KEY",
    )
    output_root.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "schema_version": 1,
        "experiment": "native_video_vs_frames_stage2_judgment",
        "evaluation_role": "model_calibration",
        "promotion_eligible": False,
        "model": args.model,
        "output_budget": args.output_budget,
        "output_budget_field": args.output_budget_field,
        "request_timeout_seconds": args.request_timeout_seconds,
        "facts_source": str(extraction_root),
        "representation_source": str(representation_root),
        "conditions": {
            "frames": "same locked facts plus frozen stage frames",
            "video": "same locked facts plus bounded original videos",
        },
        "samples": [],
    }
    for sample_id, frames_bundle, video_bundle in prepared:
        sample_record = {"sample_id": sample_id, "conditions": []}
        for condition, bundle in (("frames", frames_bundle), ("video", video_bundle)):
            condition_dir = output_root / _safe_component(sample_id) / condition / _safe_component(args.model)
            prior = _completed_result(condition_dir)
            if prior is not None:
                sample_record["conditions"].append(
                    {
                        "condition": condition,
                        "status": "completed",
                        "source_digest": prior.get("source_digest"),
                        "input_mode": prior.get("input_mode"),
                        "resumed": True,
                    }
                )
                print(f"{sample_id} {condition}: completed (reused; no new call)")
                continue
            result = run_model_independent_evaluation(
                model=args.model,
                bundle=bundle,
                output_dir=condition_dir,
                api_url=args.api_url,
                api_key_args=api_key_args,
                output_budget=args.output_budget,
                output_budget_field=args.output_budget_field,
                request_timeout_seconds=args.request_timeout_seconds,
                evaluation_role="model_calibration",
            )
            sample_record["conditions"].append(
                {
                    "condition": condition,
                    "status": result.get("status"),
                    "source_digest": bundle.source_digest,
                    "input_mode": bundle.input_mode,
                    "error": result.get("error"),
                    "failure_class": result.get("failure_class"),
                }
            )
            print(f"{sample_id} {condition}: {result.get('status')}")
            if result.get("status") != "completed":
                print("stopping after first failed condition; no retry or alternate call was made")
                summary["samples"].append(sample_record)
                write_json(output_root / "cohort_summary.json", summary)
                return 2
        summary["samples"].append(sample_record)
    write_json(output_root / "cohort_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CompactEvaluationError as exc:
        raise SystemExit(str(exc)) from exc
