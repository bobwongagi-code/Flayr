"""flayr_core.llm.pipeline：LLM 分析主入口。

三个 public 函数：
  - run_large_model_analysis        从 analysis_input.md 跑完整分析，写出 analysis_result.json
  - parse_and_validate_llm_result   解析模型原始输出，必要时做一次 repair 重试
  - merge_analysis_result           把外部提供的 analysis_result.json 合并进 analysis dict

所有入口通过 finalize_analysis_result 走同一条完整处理链，避免外部 JSON 和实时 LLM
因校验顺序不同而产生不同报告。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from ..utils import write_json, write_text
from .api import call_llm_api, extract_chat_completion_text, read_llm_api_key
from .analysis_contract import AnalysisContractError, validate_normalized_analysis_contract
from .parse import (
    normalize_analysis_result,
    normalize_category_profile,
    normalize_product_profile,
    normalize_video_fact_result,
    parse_json_text,
)
from .payload import (
    build_improvement_reconciliation_payload,
    build_llm_comparison_payload,
    build_llm_payload,
    build_llm_repair_payload,
    build_product_foundation_payload,
    build_product_foundation_repair_payload,
    build_stage_review_payload,
    build_video_fact_payload,
    load_brand_proposition,
)
from .s4_visual_verifier import maybe_apply_s4_visual_verifier
from .media import select_role_visual_inputs
from ..postprocess import apply_postprocess_chain
from ..postprocess.derive import critical_severity_stages
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

def apply_finalized_analysis_result(
    analysis: dict[str, Any],
    normalized: dict[str, Any],
    result_path: Path,
) -> None:
    """把已完成校验的结果写回主 analysis；此处不得再次做后处理。"""
    phase_c_review = normalized.get("phase_c_review")
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
    for key in (
        "category_profile",
        "product_profile",
        "s3_s4_relationship",
        "promise_chain",
        "product_proposition_contract",
        "cross_stage_state",
        "proposition_trace",
        "computed_loop_closure",
        "qa_warnings",
        "quality_audit",
        "improvement_reconciliation",
        "s4_visual_verifier",
    ):
        if key in normalized:
            analysis[key] = normalized[key]
    if isinstance(phase_c_review, dict):
        analysis["phase_c_review"] = phase_c_review
    analysis["improvements_status"] = "llm_completed"
    analysis["analysis_source"] = {
        "type": "large_model_json",
        "path": str(result_path),
        "merged_at": dt.datetime.now().isoformat(timespec="seconds"),
    }


def finalize_analysis_result(
    result: dict[str, Any],
    analysis: dict[str, Any],
    analysis_input: str,
    locked_video_understanding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """所有 LLM 结果入口共用的唯一规范化、修补和校验链。"""
    if locked_video_understanding:
        result["video_understanding"] = locked_video_understanding
    normalized = normalize_analysis_result(result)
    try:
        validate_normalized_analysis_contract(normalized)
    except AnalysisContractError as exc:
        raise SystemExit(str(exc)) from exc
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


def merge_analysis_result(analysis: dict[str, Any], result_path: Path, analysis_input: str) -> None:
    """把外部 analysis_result.json 经唯一处理链后合并入 analysis。"""
    result = json.loads(result_path.read_text(encoding="utf-8"))
    phase_c_review = result.get("phase_c_review")
    normalized = finalize_analysis_result(result, analysis, analysis_input)
    if isinstance(phase_c_review, dict):
        normalized["phase_c_review"] = phase_c_review
    apply_finalized_analysis_result(analysis, normalized, result_path)


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
) -> tuple[Path, dict[str, Any]] | None:
    """从 analysis_input.md 出发跑一次完整 LLM 分析，写出 analysis_result.json。"""
    api_key = read_llm_api_key(args).strip()
    if not api_key and not args.llm_dry_run:
        keychain_hint = ""
        if args.llm_api_key_keychain_service:
            keychain_hint = f" or Keychain service {args.llm_api_key_keychain_service}"
        raise SystemExit(f"Missing API key: set ${args.llm_api_key_env}{keychain_hint}, or use --llm-dry-run.")

    if args.llm_include_images:
        # 冻结 S1 命题尺子（人工策展）：先挂进 analysis，再跑 Step-0，避免品牌/型号空猜污染品地基。
        brand_proposition = load_brand_proposition(run_dir)
        if brand_proposition:
            analysis["brand_proposition"] = brand_proposition
        # Step-0：先确立品的商业地基（特征+命题），贯穿喂给阶段1 观察 + 阶段2 判断；失败则下游内联兜底。
        foundation = establish_product_foundation(args, analysis, run_dir, api_key)
        if foundation:
            analysis["product_foundation"] = foundation
        facts = run_video_fact_extraction(args, analysis, run_dir, api_key)
        if args.llm_dry_run:
            print(f"LLM dry run: fact request payloads written to {run_dir}")
            return None
        analysis["s1_hook_flags_required"] = True
        analysis["s2_flags_required"] = True
        analysis["s3_flags_required"] = True
        analysis["s4_flags_required"] = True
        analysis["s5_flags_required"] = True
        analysis["s6_flags_required"] = True
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
    return result_path, result


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
        refined = maybe_refine_low_confidence_stages(
            args=args,
            api_key=api_key,
            raw_result=raw_result,
            result=result,
            analysis_input=analysis_input,
            run_dir=run_dir,
            analysis=analysis,
            locked_video_understanding=locked_video_understanding,
        )
        visually_checked = maybe_apply_s4_visual_verifier(
            args=args,
            api_key=api_key,
            result=refined,
            analysis=analysis,
            run_dir=run_dir,
        )
        return maybe_reconcile_final_improvements(
            args=args,
            api_key=api_key,
            result=visually_checked,
            analysis=analysis,
            analysis_input=analysis_input,
            locked_video_understanding=locked_video_understanding,
            run_dir=run_dir,
        )
    except SystemExit as exc:
        first_error = str(exc)

    repair_payload = build_llm_repair_payload(
        args.llm_model,
        raw_result_text,
        first_error,
        analysis_input,
        locked_video_understanding,
        analysis=analysis,
    )
    repair_request_path = run_dir / "llm_repair_request.json"
    repair_response_path = run_dir / "llm_repair_response.json"
    write_json(repair_request_path, repair_payload)
    repair_raw_text = call_llm_api(args.llm_api_url, api_key, repair_request_path, repair_response_path)
    write_text(repair_response_path, repair_raw_text)

    repair_result_text = extract_chat_completion_text(json.loads(repair_raw_text))
    try:
        raw_repair_result = parse_json_text(repair_result_text)
        result = _process_llm_result(
            raw_repair_result,
            analysis,
            analysis_input,
            locked_video_understanding,
        )
        refined = maybe_refine_low_confidence_stages(
            args=args,
            api_key=api_key,
            raw_result=raw_repair_result,
            result=result,
            analysis_input=analysis_input,
            run_dir=run_dir,
            analysis=analysis,
            locked_video_understanding=locked_video_understanding,
        )
        visually_checked = maybe_apply_s4_visual_verifier(
            args=args,
            api_key=api_key,
            result=refined,
            analysis=analysis,
            run_dir=run_dir,
        )
        return maybe_reconcile_final_improvements(
            args=args,
            api_key=api_key,
            result=visually_checked,
            analysis=analysis,
            analysis_input=analysis_input,
            locked_video_understanding=locked_video_understanding,
            run_dir=run_dir,
        )
    except SystemExit as exc:
        raise SystemExit(f"LLM output repair failed. First error: {first_error}. Repair error: {exc}") from exc


def uncovered_large_stage_codes(result: dict[str, Any]) -> list[str]:
    """返回最终为 large、但 Top 提升点尚未覆盖的阶段。"""
    covered = {
        stage_code(item.get("target_stage"))
        for item in result.get("improvements", [])
        if isinstance(item, dict) and stage_code(item.get("target_stage"))
    }
    return [
        code
        for stage in result.get("stage_analysis", [])
        if isinstance(stage, dict)
        and (code := stage_code(stage.get("stage")))
        and str(stage.get("severity") or "").strip().lower() == "large"
        and code not in covered
    ]


def merge_reconciled_improvements(
    result: dict[str, Any],
    additions: list[dict[str, Any]],
    missing_stage_codes: list[str],
) -> dict[str, Any]:
    """把缺失阶段建议并入现有 Top 列表；最多五项，优先覆盖最终大差距。"""
    wanted = set(missing_stage_codes)
    valid_additions = [
        item for item in additions
        if isinstance(item, dict) and stage_code(item.get("target_stage")) in wanted
    ]
    additions_by_stage: dict[str, dict[str, Any]] = {}
    for item in valid_additions:
        additions_by_stage.setdefault(stage_code(item.get("target_stage")), item)

    merged = json.loads(json.dumps(result, ensure_ascii=False))
    existing = [item for item in merged.get("improvements", []) if isinstance(item, dict)]
    stage_severity = {
        stage_code(stage.get("stage")): str(stage.get("severity") or "medium").strip().lower()
        for stage in merged.get("stage_analysis", [])
        if isinstance(stage, dict)
    }
    combined = [*additions_by_stage.values(), *existing]
    severity_rank = {"large": 0, "medium": 1, "small": 2}
    combined.sort(
        key=lambda item: (
            severity_rank.get(stage_severity.get(stage_code(item.get("target_stage")), "medium"), 1),
            0 if stage_code(item.get("target_stage")) in additions_by_stage else 1,
            _safe_priority(item.get("priority")),
        )
    )
    merged["improvements"] = combined[:5]
    return merged


def _safe_priority(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 99


def maybe_reconcile_final_improvements(
    *,
    args: argparse.Namespace,
    api_key: str,
    result: dict[str, Any],
    analysis: dict[str, Any],
    analysis_input: str,
    locked_video_understanding: dict[str, Any] | None,
    run_dir: Path,
) -> dict[str, Any]:
    """最终 severity 与建议脱节时做一次纯文本补全；失败不得阻断主分析。"""
    missing = uncovered_large_stage_codes(result)
    if not missing or args.llm_dry_run:
        return result
    payload = build_improvement_reconciliation_payload(args.llm_model, result, missing, analysis)
    request_path = run_dir / "llm_improvement_reconciliation_request.json"
    response_path = run_dir / "llm_improvement_reconciliation_response.json"
    write_json(request_path, payload)
    preserved = {
        key: result[key]
        for key in ("phase_c_review", "s4_visual_verifier")
        if key in result
    }
    try:
        raw_text = call_llm_api(args.llm_api_url, api_key, request_path, response_path)
        write_text(response_path, raw_text)
        parsed = parse_json_text(extract_chat_completion_text(json.loads(raw_text)))
        additions = parsed.get("improvements") if isinstance(parsed.get("improvements"), list) else []
        merged = merge_reconciled_improvements(result, additions, missing)
        reconciled = _process_llm_result(
            merged,
            analysis,
            analysis_input,
            locked_video_understanding,
        )
        remaining = uncovered_large_stage_codes(reconciled)
        if any(code in remaining for code in missing):
            raise ValueError("补全结果未覆盖全部缺失的大差距阶段")
    except (Exception, SystemExit) as exc:  # 可选补全失败时保留主分析结果
        result["improvement_reconciliation"] = {
            "applied": False,
            "requested_stages": missing,
            "reason": f"最终提升点补全失败：{exc}",
        }
        return result
    reconciled.update(preserved)
    reconciled["improvement_reconciliation"] = {
        "applied": True,
        "requested_stages": missing,
        "response_path": str(response_path),
    }
    return reconciled


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
    # 候选 = 模型自报 ∪ 素材不足确定性检测 ∪ 推导临界分值（S 压阈值线邻域，4d 后可行）。
    # 按优先级取 2：P1 链路致命节点 S1/S6（判错代价最高）→ P2 高杠杆验证节点 S4 → P3 其他。
    candidates: list[str] = []
    for code in [
        *extract_low_confidence_stages(raw_result),
        *detect_low_confidence_stages(result),
        *critical_severity_stages(result),
    ]:
        if code not in candidates:
            candidates.append(code)
    _priority = {"S1": 0, "S6": 0, "S4": 1}
    stage_codes = sorted(candidates, key=lambda c: (_priority.get(c, 2), candidates.index(c)))[:2]
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
        write_text(review_response_path, review_raw_text)
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
        if code in updates_by_code:
            # 字段级合并而非整字典替换：回看若漏掉执行分等新字段，保留原值，
            # 否则 derive 对复核阶段反而退化为模型直判（code review #1）。
            base_stage = dict(stage)
            # S1 hook flags 是 severity 输入事实。S1 回看后必须由回看结果重判，
            # 不能字段级合并时沿用旧 hook，否则会出现"新 stage 套旧 hook"。
            if code == "S1":
                base_stage.pop("creator_hook", None)
                base_stage.pop("benchmark_hook", None)
            if code == "S2":
                base_stage.pop("creator_s2", None)
                base_stage.pop("benchmark_s2", None)
            if code == "S3":
                base_stage.pop("creator_s3", None)
                base_stage.pop("benchmark_s3", None)
            if code == "S4":
                base_stage.pop("creator_s4", None)
                base_stage.pop("benchmark_s4", None)
            merged_stages.append({**base_stage, **updates_by_code[code]})
        else:
            merged_stages.append(stage)
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
    """兼容内部调用点；实际处理统一委托给 finalize_analysis_result。"""
    return finalize_analysis_result(result, analysis, analysis_input, locked_video_understanding)


# ---------------------------------------------------------------------------
# 单视频事实抽取
# ---------------------------------------------------------------------------

def establish_product_foundation(
    args: argparse.Namespace,
    analysis: dict[str, Any],
    run_dir: Path,
    api_key: str,
) -> dict[str, Any] | None:
    """Step-0：看视频前先据产品事实 + 品类世界知识确立品的商业地基（category_profile 特征 +
    product_profile 命题），存 product_foundation.json 并返回，供阶段1 观察、阶段2 判断、4d 政策消费。
    失败返回 None，下游回退到阶段2 内联产出——主分析始终能跑完出报告（架构不变量）。"""
    if not has_product_foundation_anchor(analysis):
        print(
            "Step-0 跳过：缺少品类/卖点/目标用户/人工命题等可靠锚点，退回阶段2 视频推断（避免仅凭品牌名空猜）。",
            flush=True,
        )
        return None
    payload = build_product_foundation_payload(args.llm_model, analysis)
    request_path = run_dir / "llm_product_foundation_request.json"
    response_path = run_dir / "llm_product_foundation_response.json"
    write_json(request_path, payload)
    if args.llm_dry_run:
        return None
    try:
        result_text = fetch_json_completion(args, api_key, request_path, response_path)
        raw = parse_json_text(result_text)
        foundation = {
            "category_profile": normalize_category_profile(raw.get("category_profile")),
            "product_profile": normalize_product_profile(raw.get("product_profile")),
        }
        validation_reason = product_foundation_validation_reason(foundation.get("product_profile"))
        if validation_reason:
            repair_payload = build_product_foundation_repair_payload(
                args.llm_model,
                analysis,
                raw.get("product_profile") if isinstance(raw.get("product_profile"), dict) else {},
                validation_reason,
            )
            repair_request_path = run_dir / "llm_product_foundation_repair_request.json"
            repair_response_path = run_dir / "llm_product_foundation_repair_response.json"
            write_json(repair_request_path, repair_payload)
            repair_text = fetch_json_completion(args, api_key, repair_request_path, repair_response_path)
            repaired_raw = parse_json_text(repair_text)
            foundation = {
                "category_profile": normalize_category_profile(repaired_raw.get("category_profile")),
                "product_profile": normalize_product_profile(repaired_raw.get("product_profile")),
            }
            repaired_reason = product_foundation_validation_reason(foundation.get("product_profile"))
            if repaired_reason:
                # 二次回答仍不合格时保留地基，但显式降级，禁止下游把旧视觉字段当强证据。
                if foundation["product_profile"] is not None:
                    foundation["product_profile"]["short_video_proof_plan"] = {
                        "candidates": [],
                        "s4_anchor_candidate_id": "",
                        "selection_source": "model_category_default",
                        "anchor_confidence": "low",
                        "valid": False,
                        "validation_reason": f"Step-0 重答后仍无有效 short_video_proof_plan：{repaired_reason}",
                    }
                    foundation["product_profile"]["proof_contract"] = {
                        "anchor_candidate_id": "",
                        "mode": "trust_substituted",
                        "consumer_outcome": "",
                        "signal_type": "",
                        "observable_dimension": "",
                        "observable_signal": "",
                        "before_state": "",
                        "after_state": "",
                        "proof_condition": "",
                        "valid": False,
                        "validation_reason": f"Step-0 重答后仍无有效产品证明合同：{repaired_reason}",
                    }
                    foundation["product_profile"]["visual_proof_points"] = []
        if not foundation["category_profile"] and not foundation["product_profile"]:
            raise ValueError("category_profile 与 product_profile 均为空")
    except Exception as exc:  # noqa: BLE001
        print(f"Step-0 品地基确立失败，回退到阶段2 内联产出：{exc}", flush=True)
        return None
    write_json(run_dir / "product_foundation.json", foundation)
    return foundation


def product_foundation_validation_reason(product_profile: Any) -> str:
    """新 Step-0 必须同时产出卖点分流计划与可校验合同；历史分析结果仍由 normalize 层兼容。"""
    if not isinstance(product_profile, dict):
        return "缺少 product_profile"
    plan = product_profile.get("short_video_proof_plan")
    if not isinstance(plan, dict) or plan.get("valid") is not True:
        return str(plan.get("validation_reason") or "缺少有效 short_video_proof_plan") if isinstance(plan, dict) else "缺少 short_video_proof_plan"
    contract = product_profile.get("proof_contract")
    if not isinstance(contract, dict) or contract.get("valid") is not True:
        return str(contract.get("validation_reason") or "证明合同不合法") if isinstance(contract, dict) else "缺少 proof_contract"
    return ""


def has_product_foundation_anchor(analysis: dict[str, Any]) -> bool:
    """判断 Step-0 是否有足够产品锚点；纯英文品牌/型号不算可靠锚点。"""
    product = analysis.get("product") if isinstance(analysis.get("product"), dict) else {}
    if isinstance(analysis.get("brand_proposition"), dict) and analysis["brand_proposition"]:
        return True
    for key in ("category", "core_selling_points", "target_user", "purchase_motivation", "notes"):
        value = str(product.get(key) or "").strip()
        if value and value not in {"未填写", "未提供", "无"}:
            return True
    name = str(product.get("name") or "").strip()
    if not name or name in {"未填写", "未提供"}:
        return False
    return any("\u4e00" <= ch <= "\u9fff" for ch in name)


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
