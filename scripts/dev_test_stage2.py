#!/usr/bin/env python3
"""阶段二（对比判断）独立测试工具，复用已有 run 的阶段一产物。

用途：调 Phase B 的 prompt / 感官素材时，不想每次重跑 whisper + fact extraction
（慢且费钱）。本脚本直接读某个 run 已生成的 facts + 帧 + 音频，只跑对比那一个
LLM 调用，把结果写到 <run>/dev_stage2_result.json，并打印关键验收指标。
支持 --repeat 连跑多次，用于验收 severity 稳定性。

用法：
  python3 scripts/dev_test_stage2.py runs/20260531-143521-improve
  python3 scripts/dev_test_stage2.py runs/20260531-143521-improve --dry  # 只看 payload 不调 LLM

依赖：阶段一产物必须存在（video_facts_{benchmark,creator}.json、analysis.json、
      各 role 的 audio.wav 和 frames/）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from flayr_core.llm.api import call_llm_api, extract_chat_completion_text, read_llm_api_key
from flayr_core.llm.parse import parse_json_text
from flayr_core.llm.payload import build_llm_comparison_payload, load_brand_proposition
from flayr_core.llm.pipeline import _process_llm_result
from flayr_core.utils import write_json

API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
MODEL = "qwen3.5-omni-plus"
KEYCHAIN_SERVICE = "VidLingo.Qwen"


def count_modalities(payload: dict) -> tuple[int, int, int]:
    """统计 user message 里 text / image_url / input_audio 数量。"""
    content = payload["messages"][1]["content"]
    if isinstance(content, str):
        return (1, 0, 0)
    t = sum(1 for c in content if isinstance(c, dict) and c.get("type") == "text")
    i = sum(1 for c in content if isinstance(c, dict) and c.get("type") == "image_url")
    a = sum(1 for c in content if isinstance(c, dict) and c.get("type") == "input_audio")
    return (t, i, a)


def get_stage(result: dict[str, Any], stage_prefix: str) -> dict[str, Any]:
    """按 S1/S2/S3 等前缀取 stage；取不到时返回空 dict。"""
    for stage in result.get("stage_analysis", []):
        if str(stage.get("stage") or "").startswith(stage_prefix):
            return stage
    return {}


def stage_severities(result: dict[str, Any]) -> dict[str, str]:
    """提取 S1-S6 的 severity。"""
    values: dict[str, str] = {}
    for stage in result.get("stage_analysis", []):
        name = str(stage.get("stage") or "?")
        key = name.split()[0] if name.startswith("S") else name
        values[key] = str(stage.get("severity") or "")
    return values


def finish_reason(raw: dict[str, Any]) -> str:
    """兼容 OpenAI-compatible choices finish_reason。"""
    choices = raw.get("choices")
    if isinstance(choices, list) and choices:
        return str(choices[0].get("finish_reason") or "")
    return ""


def call_once(
    run: Path,
    api_key: str,
    index: int,
    retries: int,
    analysis: dict[str, Any],
    analysis_input: str,
    facts: dict[str, Any],
) -> dict[str, Any]:
    """单次 stage2 调用，失败按 retries 重试；返回本次验收记录。"""
    raw_path = run / f"dev_stage2_response_{index:02d}.json"
    raw_result_path = run / f"dev_stage2_result_raw_{index:02d}.json"
    result_path = run / f"dev_stage2_result_{index:02d}.json"
    errors: list[str] = []
    for attempt in range(1, retries + 2):
        try:
            print(f"[llm] 第 {index} 次调用，attempt {attempt}/{retries + 1}…", flush=True)
            raw_text = call_llm_api(API_URL, api_key, run / "dev_stage2_request.json", raw_path)
            raw_path.write_text(raw_text, encoding="utf-8")
            raw = json.loads(raw_text)
            result_text = extract_chat_completion_text(raw)
            raw_result = parse_json_text(result_text)
            write_json(raw_result_path, raw_result)
            result = _process_llm_result(raw_result, analysis, analysis_input, facts)
            write_json(result_path, result)
            severities = stage_severities(result)
            raw_severities = stage_severities(raw_result)
            s3 = get_stage(result, "S3")
            return {
                "index": index,
                "ok": True,
                "attempts": attempt,
                "finish_reason": finish_reason(raw),
                "severity": severities,
                "raw_severity": raw_severities,
                "s3_severity": severities.get("S3", ""),
                "s3_gap": str(s3.get("gap") or ""),
                "s3_gap_summary": s3.get("gap_summary", []),
                "ambiguity_count": json.dumps(result, ensure_ascii=False).count("感知歧义"),
                "result_path": str(result_path),
                "raw_result_path": str(raw_result_path),
                "response_path": str(raw_path),
                "errors": errors,
            }
        except (SystemExit, json.JSONDecodeError, ValueError) as exc:
            errors.append(str(exc))
            if attempt <= retries:
                time.sleep(3 * attempt)
                continue
            return {
                "index": index,
                "ok": False,
                "attempts": attempt,
                "finish_reason": "",
                "severity": {},
                "raw_severity": {},
                "s3_severity": "",
                "s3_gap": "",
                "s3_gap_summary": [],
                "ambiguity_count": 0,
                "result_path": "",
                "raw_result_path": str(raw_result_path),
                "response_path": str(raw_path),
                "errors": errors,
            }


def process_existing_once(
    run: Path,
    index: int,
    analysis: dict[str, Any],
    analysis_input: str,
    facts: dict[str, Any],
) -> dict[str, Any]:
    """复用已落盘的 raw 结果，重新跑当前 postprocess 和稳定性统计。"""
    raw_result_path = run / f"dev_stage2_result_raw_{index:02d}.json"
    if not raw_result_path.exists():
        raw_result_path = run / f"dev_stage2_result_{index:02d}.json"
    result_path = run / f"dev_stage2_result_postprocessed_{index:02d}.json"
    response_path = run / f"dev_stage2_response_{index:02d}.json"
    if not raw_result_path.exists():
        return {
            "index": index,
            "ok": False,
            "attempts": 0,
            "finish_reason": "",
            "severity": {},
            "raw_severity": {},
            "s3_severity": "",
            "s3_gap": "",
            "s3_gap_summary": [],
            "ambiguity_count": 0,
            "result_path": "",
            "raw_result_path": str(raw_result_path),
            "response_path": str(response_path),
            "errors": [f"Missing existing result: {raw_result_path}"],
        }
    raw_result = json.loads(raw_result_path.read_text(encoding="utf-8"))
    try:
        result = _process_llm_result(raw_result, analysis, analysis_input, facts)
    except SystemExit as exc:
        return {
            "index": index,
            "ok": False,
            "attempts": 0,
            "finish_reason": "",
            "severity": {},
            "raw_severity": stage_severities(raw_result),
            "s3_severity": "",
            "s3_gap": "",
            "s3_gap_summary": [],
            "ambiguity_count": 0,
            "result_path": "",
            "raw_result_path": str(raw_result_path),
            "response_path": str(response_path),
            "errors": [str(exc)],
        }
    write_json(result_path, result)
    severities = stage_severities(result)
    s3 = get_stage(result, "S3")
    finish = "unknown"
    if response_path.exists():
        try:
            finish = finish_reason(json.loads(response_path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            finish = ""
    return {
        "index": index,
        "ok": True,
        "attempts": 0,
        "finish_reason": finish,
        "severity": severities,
        "raw_severity": stage_severities(raw_result),
        "s3_severity": severities.get("S3", ""),
        "s3_gap": str(s3.get("gap") or ""),
        "s3_gap_summary": s3.get("gap_summary", []),
        "ambiguity_count": json.dumps(result, ensure_ascii=False).count("感知歧义"),
        "result_path": str(result_path),
        "raw_result_path": str(raw_result_path),
        "response_path": str(response_path),
        "errors": [],
    }


def summarize_stability(
    run: Path,
    records: list[dict[str, Any]],
    payload_meta: dict[str, Any],
) -> dict[str, Any]:
    """生成 severity 稳定性验收汇总。"""
    successes = [record for record in records if record.get("ok")]
    severity_by_stage: dict[str, list[str]] = defaultdict(list)
    for record in successes:
        for stage, severity in record.get("severity", {}).items():
            severity_by_stage[stage].append(str(severity))

    s3_values = severity_by_stage.get("S3", [])
    s3_counts = Counter(s3_values)
    other_stage_stable = all(
        len(set(values)) <= 1
        for stage, values in severity_by_stage.items()
        if stage != "S3"
    )
    all_stop = all(record.get("finish_reason") == "stop" for record in successes)
    if all(record.get("finish_reason") == "unknown" for record in successes):
        all_stop = True
    gap_has_basis = all(
        "达人" in str(record.get("s3_gap") or "")
        and "标杆" in str(record.get("s3_gap") or "")
        and "购买" in str(record.get("s3_gap") or "")
        for record in successes
    )
    ambiguity_count = sum(int(record.get("ambiguity_count") or 0) for record in successes)
    s3_stable = bool(s3_values) and len(s3_counts) == 1
    passed = (
        len(successes) >= 3
        and len(successes) == len(records)
        and s3_stable
        and other_stage_stable
        and all_stop
        and gap_has_basis
        and ambiguity_count == 0
    )
    return {
        "run_dir": str(run),
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "payload": payload_meta,
        "criteria": {
            "success_count_at_least": 3,
            "all_requested_calls_successful": True,
            "s3_severity_all_same": True,
            "other_stage_severity_all_same": True,
            "finish_reason_all_stop": True,
            "s3_gap_mentions_creator_benchmark_purchase": True,
            "ambiguity_count_zero": True,
        },
        "passed": passed,
        "summary": {
            "requested": len(records),
            "successful": len(successes),
            "s3_counts": dict(s3_counts),
            "severity_by_stage": dict(severity_by_stage),
            "other_stage_stable": other_stage_stable,
            "finish_reason_all_stop": all_stop,
            "s3_gap_has_basis": gap_has_basis,
            "ambiguity_count": ambiguity_count,
        },
        "records": records,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", help="已有 run 目录，含阶段一产物")
    ap.add_argument("--dry", action="store_true", help="只构建并校验 payload，不调 LLM")
    ap.add_argument("--repeat", type=int, default=1, help="重复调用次数，用于 severity 稳定性验收")
    ap.add_argument("--retries", type=int, default=2, help="每次调用失败后的重试次数")
    ap.add_argument("--reuse-existing", action="store_true", help="复用 dev_stage2_result_XX.json，不重新调 LLM")
    ap.add_argument(
        "--skip-existing",
        action="store_true",
        help="断点续跑：raw 结果已存在且可处理的 index 直接复用，缺的才调 LLM（中断重跑零浪费）",
    )
    args = ap.parse_args()

    run = Path(args.run_dir)
    facts = {
        "benchmark": json.loads((run / "video_facts_benchmark.json").read_text(encoding="utf-8")),
        "creator": json.loads((run / "video_facts_creator.json").read_text(encoding="utf-8")),
    }
    analysis = json.loads((run / "analysis.json").read_text(encoding="utf-8"))
    # 注入 Step-0 地基 + 冻结命题尺子（pipeline 正常会做；本工具绕过 pipeline，需手动补，否则 S1 hook flag 不触发）
    foundation_path = run / "product_foundation.json"
    if foundation_path.is_file():
        analysis["product_foundation"] = json.loads(foundation_path.read_text(encoding="utf-8"))
    bp = load_brand_proposition(run)
    if bp:
        analysis["brand_proposition"] = bp
    analysis_input = (run / "analysis_input.md").read_text(encoding="utf-8")

    payload = build_llm_comparison_payload(MODEL, analysis_input, facts, analysis)
    t, i, a = count_modalities(payload)
    size_mb = len(json.dumps(payload)) / 1048576
    payload_meta = {
        "model": MODEL,
        "temperature": payload.get("temperature"),
        "max_tokens": payload.get("max_tokens"),
        "text_count": t,
        "image_count": i,
        "audio_count": a,
        "size_mb": round(size_mb, 2),
        "postprocess": "_process_llm_result",
    }
    print(f"[payload] text={t} image={i} audio={a} | {size_mb:.2f} MB", flush=True)

    # 硬校验：Phase B 必须挂上音频
    if a == 0:
        print("❌ 音频段数为 0 —— Phase B 感官素材未生效，停止。", flush=True)
        sys.exit(1)
    print(f"✅ 感官素材已挂载：{i} 帧 + {a} 段音频", flush=True)

    write_json(run / "dev_stage2_request.json", payload)
    if args.dry:
        print("[dry] 只构建 payload，未调 LLM。")
        return

    # 真正调用 LLM
    class _Args:
        llm_api_key_env = "OPENAI_API_KEY"
        llm_api_key_keychain_service = KEYCHAIN_SERVICE
        llm_api_key_keychain_account = "API_KEY"

    api_key = "" if args.reuse_existing else read_llm_api_key(_Args()).strip()
    if not api_key and not args.reuse_existing:
        print("❌ 无 API key（keychain VidLingo.Qwen）"); sys.exit(1)

    if args.reuse_existing:
        records = [
            process_existing_once(run, idx, analysis, analysis_input, facts)
            for idx in range(1, args.repeat + 1)
        ]
    elif args.skip_existing:
        records = []
        for idx in range(1, args.repeat + 1):
            raw_result_path = run / f"dev_stage2_result_raw_{idx:02d}.json"
            if raw_result_path.exists():
                record = process_existing_once(run, idx, analysis, analysis_input, facts)
                if record.get("ok"):
                    print(f"[skip] 第 {idx} 次已有 raw 结果，复用。", flush=True)
                    records.append(record)
                    continue
            records.append(call_once(run, api_key, idx, args.retries, analysis, analysis_input, facts))
    else:
        records = [
            call_once(run, api_key, idx, args.retries, analysis, analysis_input, facts)
            for idx in range(1, args.repeat + 1)
        ]
    summary = summarize_stability(run, records, payload_meta)
    summary_path = run / "dev_stage2_stability.json"
    write_json(summary_path, summary)
    print(f"[done] 稳定性汇总已写入 {summary_path}")

    # 简要验收
    print("\n=== severity 稳定性 ===")
    for stage, values in summary["summary"]["severity_by_stage"].items():
        print(f"  {stage:<3} {values}")
    print(f"\nS3 统计: {summary['summary']['s3_counts']}")
    print(f"感知歧义标注: {summary['summary']['ambiguity_count']} 次")
    print(f"验收结果: {'PASS' if summary['passed'] else 'FAIL'}")
    if not summary["passed"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
