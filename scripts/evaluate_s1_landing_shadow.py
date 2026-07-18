#!/usr/bin/env python3
"""用已有 Stage1 事实与开头感官素材，独立验证 S1 Landing shadow。"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm.api import call_llm_api, extract_chat_completion_text, read_llm_api_key
from flayr_core.llm.json_codec import parse_json_text
from flayr_core.llm.parse import hook_reason_window_leaks, normalize_landing_shadow
from flayr_core.llm.payload import build_s1_landing_shadow_payload
from flayr_core.utils import write_json

API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
MODEL = "qwen3.5-omni-plus"
KEYCHAIN_SERVICE = "VidLingo.Qwen"
CONDITIONS = (
    "immediately_understandable",
    "singular_and_concrete",
    "creates_stay_motivation",
    "effectively_received",
)


def load_run(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    analysis = json.loads((run_dir / "analysis.json").read_text(encoding="utf-8"))
    foundation = run_dir / "product_foundation.json"
    if foundation.is_file():
        analysis["product_foundation"] = json.loads(foundation.read_text(encoding="utf-8"))
    facts = {
        role: json.loads((run_dir / f"video_facts_{role}.json").read_text(encoding="utf-8"))
        for role in ("creator", "benchmark")
    }
    return analysis, facts


def normalize_side(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    shadow_source = source.get("landing_conditions") if isinstance(source.get("landing_conditions"), dict) else {}
    normalized = normalize_landing_shadow(
        {
            **shadow_source,
            "stay_motivation_mechanism": source.get("stay_motivation_mechanism"),
            "landing_shadow_reason": source.get("landing_shadow_reason"),
        }
    )
    try:
        boundary = float(source.get("hook_boundary_seconds"))
    except (TypeError, ValueError):
        boundary = None
    shadow_reason = str(normalized.get("landing_shadow_reason") or "").strip()
    return {
        "hook_boundary_seconds": boundary,
        "hook_boundary_reason": str(source.get("hook_boundary_reason") or "").strip(),
        "s2_start_signal": str(source.get("s2_start_signal") or "").strip(),
        **normalized,
        "landing_shadow_window_leak": hook_reason_window_leaks(shadow_reason, boundary),
    }


def validate_side(side: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if side.get("hook_boundary_seconds") is None:
        errors.append("missing_hook_boundary_seconds")
    if not side.get("hook_boundary_reason"):
        errors.append("missing_hook_boundary_reason")
    if not side.get("s2_start_signal"):
        errors.append("missing_s2_start_signal")
    if side.get("landing_shadow_met") is None:
        errors.append("incomplete_landing_conditions")
    if not side.get("landing_shadow_reason"):
        errors.append("missing_landing_shadow_reason")
    if side.get("landing_shadow_window_leak") is True:
        errors.append("landing_shadow_window_leak")
    return errors


def run_once(
    sample: dict[str, Any],
    repeat_index: int,
    api_key: str,
    output_dir: Path,
    retries: int,
) -> dict[str, Any]:
    sample_id = str(sample["id"])
    run_dir = ROOT / str(sample["run_dir"])
    analysis, facts = load_run(run_dir)
    payload = build_s1_landing_shadow_payload(MODEL, analysis, facts)
    stem = f"{sample_id}-{repeat_index:02d}"
    request_path = output_dir / f"{stem}.request.json"
    response_path = output_dir / f"{stem}.response.json"
    write_json(request_path, payload)
    try:
        raw_text = call_llm_api(
            API_URL,
            api_key,
            request_path,
            response_path,
            retries=retries,
            max_time_seconds=600,
        )
        raw = json.loads(raw_text)
        parsed = parse_json_text(extract_chat_completion_text(raw))
        creator = normalize_side(parsed.get("creator"))
        benchmark = normalize_side(parsed.get("benchmark"))
        contract_errors = [
            *(f"creator:{item}" for item in validate_side(creator)),
            *(f"benchmark:{item}" for item in validate_side(benchmark)),
        ]
        return {
            "sample_id": sample_id,
            "repeat_index": repeat_index,
            "ok": not contract_errors,
            "creator": creator,
            "benchmark": benchmark,
            "contract_errors": contract_errors,
            "response_path": str(response_path),
        }
    except (OSError, ValueError, json.JSONDecodeError, SystemExit) as exc:
        return {"sample_id": sample_id, "repeat_index": repeat_index, "ok": False, "error": str(exc)}


def consistency(values: list[Any]) -> float | None:
    usable = [value for value in values if value is not None]
    if not usable:
        return None
    return max(Counter(usable).values()) / len(usable)


def modal_value(values: list[Any]) -> Any:
    usable = [value for value in values if value is not None]
    return Counter(usable).most_common(1)[0][0] if usable else None


def summarize(manifest: dict[str, Any], records: list[dict[str, Any]], repeat: int) -> dict[str, Any]:
    by_sample: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_sample[str(record["sample_id"])].append(record)
    sample_summaries: list[dict[str, Any]] = []
    labeled_correct = 0
    labeled_total = 0
    false_kills = 0
    pass_controls_evaluated = 0
    old_true_corrected = 0
    old_true_failures = 0
    for sample in manifest.get("samples") or []:
        sample_id = str(sample["id"])
        successes = [record for record in by_sample[sample_id] if record.get("ok")]
        role_summary: dict[str, Any] = {}
        for role in ("creator", "benchmark"):
            sides = [record[role] for record in successes]
            condition_summary = {
                condition: {
                    "mode": modal_value([side["landing_conditions"].get(condition) for side in sides]),
                    "consistency": consistency([side["landing_conditions"].get(condition) for side in sides]),
                }
                for condition in CONDITIONS
            }
            shadow_values = [side.get("landing_shadow_met") for side in sides]
            boundaries = [side.get("hook_boundary_seconds") for side in sides if side.get("hook_boundary_seconds") is not None]
            role_summary[role] = {
                "landing_shadow_mode": modal_value(shadow_values),
                "landing_shadow_consistency": consistency(shadow_values),
                "conditions": condition_summary,
                "boundary_min": min(boundaries) if boundaries else None,
                "boundary_max": max(boundaries) if boundaries else None,
                "reasons": [side.get("landing_shadow_reason") for side in sides],
            }
        expected = sample.get("expected_shadow_met")
        predicted = role_summary["creator"]["landing_shadow_mode"]
        matches = predicted == expected if isinstance(expected, bool) and predicted is not None else None
        if matches is not None:
            labeled_total += 1
            labeled_correct += int(matches)
            false_kills += int(expected is True and predicted is False)
            pass_controls_evaluated += int(expected is True)
        run_analysis = json.loads((ROOT / str(sample["run_dir"]) / "analysis.json").read_text(encoding="utf-8"))
        old_stage = next((stage for stage in run_analysis.get("stage_analysis") or [] if str(stage.get("stage") or "").startswith("S1")), {})
        old_landing = (old_stage.get("creator_hook") or {}).get("landing_met")
        if expected is False and old_landing is True:
            old_true_failures += 1
            old_true_corrected += int(predicted is False)
        sample_summaries.append(
            {
                **sample,
                "successful_repeats": len(successes),
                "requested_repeats": repeat,
                "old_creator_landing_met": old_landing,
                "matches_expected": matches,
                **role_summary,
            }
        )
    consistency_values = [
        summary[role]["landing_shadow_consistency"]
        for summary in sample_summaries
        for role in ("creator", "benchmark")
        if summary[role]["landing_shadow_consistency"] is not None
    ]
    complete_samples = sum(
        1 for summary in sample_summaries if summary["successful_repeats"] == repeat
    )
    stable_samples = sum(
        1
        for summary in sample_summaries
        if summary["successful_repeats"] == repeat
        and all(
            (summary[role]["landing_shadow_consistency"] or 0) >= 0.8
            for role in ("creator", "benchmark")
        )
    )
    labeled_accuracy = labeled_correct / labeled_total if labeled_total else None
    false_kill_rate = false_kills / pass_controls_evaluated if pass_controls_evaluated else None
    correction_rate = old_true_corrected / old_true_failures if old_true_failures else None
    mean_consistency = sum(consistency_values) / len(consistency_values) if consistency_values else None
    ready_for_promotion = bool(
        len(records) > 0
        and sum(1 for record in records if record.get("ok")) == len(records)
        and labeled_accuracy is not None
        and labeled_accuracy >= 0.85
        and false_kill_rate is not None
        and false_kill_rate <= 0.15
        and mean_consistency is not None
        and mean_consistency >= 0.8
        and stable_samples == len(sample_summaries)
    )
    return {
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "model": MODEL,
        "repeat": repeat,
        "metrics": {
            "successful_calls": sum(1 for record in records if record.get("ok")),
            "requested_calls": len(records),
            "labeled_accuracy": labeled_accuracy,
            "labeled_count": labeled_total,
            "pass_controls_evaluated": pass_controls_evaluated,
            "false_kill_rate_on_pass_controls": false_kill_rate,
            "old_true_failure_correction_rate": correction_rate,
            "old_true_failure_count": old_true_failures,
            "mean_shadow_consistency": mean_consistency,
            "samples_with_all_repeats": complete_samples,
            "stable_samples_with_all_repeats": stable_samples,
            "ready_for_promotion": ready_for_promotion,
        },
        "samples": sample_summaries,
        "records": records,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", default="references/s1-landing-shadow-validation.json")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--sample", action="append", help="只跑指定 sample id；可重复传入")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-dir")
    args = parser.parse_args()

    manifest = json.loads((ROOT / args.manifest).read_text(encoding="utf-8"))
    if args.sample:
        selected = set(args.sample)
        manifest["samples"] = [item for item in manifest.get("samples") or [] if item.get("id") in selected]
        missing = selected - {str(item.get("id")) for item in manifest["samples"]}
        if missing:
            raise SystemExit("manifest 不含 sample：" + ", ".join(sorted(missing)))
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = Path(args.output_dir) if args.output_dir else ROOT / "runs" / f"s1-landing-shadow-{stamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    for sample in manifest.get("samples") or []:
        analysis, facts = load_run(ROOT / str(sample["run_dir"]))
        payload = build_s1_landing_shadow_payload(MODEL, analysis, facts)
        content = payload["messages"][1]["content"]
        if not isinstance(content, list) or not any(item.get("type") == "input_audio" for item in content if isinstance(item, dict)):
            raise SystemExit(f"{sample['id']}: 聚焦 payload 没有音频，停止验证。")
    if args.dry_run:
        print(f"dry-run PASS: {len(manifest['samples'])} samples have focused multimodal payloads")
        return

    key_args = SimpleNamespace(
        llm_api_key_env="OPENAI_API_KEY",
        llm_api_key_keychain_service=KEYCHAIN_SERVICE,
        llm_api_key_keychain_account="API_KEY",
    )
    api_key = read_llm_api_key(key_args).strip()
    if not api_key:
        raise SystemExit("无 API key（OPENAI_API_KEY 或 keychain VidLingo.Qwen/API_KEY）。")
    jobs = [
        (sample, index)
        for sample in manifest.get("samples") or []
        for index in range(1, max(1, args.repeat) + 1)
    ]
    records: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(run_once, sample, index, api_key, output_dir, args.retries): (sample["id"], index)
            for sample, index in jobs
        }
        for future in as_completed(futures):
            record = future.result()
            records.append(record)
            print(f"[{len(records)}/{len(jobs)}] {record['sample_id']} #{record['repeat_index']}: {'ok' if record.get('ok') else 'failed'}", flush=True)
    summary = summarize(manifest, records, max(1, args.repeat))
    write_json(output_dir / "summary.json", summary)
    print(json.dumps(summary["metrics"], ensure_ascii=False, indent=2))
    print(output_dir / "summary.json")


if __name__ == "__main__":
    main()
