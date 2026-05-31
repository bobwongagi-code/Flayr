"""flayr_core.llm.pipeline：LLM 分析主入口。

三个 public 函数：
  - run_large_model_analysis        从 analysis_input.md 跑完整分析，写出 analysis_result.json
  - parse_and_validate_llm_result   解析模型原始输出，必要时做一次 repair 重试
  - merge_analysis_result           把外部提供的 analysis_result.json 合并进 analysis dict

merge 与 parse_and_validate 共用中段处理链 apply_postprocess_chain；
尾部处理（sanitize / extra validate / clamp）两边略有差异，故各自显式调用。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

from ..utils import write_json
from .api import call_llm_api, extract_chat_completion_text, read_llm_api_key
from .parse import (
    normalize_analysis_result,
    normalize_video_fact_result,
    parse_json_text,
)
from .payload import (
    build_llm_comparison_payload,
    build_llm_payload,
    build_llm_repair_payload,
    build_video_fact_payload,
    select_role_visual_inputs,
)
from ..postprocess import apply_postprocess_chain
from ..postprocess.health_rewrite import (
    sanitize_child_toothpaste_recommendations,
    sanitize_health_recommendations,
    validate_creator_script_language,
    validate_recommendation_safety,
)
from ..postprocess.repair import (
    clamp_result_time_ranges,
    ground_improvement_evidence,
    remove_unverified_brand_models,
    stabilize_improvement_priorities,
)
from ..postprocess.validate import (
    validate_analysis_dimensions,
    validate_evidence_alignment,
    validate_stage_ownership,
)


# ---------------------------------------------------------------------------
# 外部 JSON 合并入口
# ---------------------------------------------------------------------------

def merge_analysis_result(analysis: dict[str, Any], result_path: Path) -> None:
    """把外部 analysis_result.json 经过 normalize + postprocess + 校验后合并入 analysis。"""
    result = json.loads(result_path.read_text(encoding="utf-8"))
    normalized = normalize_analysis_result(result)

    apply_postprocess_chain(normalized, analysis)

    # 尾部：先 ground_improvement_evidence，再用 product 信息做品类合规重写
    ground_improvement_evidence(normalized)
    merge_context = json.dumps(analysis.get("product", {}), ensure_ascii=False)
    sanitize_child_toothpaste_recommendations(normalized, merge_context)
    stabilize_improvement_priorities(normalized)
    sanitize_health_recommendations(normalized, merge_context)
    validate_evidence_alignment(normalized)
    validate_stage_ownership(normalized)
    remove_unverified_brand_models(normalized, analysis)
    clamp_result_time_ranges(normalized, analysis)

    analysis["executive_summary"] = normalized["executive_summary"]
    analysis["one_line_summary"] = normalized["one_line_summary"]
    analysis["one_line_verdict"] = normalized["one_line_verdict"]
    analysis["holistic_assessment"] = normalized["holistic_assessment"]
    analysis["key_conclusions"] = normalized.get("key_conclusions", [])
    analysis["product_visibility"] = normalized["product_visibility"]
    analysis["loop_closure"] = normalized["loop_closure"]
    analysis["video_understanding"] = normalized["video_understanding"]
    analysis["stage_analysis"] = normalized["stage_analysis"]
    analysis["improvements"] = normalized["improvements"]
    # LLM 分析已成功合并，标记 status 让 report 不再渲染"未跑 LLM"警告
    analysis["improvements_status"] = "llm_completed"
    analysis["analysis_source"] = {
        "type": "large_model_json",
        "path": str(result_path),
        "merged_at": dt.datetime.now().isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# LLM 调用 + 校验主入口
# ---------------------------------------------------------------------------

def run_large_model_analysis(
    args: argparse.Namespace,
    analysis: dict[str, Any],
    analysis_input_path: Path,
    run_dir: Path,
) -> Path | None:
    """从 analysis_input.md 出发跑一次完整 LLM 分析，写出 analysis_result.json。"""
    api_key = read_llm_api_key(args).strip()
    if not api_key and not args.llm_dry_run:
        keychain_hint = ""
        if args.llm_api_key_keychain_service:
            keychain_hint = f" or Keychain service {args.llm_api_key_keychain_service}"
        raise SystemExit(f"Missing API key: set ${args.llm_api_key_env}{keychain_hint}, or use --llm-dry-run.")

    if args.llm_include_images:
        facts = run_video_fact_extraction(args, analysis, run_dir, api_key)
        if args.llm_dry_run:
            print(f"LLM dry run: fact request payloads written to {run_dir}")
            return None
        analysis_input = analysis_input_path.read_text(encoding="utf-8")
        payload = build_llm_comparison_payload(args.llm_model, analysis_input, facts, analysis)
    else:
        facts = None
        payload = build_llm_payload(args.llm_model, analysis_input_path.read_text(encoding="utf-8"), [])

    payload_path = run_dir / "llm_request.json"
    write_json(payload_path, payload)

    if args.llm_dry_run:
        print(f"LLM dry run: request payload written to {payload_path}")
        return None

    raw_path = run_dir / "llm_response.json"
    raw_text = call_llm_api(args.llm_api_url, api_key, payload_path, raw_path)
    raw_path.write_text(raw_text, encoding="utf-8")

    result_text = extract_chat_completion_text(json.loads(raw_text))
    result = parse_and_validate_llm_result(
        args=args,
        api_key=api_key,
        raw_result_text=result_text,
        analysis_input=analysis_input_path.read_text(encoding="utf-8"),
        run_dir=run_dir,
        analysis=analysis,
        locked_video_understanding=facts,
    )
    result_path = run_dir / "analysis_result.json"
    write_json(result_path, result)
    return result_path


def parse_and_validate_llm_result(
    args: argparse.Namespace,
    api_key: str,
    raw_result_text: str,
    analysis_input: str,
    run_dir: Path,
    analysis: dict[str, Any],
    locked_video_understanding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """解析 LLM 输出并跑全套校验；首次失败时构造 repair payload 再跑一次。"""
    try:
        return _process_llm_result(
            parse_json_text(raw_result_text),
            analysis,
            analysis_input,
            locked_video_understanding,
        )
    except SystemExit as exc:
        first_error = str(exc)

    repair_payload = build_llm_repair_payload(args.llm_model, raw_result_text, first_error, analysis_input)
    repair_request_path = run_dir / "llm_repair_request.json"
    repair_response_path = run_dir / "llm_repair_response.json"
    write_json(repair_request_path, repair_payload)
    repair_raw_text = call_llm_api(args.llm_api_url, api_key, repair_request_path, repair_response_path)
    repair_response_path.write_text(repair_raw_text, encoding="utf-8")

    repair_result_text = extract_chat_completion_text(json.loads(repair_raw_text))
    try:
        return _process_llm_result(
            parse_json_text(repair_result_text),
            analysis,
            analysis_input,
            locked_video_understanding,
        )
    except SystemExit as exc:
        raise SystemExit(f"LLM output repair failed. First error: {first_error}. Repair error: {exc}") from exc


def _process_llm_result(
    result: dict[str, Any],
    analysis: dict[str, Any],
    analysis_input: str,
    locked_video_understanding: dict[str, Any] | None,
) -> dict[str, Any]:
    """parse_and_validate 主路径与 repair 路径共享的完整处理链。

    与 merge_analysis_result 的区别（codex 历史选择，保持不变）：
      - validate_evidence_alignment + validate_stage_ownership 在 sanitize 之前
      - sanitize 顺序为 health → child_toothpaste
      - ground_improvement_evidence 在 sanitize 之后
      - 额外做 validate_analysis_dimensions / validate_recommendation_safety / validate_creator_script_language
    """
    if locked_video_understanding:
        result["video_understanding"] = locked_video_understanding
    normalized = normalize_analysis_result(result)
    apply_postprocess_chain(normalized, analysis)
    validate_evidence_alignment(normalized)
    validate_stage_ownership(normalized)
    sanitize_health_recommendations(normalized, analysis_input)
    sanitize_child_toothpaste_recommendations(normalized, analysis_input)
    stabilize_improvement_priorities(normalized)
    ground_improvement_evidence(normalized)
    stabilize_improvement_priorities(normalized)
    validate_analysis_dimensions(normalized)
    validate_recommendation_safety(normalized, analysis_input)
    validate_creator_script_language(normalized, analysis_input)
    remove_unverified_brand_models(normalized, analysis)
    clamp_result_time_ranges(normalized, analysis)
    return normalized


# ---------------------------------------------------------------------------
# 单视频事实抽取
# ---------------------------------------------------------------------------

def run_video_fact_extraction(
    args: argparse.Namespace,
    analysis: dict[str, Any],
    run_dir: Path,
    api_key: str,
) -> dict[str, Any]:
    """对每个 role 跑一次单视频事实抽取，写出 video_facts_{role}.json 并返回 dict。"""
    facts: dict[str, Any] = {}
    videos = analysis.get("videos", {})
    per_role_limit = max(4, args.llm_image_limit // max(1, len(videos)))
    for role in ("benchmark", "creator"):
        if role not in videos:
            continue
        role_dir = run_dir / role
        visual_inputs = select_role_visual_inputs(videos[role], role, per_role_limit)
        payload = build_video_fact_payload(args.llm_model, role, analysis, visual_inputs)
        request_path = run_dir / f"llm_facts_{role}_request.json"
        response_path = run_dir / f"llm_facts_{role}_response.json"
        result_path = run_dir / f"video_facts_{role}.json"
        write_json(request_path, payload)
        if args.llm_dry_run:
            continue
        raw_text = call_llm_api(args.llm_api_url, api_key, request_path, response_path)
        response_path.write_text(raw_text, encoding="utf-8")
        result_text = extract_chat_completion_text(json.loads(raw_text))
        fact_result = normalize_video_fact_result(role, parse_json_text(result_text), analysis)
        facts[role] = fact_result
        write_json(result_path, fact_result)
    return facts
