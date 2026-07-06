#!/usr/bin/env python3
"""S2 产品引出契约 flag 定向验证工具。

完整 Stage2 会生成整份报告，输出长、API 等待不稳定。这个 probe 只让模型基于
现有 Stage1 facts 输出 S2 契约字段，用于低成本验证 S2 flag 的稳定性与准度。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from flayr_core.llm.api import call_llm_api, extract_chat_completion_text, read_llm_api_key  # noqa: E402
from flayr_core.llm.parse import normalize_s2_flags, parse_json_text  # noqa: E402
from flayr_core.llm.payload import extract_comparison_context  # noqa: E402
from flayr_core.utils import write_json  # noqa: E402

API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
MODEL = "qwen3.5-omni-plus"
KEYCHAIN_SERVICE = "VidLingo.Qwen"


def compact_facts(facts: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for role in ("creator", "benchmark"):
        units = []
        for unit in (facts.get(role) or {}).get("evidence_units") or []:
            if not isinstance(unit, dict):
                continue
            units.append({
                "id": unit.get("id"),
                "time_range": unit.get("time_range"),
                "information": unit.get("information"),
                "voiceover_zh": unit.get("voiceover_zh"),
                "visual_fact": unit.get("visual_fact"),
                "subtitle_fact": unit.get("subtitle_fact"),
                "functions": unit.get("functions"),
                "product_visible": unit.get("product_visible"),
                "product_coverage": unit.get("product_coverage"),
            })
        out[role] = {
            "content_summary": (facts.get(role) or {}).get("content_summary"),
            "communication_strategy": (facts.get(role) or {}).get("communication_strategy"),
            "evidence_units": units,
        }
    return out


def latest_stage_context(run: Path) -> dict[str, Any]:
    candidates = sorted(run.glob("dev_stage2_result_0*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        stages = data.get("stage_analysis") or []
        s1 = next((s for s in stages if str(s.get("stage") or "").startswith("S1")), {})
        s2 = next((s for s in stages if str(s.get("stage") or "").startswith("S2")), {})
        return {
            "source": str(path),
            "s1": {
                "creator_module_id": s1.get("creator_module_id"),
                "benchmark_module_id": s1.get("benchmark_module_id"),
                "creator_hook": s1.get("creator_hook"),
                "benchmark_hook": s1.get("benchmark_hook"),
            },
            "s2_current": {
                "creator_time_range": s2.get("creator_time_range"),
                "benchmark_time_range": s2.get("benchmark_time_range"),
                "creator_module_id": s2.get("creator_module_id"),
                "benchmark_module_id": s2.get("benchmark_module_id"),
                "creator_s2": s2.get("creator_s2"),
                "benchmark_s2": s2.get("benchmark_s2"),
            },
        }
    return {}


def build_payload(run: Path) -> dict[str, Any]:
    facts = {
        "benchmark": json.loads((run / "video_facts_benchmark.json").read_text(encoding="utf-8")),
        "creator": json.loads((run / "video_facts_creator.json").read_text(encoding="utf-8")),
    }
    analysis_input = (run / "analysis_input.md").read_text(encoding="utf-8")
    user_text = "\n\n".join([
        extract_comparison_context(analysis_input),
        "## 任务\n只判断 S2 产品引出契约，不输出完整报告。只输出严格 JSON。",
        "S2 定义：从 S1 Hook 自然过渡到产品。产品露出不等于产品引出完成；S2 只判三件事：承接 S1、说清产品身份、让产品成为答案/解决方案。卖点细节/成分/认证/选购建议归 S3/S4/S5。",
        "S2 模块：A=承接式引出，B=解谜式引出，C=对比式引出，D=第三方式引出，unknown=无法识别。",
        "S1→S2 兼容矩阵：S1-A→S2-A/S2-C；S1-B→S2-B；S1-C→S2-B/S2-A；S1-D→S2-A/S2-D；S1-E→S2-B/S2-D；S1-F→S2-A/S2-C；S1-G→S2-A/S2-D。",
        "输出 schema：{creator_s2:{exists,merged_with_s3,module_type,handoff_met,s1_s2_compatible,product_identity_clear,product_role_clear,excluded_or_risky_module,start_seconds,end_seconds,handoff_reason,evidence_ids}, benchmark_s2:{同字段}, comparison:{s2_severity_suggested: small|medium|large, reason: 一句话}}。",
        "所有 bool 字段必须是 true/false；module_type 只能 A/B/C/D/unknown；evidence_ids 必须引用下方 facts 里的 id。",
        "## 当前 S1/S2 上下文（若存在，仅作参考，必须基于 facts 复核）",
        json.dumps(latest_stage_context(run), ensure_ascii=False, indent=2),
        "## 已校验 facts",
        json.dumps(compact_facts(facts), ensure_ascii=False, indent=2),
    ])
    return {
        "model": MODEL,
        "temperature": 0.0,
        "max_tokens": 4096,
        "messages": [
            {"role": "system", "content": "你是 Flayr S2 产品引出契约审计器。只输出严格 JSON，不要 Markdown。"},
            {"role": "user", "content": user_text},
        ],
    }


def normalize_result(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "creator_s2": normalize_s2_flags(raw.get("creator_s2")),
        "benchmark_s2": normalize_s2_flags(raw.get("benchmark_s2")),
        "comparison": raw.get("comparison") if isinstance(raw.get("comparison"), dict) else {},
    }


def agreement(values: list[Any]) -> tuple[str, float]:
    vals = [str(v) for v in values if v is not None]
    if not vals:
        return "", 0.0
    value, count = Counter(vals).most_common(1)[0]
    return value, round(count / len(vals), 2)


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in records if r.get("ok")]
    out: dict[str, Any] = {"requested": len(records), "successful": len(ok)}
    for side in ("creator_s2", "benchmark_s2"):
        flags = [r.get("result", {}).get(side) for r in ok if isinstance(r.get("result", {}).get(side), dict)]
        side_out: dict[str, Any] = {}
        for key in ("module_type", "handoff_met", "s1_s2_compatible", "product_identity_clear", "product_role_clear", "merged_with_s3"):
            mode, ratio = agreement([f.get(key) for f in flags])
            side_out[key] = {"mode": mode, "agreement": ratio}
        out[side] = side_out
    mode, ratio = agreement([(r.get("result", {}).get("comparison") or {}).get("s2_severity_suggested") for r in ok])
    out["severity_suggested"] = {"mode": mode, "agreement": ratio}
    return out


def run_one(run: Path, api_key: str, index: int) -> dict[str, Any]:
    payload = build_payload(run)
    request_path = run / f"dev_s2_probe_request_{index:02d}.json"
    response_path = run / f"dev_s2_probe_response_{index:02d}.json"
    result_path = run / f"dev_s2_probe_{index:02d}.json"
    write_json(request_path, payload)
    raw_text = call_llm_api(API_URL, api_key, request_path, response_path)
    text = extract_chat_completion_text(json.loads(raw_text))
    raw = parse_json_text(text)
    result = normalize_result(raw)
    write_json(result_path, result)
    return {"index": index, "ok": True, "result_path": str(result_path), "result": result}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("samples", nargs="+")
    parser.add_argument("--repeat", type=int, default=2)
    args = parser.parse_args()

    key_args = argparse.Namespace(
        llm_api_key_env="DASHSCOPE_API_KEY",
        llm_api_key_keychain_service=KEYCHAIN_SERVICE,
        llm_api_key_keychain_account="API_KEY",
    )
    api_key = read_llm_api_key(key_args)
    if not api_key:
        raise SystemExit("Missing DashScope API key.")

    all_summaries = []
    for sample in args.samples:
        run = Path(sample)
        records = []
        print(f"[s2-probe] {run}", flush=True)
        for index in range(1, args.repeat + 1):
            try:
                record = run_one(run, api_key, index)
            except SystemExit as exc:
                record = {"index": index, "ok": False, "error": str(exc)}
            records.append(record)
            print(f"  run {index}: {'ok' if record.get('ok') else 'fail'}", flush=True)
            if not record.get("ok"):
                print(f"    {record.get('error')}", flush=True)
            time.sleep(1)
        summary = {"run_dir": str(run), "records": records, "summary": summarize(records)}
        write_json(run / "dev_s2_probe_summary.json", summary)
        all_summaries.append(summary)
    write_json(ROOT / "runs" / "s2_contract_probe_summary.json", {"samples": all_summaries})


if __name__ == "__main__":
    main()
