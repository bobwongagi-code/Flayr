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
import re
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
    build_stage_review_payload,
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
    validate_quality_contract,
    validate_stage_ownership,
)


# ---------------------------------------------------------------------------
# 外部 JSON 合并入口
# ---------------------------------------------------------------------------

def merge_analysis_result(analysis: dict[str, Any], result_path: Path) -> None:
    """把外部 analysis_result.json 经过 normalize + postprocess + 校验后合并入 analysis。"""
    result = json.loads(result_path.read_text(encoding="utf-8"))
    phase_c_review = result.get("phase_c_review")
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
    validate_quality_contract(normalized, analysis)

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
    if isinstance(phase_c_review, dict):
        analysis["phase_c_review"] = phase_c_review
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

def fetch_json_completion(
    args: argparse.Namespace,
    api_key: str,
    payload_path: Path,
    raw_path: Path,
    max_attempts: int = 3,
) -> str:
    """调用 LLM 并确保返回内容是可解析 JSON；静默截断时整体重取。

    DashScope 大响应偶发在传输途中被截断（无错误、finish_reason=None、body 不完整），
    导致 JSON 残缺。这类截断 repair 无效（重发也会截断），唯一可靠解是重取整次调用。
    重试 max_attempts 次仍不完整，则返回最后一次内容，交由下游 repair 兜底。
    """
    last_text = ""
    for attempt in range(max_attempts):
        raw_text = call_llm_api(args.llm_api_url, api_key, payload_path, raw_path)
        raw = json.loads(raw_text)
        last_text = extract_chat_completion_text(raw)
        try:
            parse_json_text(last_text)
            return last_text
        except SystemExit:
            # max_tokens 截断（finish_reason=length）重发也会在同处截断，直接交给 repair，不徒劳重取。
            if str(raw.get("choices", [{}])[0].get("finish_reason")) == "length":
                break
            if attempt + 1 >= max_attempts:
                break
    return last_text


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
    result_text = fetch_json_completion(args, api_key, payload_path, raw_path)
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
        raw_result = parse_json_text(raw_result_text)
        result = _process_llm_result(
            raw_result,
            analysis,
            analysis_input,
            locked_video_understanding,
        )
        return maybe_refine_low_confidence_stages(
            args=args,
            api_key=api_key,
            raw_result=raw_result,
            result=result,
            analysis_input=analysis_input,
            run_dir=run_dir,
            analysis=analysis,
            locked_video_understanding=locked_video_understanding,
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
        raw_repair_result = parse_json_text(repair_result_text)
        result = _process_llm_result(
            raw_repair_result,
            analysis,
            analysis_input,
            locked_video_understanding,
        )
        return maybe_refine_low_confidence_stages(
            args=args,
            api_key=api_key,
            raw_result=raw_repair_result,
            result=result,
            analysis_input=analysis_input,
            run_dir=run_dir,
            analysis=analysis,
            locked_video_understanding=locked_video_understanding,
        )
    except SystemExit as exc:
        raise SystemExit(f"LLM output repair failed. First error: {first_error}. Repair error: {exc}") from exc


def maybe_refine_low_confidence_stages(
    args: argparse.Namespace,
    api_key: str,
    raw_result: dict[str, Any],
    result: dict[str, Any],
    analysis_input: str,
    run_dir: Path,
    analysis: dict[str, Any],
    locked_video_understanding: dict[str, Any] | None,
) -> dict[str, Any]:
    """Phase C：模型主动声明低置信阶段后，只回看一次原生视频切片并重判。

    硬约束：
      - 只接受第一遍输出里的 low_confidence_stages；
      - 最多 2 个阶段；
      - 最多 1 次回看，不做循环；
      - facts 仍是唯一事实源，回看只修 stage_analysis。
    """
    if not locked_video_understanding:
        return result
    # 模型自报 ∪ 代码侧确定性检测（兜底模型漏报），最多 2 个，模型自报优先占位。
    stage_codes = []
    for code in [*extract_low_confidence_stages(raw_result), *detect_low_confidence_stages(result)]:
        if code not in stage_codes:
            stage_codes.append(code)
    stage_codes = stage_codes[:2]
    if not stage_codes:
        return result

    review_payload = build_stage_review_payload(
        args.llm_model,
        analysis,
        locked_video_understanding,
        result,
        stage_codes,
    )
    if not payload_has_video(review_payload):
        result["phase_c_review"] = {
            "requested_stages": stage_codes,
            "applied": False,
            "reason": "low_confidence_stages 已声明，但本地视频切片构造失败。",
        }
        return result

    review_request_path = run_dir / "llm_stage_review_request.json"
    review_response_path = run_dir / "llm_stage_review_response.json"
    write_json(review_request_path, review_payload)
    try:
        review_raw_text = call_llm_api(args.llm_api_url, api_key, review_request_path, review_response_path)
        review_response_path.write_text(review_raw_text, encoding="utf-8")
        review_text = extract_chat_completion_text(json.loads(review_raw_text))
        review_result = parse_json_text(review_text)
        refined = apply_stage_review_updates(
            result,
            review_result,
            analysis,
            analysis_input,
            locked_video_understanding,
        )
    except (SystemExit, json.JSONDecodeError) as exc:
        result["phase_c_review"] = {
            "requested_stages": stage_codes,
            "applied": False,
            "reason": f"低置信阶段回看失败：{exc}",
        }
        return result

    refined["phase_c_review"] = {
        "requested_stages": stage_codes,
        "applied": True,
        "response_path": str(review_response_path),
        "notes": review_result.get("review_notes", []),
    }
    return refined


def extract_low_confidence_stages(raw_result: dict[str, Any]) -> list[str]:
    """从第一遍 LLM 输出中提取 S1-S6 低置信阶段代码。"""
    values = raw_result.get("low_confidence_stages")
    if values is None and isinstance(raw_result.get("quality_control"), dict):
        values = raw_result["quality_control"].get("low_confidence_stages")
    if not isinstance(values, list):
        return []
    codes: list[str] = []
    for value in values:
        text = str(value or "").strip().upper()
        if text.startswith("S") and len(text) >= 2:
            code = text[:2]
            if code in {"S1", "S2", "S3", "S4", "S5", "S6"} and code not in codes:
                codes.append(code)
    return codes[:2]


# 占位证据单元（_NO_STAGE_/_NO_USAGE/_NO_CTA 等）和"证据不足"提示，是后处理写入的
# 客观"素材不足"标记，不依赖模型自觉，可作为确定性回看触发信号。
_PLACEHOLDER_EVIDENCE_RE = re.compile(r"_NO_|NO_STAGE|NO_USAGE|NO_CTA")
_EVIDENCE_CAUTION_RE = re.compile(r"证据不足|待复核|需人工复核|未识别|未发现可|未验证|画面证据不足")


def detect_low_confidence_stages(result: dict[str, Any]) -> list[str]:
    """代码侧确定性兜底：用客观素材不足信号识别该回看的阶段，补模型自报的漏报。

    判据（针对达人侧——分析主体）：
    - 引用的是占位 evidence_unit（_NO_*），或 support_status=visual_only 且无有效口播/带待复核提示；
    - 且 severity ∈ {large, medium}：只有"薄证据上的高后果判断"才值得花一次回看。
    large 优先，最多 2 个，与现有 Phase C 约束一致。
    """
    creator_units = {
        str(unit.get("id")): unit
        for unit in result.get("video_understanding", {}).get("creator", {}).get("evidence_units", [])
        if isinstance(unit, dict)
    }
    large: list[str] = []
    medium: list[str] = []
    for stage in result.get("stage_analysis", []):
        if not isinstance(stage, dict):
            continue
        code = stage_code(stage.get("stage"))
        severity = str(stage.get("severity") or "").strip().lower()
        if not code or severity not in {"large", "medium"}:
            continue
        ids = [str(value) for value in stage.get("creator_evidence_ids", [])]
        has_placeholder = any(_PLACEHOLDER_EVIDENCE_RE.search(item) for item in ids)
        unit_visual = " ".join(str(creator_units.get(item, {}).get("visual_fact", "")) for item in ids)
        stage_visual = " ".join(str(value) for value in stage.get("creator_visual_evidence", []))
        has_caution = bool(_EVIDENCE_CAUTION_RE.search(unit_visual + " " + stage_visual))
        visual_only = str(stage.get("creator_support_status") or "") == "visual_only"
        no_voice = not str(stage.get("creator_quote") or "").strip()
        if has_placeholder or (visual_only and (has_caution or no_voice)):
            (large if severity == "large" else medium).append(code)
    ordered: list[str] = []
    for code in [*large, *medium]:
        if code not in ordered:
            ordered.append(code)
    return ordered[:2]


def payload_has_video(payload: dict[str, Any]) -> bool:
    """判断回看 payload 是否真正挂了 video_url。"""
    for message in payload.get("messages", []):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        if any(isinstance(item, dict) and item.get("type") == "video_url" for item in content):
            return True
    return False


def apply_stage_review_updates(
    current_result: dict[str, Any],
    review_result: dict[str, Any],
    analysis: dict[str, Any],
    analysis_input: str,
    locked_video_understanding: dict[str, Any],
) -> dict[str, Any]:
    """把 Phase C 返回的 stage_updates 合并回完整结果，再走现有校验链。"""
    updates = review_result.get("stage_updates")
    if not isinstance(updates, list) or not updates:
        raise SystemExit("Phase C review returned no stage_updates.")

    updates_by_code: dict[str, dict[str, Any]] = {}
    for update in updates:
        if not isinstance(update, dict):
            continue
        code = stage_code(update.get("stage"))
        if code:
            updates_by_code[code] = update
    if not updates_by_code:
        raise SystemExit("Phase C review returned no valid stage codes.")

    merged = json.loads(json.dumps(current_result, ensure_ascii=False))
    merged_stages = []
    for stage in merged.get("stage_analysis", []):
        code = stage_code(stage.get("stage"))
        merged_stages.append(updates_by_code.get(code, stage))
    merged["stage_analysis"] = merged_stages
    return _process_llm_result(merged, analysis, analysis_input, locked_video_understanding)


def stage_code(value: Any) -> str:
    text = str(value or "").strip().upper()
    if len(text) >= 2 and text[:2] in {"S1", "S2", "S3", "S4", "S5", "S6"}:
        return text[:2]
    return ""


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
    validate_quality_contract(normalized, analysis)
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
        result_text = fetch_json_completion(args, api_key, request_path, response_path)
        fact_result = normalize_video_fact_result(role, parse_json_text(result_text), analysis)
        facts[role] = fact_result
        write_json(result_path, fact_result)
    return facts
