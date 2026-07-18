"""flayr_core.llm.pipeline：LLM 分析主入口。

三个 public 函数：
  - run_comparison_scope_preflight  只建立锁定事实与双视频产品比较资格
  - run_large_model_analysis        从 analysis_input.md 跑完整分析，写出 analysis_result.json
  - parse_and_validate_llm_result   解析模型原始输出，必要时做一次 repair 重试
  - merge_analysis_result           把外部提供的 analysis_result.json 合并进 analysis dict

所有入口通过 finalize_analysis_result 走同一条完整处理链，避免外部 JSON 和实时 LLM
因校验顺序不同而产生不同报告。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from ..utils import write_json, write_text
from ..structure_modules import canonical_module_id
from .api import call_llm_api, extract_chat_completion_text, read_llm_api_key
from .analysis_contract import AnalysisContractError, validate_normalized_analysis_contract
from .parse import (
    normalize_analysis_result,
    normalize_category_profile,
    normalize_comparison_contract,
    normalize_absolute_execution_shadow,
    normalize_product_profile,
    normalize_video_product_identity,
    normalize_video_fact_result,
    parse_json_text,
)
from .payload import (
    build_improvement_reconciliation_payload,
    build_comparison_eligibility_payload,
    build_absolute_execution_shadow_payload,
    build_llm_comparison_payload,
    build_llm_payload,
    build_llm_repair_payload,
    build_product_foundation_payload,
    build_product_foundation_repair_payload,
    build_stage_review_payload,
    build_video_identity_payload,
    build_video_fact_payload,
    load_brand_proposition,
)
from .s4_visual_verifier import maybe_apply_s4_visual_verifier
from .media import select_role_visual_inputs
from ..postprocess import apply_postprocess_chain
from ..postprocess.derive import critical_severity_stages
from ..postprocess.global_diagnosis import materialize_global_diagnosis
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


# 修改 build_video_fact_payload 的语义合同后必须递增，避免旧 facts 与新判断规则混用。
VIDEO_FACT_CACHE_SCHEMA_VERSION = 7


# ---------------------------------------------------------------------------
# 外部 JSON 合并入口
# ---------------------------------------------------------------------------

def run_comparison_scope_preflight(
    args: argparse.Namespace,
    analysis: dict[str, Any],
    run_dir: Path,
) -> dict[str, Any]:
    """只跑事实抽取和产品级比较资格，供验证集入场审计使用。

    资格判断只依赖锁定的双侧产品身份，不应为它支付阶段对比、Phase C 或提升点的成本。
    事实与资格文件仍按完整链路同名落盘，因此后续可在同一目录继续运行完整分析。
    """
    api_key = read_llm_api_key(args).strip()
    if not api_key and not args.llm_dry_run:
        raise SystemExit("比较资格预检需要 LLM API key。")
    facts = run_video_identity_extraction(args, analysis, run_dir, api_key)
    if args.llm_dry_run:
        return normalize_comparison_contract({"reason": "dry run 未调用事实抽取和资格判定。"})
    eligibility = establish_comparison_eligibility(args, facts, run_dir, api_key)
    analysis["comparison_contract"] = eligibility
    analysis["comparison_eligibility"] = eligibility
    analysis["video_understanding"] = facts
    analysis["analysis_source"] = {
        "type": "comparison_scope_preflight",
        "merged_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    return eligibility

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
    analysis["comparison_contract"] = normalized.get("comparison_contract", {})
    analysis["comparison_eligibility"] = normalized.get("comparison_eligibility", {})
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
        "absolute_quality",
        "absolute_execution_shadow",
        "computed_loop_closure",
        "qa_warnings",
        "quality_audit",
        "improvement_reconciliation",
        "s4_visual_verifier",
        "global_diagnosis",
        "commercial_priorities",
        "commercial_priority_summary",
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
    # 全局门控须在提升点完成最终过滤后生成，避免商业优先级引用已被清除的建议。
    materialize_global_diagnosis(normalized, analysis)
    validate_quality_contract(normalized, analysis)
    return normalized


def merge_analysis_result(analysis: dict[str, Any], result_path: Path, analysis_input: str) -> None:
    """把外部 analysis_result.json 经唯一处理链后合并入 analysis。"""
    result = json.loads(result_path.read_text(encoding="utf-8"))
    stages = result.get("stage_analysis") if isinstance(result.get("stage_analysis"), list) else []
    has_structured_s5 = any(
        isinstance(stage, dict)
        and str(stage.get("stage") or "").upper().startswith("S5")
        and any(isinstance(stage.get(f"{role}_s5"), dict) for role in ("creator", "benchmark"))
        for stage in stages
    )
    if has_structured_s5:
        # 外部导入也必须使用和主链相同的 S5 来源门禁，避免入口不同导致背书结论漂移。
        analysis["s5_source_signals_required"] = True
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
    request_max_time_seconds: int | None = None,
) -> str:
    """调用 LLM 并确保返回内容是可解析 JSON；静默截断时整体重取。

    DashScope 大响应偶发在传输途中被截断（无错误、finish_reason=None、body 不完整），
    导致 JSON 残缺。这类截断 repair 无效（重发也会截断），唯一可靠解是重取整次调用。
    重试 max_attempts 次仍不完整，则返回最后一次内容，交由下游 repair 兜底。
    """
    last_text = ""
    for attempt in range(max_attempts):
        request_options = {}
        if request_max_time_seconds is not None:
            request_options["max_time_seconds"] = request_max_time_seconds
        try:
            raw_text = call_llm_api(args.llm_api_url, api_key, payload_path, raw_path, **request_options)
        except SystemExit:
            # 底层已做同一 SSE 请求的传输重试；仍失败时重取完整响应，不能让单次网络中断终止整条 pipeline。
            if attempt + 1 >= max_attempts:
                raise
            time.sleep(5 * (attempt + 1))
            continue
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


def _stable_digest(value: Any) -> str:
    """生成跨运行稳定的内容摘要；缓存 key 只依赖可审计输入。"""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _source_video_hash(analysis: dict[str, Any], role: str) -> str:
    videos = analysis.get("videos") if isinstance(analysis.get("videos"), dict) else {}
    info = videos.get(role) if isinstance(videos, dict) else None
    fingerprint = info.get("preprocess_fingerprint") if isinstance(info, dict) else None
    source = fingerprint.get("source_video") if isinstance(fingerprint, dict) else None
    return str(source.get("sha256") or "") if isinstance(source, dict) else ""


def _cache_path(run_dir: Path, namespace: str, key: dict[str, Any]) -> Path | None:
    """缓存归属输出目录父级，避免依赖本地 run 名称，也便于未来线上换存储实现。"""
    source_hash = str(key.get("source_video_sha256") or "")
    if not source_hash:
        return None
    return run_dir.parent / namespace / f"{_stable_digest(key)}.json"


def _read_cache_result(path: Path | None, result_key: str) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    result = cached.get(result_key) if isinstance(cached, dict) else None
    return result if isinstance(result, dict) else None


def _write_cache_result(path: Path | None, record: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, record)


def _video_fact_cache_key(args: argparse.Namespace, analysis: dict[str, Any], role: str) -> dict[str, Any]:
    foundation = analysis.get("product_foundation") if isinstance(analysis.get("product_foundation"), dict) else {}
    return {
        "cache_schema_version": VIDEO_FACT_CACHE_SCHEMA_VERSION,
        "source_video_sha256": _source_video_hash(analysis, role),
        "role": role,
        "llm_model": str(args.llm_model or ""),
        "foundation_digest": _stable_digest(foundation),
    }


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
        product = analysis.get("product") if isinstance(analysis.get("product"), dict) else {}
        brand_proposition = load_brand_proposition(
            run_dir,
            str(product.get("proposition_key") or ""),
        )
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
        maybe_run_absolute_execution_shadow(args, analysis, facts, run_dir, api_key)
        comparison_contract = establish_comparison_eligibility(args, facts, run_dir, api_key)
        analysis["comparison_contract"] = comparison_contract
        analysis["comparison_eligibility"] = comparison_contract
        if comparison_contract.get("overall_status") in {"not_comparable", "uncertain"}:
            _apply_non_comparable_result(analysis, facts, comparison_contract, run_dir)
            return None
        analysis["s1_hook_flags_required"] = True
        analysis["structured_relevance_required"] = True
        analysis["s2_flags_required"] = True
        analysis["s3_flags_required"] = True
        analysis["s4_flags_required"] = True
        analysis["s5_flags_required"] = True
        analysis["s5_source_signals_required"] = True
        analysis["s6_flags_required"] = True
        analysis["multimodal_assessment_required"] = True
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
    result["analysis_run_metadata"] = {
        "llm_model": str(args.llm_model or ""),
        "llm_api_url": str(args.llm_api_url or ""),
        "comparison_temperature": payload.get("temperature"),
        "multimodal_input": bool(args.llm_include_images),
    }
    result_path = run_dir / "analysis_result.json"
    write_json(result_path, result)
    return result_path, result


def maybe_run_absolute_execution_shadow(
    args: argparse.Namespace,
    analysis: dict[str, Any],
    facts: dict[str, Any],
    run_dir: Path,
    api_key: str,
) -> None:
    """按开关运行单侧执行审计；失败只记录结果，不得中断主分析。

    审计读取的仅是每侧 Stage1 锁定事实，因此不会接触另一侧视频或主对比的
    stage_analysis。当前是 shadow mode：结果不进入 severity，也不缓存评分结果；
    这样才能如实测量模型在相同 facts 下的方差，而不是把首次随机结果伪装成稳定。
    """
    if not getattr(args, "absolute_execution_shadow", False):
        return
    audit: dict[str, Any] = {"status": "pending", "roles": {}, "errors": []}
    for role in ("benchmark", "creator"):
        if not isinstance(facts.get(role), dict):
            audit["errors"].append(f"{role}: 缺少锁定单视频事实")
            continue
        try:
            request_path = run_dir / f"llm_absolute_execution_{role}_request.json"
            response_path = run_dir / f"llm_absolute_execution_{role}_response.json"
            payload = build_absolute_execution_shadow_payload(args.llm_model, role, facts, analysis)
            write_json(request_path, payload)
            response_text = fetch_json_completion(args, api_key, request_path, response_path)
            parsed = normalize_absolute_execution_shadow(role, parse_json_text(response_text))
            if parsed is None:
                raise SystemExit("单侧审计缺少完整 S1-S4 枚举输出")
            audit["roles"][role] = parsed
            write_json(run_dir / f"absolute_execution_{role}.json", parsed)
        except (OSError, ValueError, SystemExit, json.JSONDecodeError) as exc:
            audit["errors"].append(f"{role}: {exc}")
    audit["status"] = "completed" if len(audit["roles"]) == 2 else "partial" if audit["roles"] else "failed"
    analysis["absolute_execution_shadow"] = audit


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
    raw_result: dict[str, Any] | None = None
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
        raw_repair_result = preserve_valid_repair_sections(raw_result, parse_json_text(repair_result_text))
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


def preserve_valid_repair_sections(
    original: dict[str, Any] | None,
    repaired: dict[str, Any],
) -> dict[str, Any]:
    """Repair 只覆盖它实际输出的字段，避免一次小修复清空完整阶段结果。"""
    if not isinstance(original, dict):
        return repaired
    repaired = dict(repaired)
    original_stages = original.get("stage_analysis")
    repaired_stages = repaired.get("stage_analysis")
    if isinstance(original_stages, list) and isinstance(repaired_stages, list):
        merged_stages: list[Any] = []
        for index, repaired_stage in enumerate(repaired_stages, start=1):
            original_stage = original_stages[index - 1] if index <= len(original_stages) else None
            if not isinstance(original_stage, dict) or not isinstance(repaired_stage, dict):
                merged_stages.append(repaired_stage)
                continue
            merged_stage = dict(repaired_stage)
            for key, value in original_stage.items():
                current = merged_stage.get(key)
                if key not in merged_stage or current is None or (isinstance(current, str) and not current.strip()):
                    if key in {"creator_module_id", "benchmark_module_id"}:
                        merged_stage[key] = canonical_module_id(value, index)
                    else:
                        merged_stage[key] = json.loads(json.dumps(value, ensure_ascii=False))
            merged_stages.append(merged_stage)
        repaired["stage_analysis"] = merged_stages
    repaired_improvements = repaired.get("improvements")
    original_improvements = original.get("improvements")
    if (
        (not isinstance(repaired_improvements, list) or not repaired_improvements)
        and isinstance(original_improvements, list)
        and 1 <= len(original_improvements) <= 5
    ):
        repaired["improvements"] = json.loads(json.dumps(original_improvements, ensure_ascii=False))
    return repaired


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
        and str(stage.get("comparison_status") or "") not in {"not_directly_comparable", "not_applicable"}
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
    stages_by_code = {
        stage_code(stage.get("stage")): stage
        for stage in merged.get("stage_analysis", [])
        if isinstance(stage, dict)
    }
    existing = [
        item
        for item in merged.get("improvements", [])
        if isinstance(item, dict)
        and str(stages_by_code.get(stage_code(item.get("target_stage")), {}).get("comparison_status") or "")
        not in {"not_directly_comparable", "not_applicable"}
    ]
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
    before_stages = [
        json.loads(json.dumps(stage, ensure_ascii=False))
        for stage in result.get("stage_analysis") or []
        if isinstance(stage, dict) and stage_code(stage.get("stage")) in stage_codes
    ]

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
            "before_stage_analysis": before_stages,
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
            fallback_improvements=raw_result.get("improvements"),
        )
    except (SystemExit, json.JSONDecodeError) as exc:
        result["phase_c_review"] = {
            "requested_stages": stage_codes,
            "applied": False,
            "reason": f"低置信阶段回看失败：{exc}",
            "before_stage_analysis": before_stages,
        }
        return result

    refined["phase_c_review"] = {
        "requested_stages": stage_codes,
        "applied": True,
        "response_path": str(review_response_path),
        "notes": review_result.get("review_notes", []),
        "before_stage_analysis": before_stages,
        "after_stage_analysis": [
            json.loads(json.dumps(stage, ensure_ascii=False))
            for stage in refined.get("stage_analysis") or []
            if isinstance(stage, dict) and stage_code(stage.get("stage")) in stage_codes
        ],
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
    fallback_improvements: Any = None,
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
    # Phase C 只更新阶段。若初轮后处理已过滤掉所有提升点，恢复初轮已通过 schema 的原始条目，
    # 使重走统一收口时满足 raw envelope；最终仍由既有过滤/重排逻辑决定是否保留。
    if not merged.get("improvements") and isinstance(fallback_improvements, list) and fallback_improvements:
        merged["improvements"] = json.loads(json.dumps(fallback_improvements, ensure_ascii=False))
    merged_stages = []
    for stage in merged.get("stage_analysis", []):
        code = stage_code(stage.get("stage"))
        if code in updates_by_code:
            # 字段级合并而非整字典替换：回看若漏掉执行分等新字段，保留原值，
            # 否则 derive 对复核阶段反而退化为模型直判（code review #1）。
            base_stage = dict(stage)
            # 跨模态净效果依赖本次回看片段。Phase C 更新任一阶段时必须重判，
            # 不能因模型漏字段而沿用旧证据上的综合结论。
            base_stage.pop("creator_multimodal", None)
            base_stage.pop("benchmark_multimodal", None)
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
    cache_path = run_dir / "product_foundation.json"
    if getattr(args, "reuse_preprocessing", False) and cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = None
        if isinstance(cached, dict) and (
            isinstance(cached.get("category_profile"), dict)
            or isinstance(cached.get("product_profile"), dict)
        ):
            _stamp_proof_contract_source(cached, analysis)
            return cached
    payload = build_product_foundation_payload(args.llm_model, analysis)
    request_path = run_dir / "llm_product_foundation_request.json"
    response_path = run_dir / "llm_product_foundation_response.json"
    write_json(request_path, payload)
    if args.llm_dry_run:
        return None
    try:
        result_text = fetch_json_completion(
            args,
            api_key,
            request_path,
            response_path,
            request_max_time_seconds=240,
        )
        raw = parse_json_text(result_text)
        foundation = {
            "category_profile": normalize_category_profile(raw.get("category_profile")),
            "product_profile": normalize_product_profile(raw.get("product_profile")),
        }
        _stamp_proof_contract_source(foundation, analysis)
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
            repair_text = fetch_json_completion(
                args,
                api_key,
                repair_request_path,
                repair_response_path,
                request_max_time_seconds=240,
            )
            repaired_raw = parse_json_text(repair_text)
            foundation = {
                "category_profile": normalize_category_profile(repaired_raw.get("category_profile")),
                "product_profile": normalize_product_profile(repaired_raw.get("product_profile")),
            }
            _stamp_proof_contract_source(foundation, analysis)
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
    write_json(cache_path, foundation)
    return foundation


def establish_comparison_eligibility(
    args: argparse.Namespace,
    facts: dict[str, Any],
    run_dir: Path,
    api_key: str,
) -> dict[str, Any]:
    """独立判定双视频能否做产品级比较；失败时保守退回 uncertain。"""
    payload = build_comparison_eligibility_payload(args.llm_model, facts)
    request_path = run_dir / "llm_comparison_eligibility_request.json"
    response_path = run_dir / "llm_comparison_eligibility_response.json"
    write_json(request_path, payload)
    try:
        result_text = fetch_json_completion(args, api_key, request_path, response_path)
        eligibility = normalize_comparison_contract(parse_json_text(result_text))
        if eligibility["overall_status"] == "uncertain" and not eligibility["reason"]:
            eligibility["reason"] = "双侧产品身份不足以确认产品级比较资格。"
    except Exception as exc:  # 资格层不允许阻断主分析；uncertain 会阻止后续误用为直接产品比较。
        eligibility = normalize_comparison_contract(
            {"reason": f"产品级比较资格判定失败，保守按 uncertain 处理：{exc}"}
        )
    eligibility = _stamp_facts_eligibility(eligibility)
    eligibility = _apply_operator_scope_override(
        eligibility,
        getattr(args, "comparison_scope_override", None),
    )
    write_json(run_dir / "comparison_contract.json", eligibility)
    write_json(run_dir / "comparison_eligibility.json", eligibility)
    return eligibility


def _stamp_facts_eligibility(eligibility: dict[str, Any]) -> dict[str, Any]:
    """让事实预检的审计字段与其判定结论保持一致。

    资格预检的唯一输入是锁定的双侧产品身份事实，因此不能保留主比较模型或
    normalize 默认值带来的 ``facts_scope=uncertain``。人工结构对标覆盖在此之后
    单独处理，并把这份事实结论作为原始审计记录保留下来。
    """
    normalized = normalize_comparison_contract(eligibility)
    normalized["scope_origin"] = "facts"
    normalized["facts_scope"] = normalized["scope"]
    normalized["facts_reason"] = normalized["reason"]
    return normalized


def _apply_operator_scope_override(
    facts_eligibility: dict[str, Any],
    override: str | None,
) -> dict[str, Any]:
    """应用人工确认的结构对标范围，同时保留 facts 身份审计结论。

    模型绝不能自行把跨品样本升级为同任务结构对标。该范围只能由运营或验证清单显式提供，
    因此未来线上任务也应传递元数据，而不能从目录名或产品名推断。
    """
    if override != "same_task_structure":
        return facts_eligibility
    overridden = normalize_comparison_contract(facts_eligibility)
    overridden["scope_origin"] = "operator_certified"
    overridden["facts_scope"] = str(facts_eligibility.get("scope") or "uncertain")
    overridden["facts_reason"] = str(facts_eligibility.get("reason") or "双侧产品身份事实不足。")
    if overridden.get("identity_relation") == "different_product":
        overridden["substitution_relation"] = "strong_substitute"
        shared = dict(overridden.get("shared_job") or {})
        shared.update(
            {
                "same_consumer_job": True,
                "same_target_object": True,
                "same_desired_outcome": True,
                "same_purchase_decision": True,
                "complement_or_dependency": False,
            }
        )
        overridden["shared_job"] = shared
    overridden["reason"] = (
        "运营确认双方共享消费者任务与替代关系；各阶段仍按已锁定 stage_eligibility 单独决定是否可比，"
        "不得据此固定开放 S1-S4/S6。"
    )
    return normalize_comparison_contract(overridden)


def _apply_non_comparable_result(
    analysis: dict[str, Any],
    facts: dict[str, Any],
    contract: dict[str, Any],
    run_dir: Path,
) -> None:
    """无替代或身份不确定时在主对比前结束，不伪造 S1-S6 占位结论。"""
    uncertain = contract.get("overall_status") == "uncertain"
    reason = str(contract.get("reason") or "双侧产品缺少共同消费者任务，不能进行带货内容对比。")
    analysis["analysis_status"] = "comparison_uncertain" if uncertain else "not_comparable"
    analysis["one_line_verdict"] = "商品关系不确定，暂不分析" if uncertain else "两条视频不具备比较资格"
    analysis["one_line_summary"] = reason
    analysis["executive_summary"] = reason
    analysis["key_conclusions"] = [reason]
    analysis["comparison_contract"] = contract
    analysis["comparison_eligibility"] = contract
    analysis["video_understanding"] = facts
    analysis["stage_analysis"] = []
    analysis["improvements"] = []
    analysis["improvements_status"] = "not_applicable"
    analysis["analysis_source"] = {
        "type": "comparison_contract_gate",
        "merged_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    write_json(
        run_dir / "comparison_rejection.json",
        {"analysis_status": analysis["analysis_status"], "comparison_contract": contract, "reason": reason},
    )


def _stamp_proof_contract_source(foundation: dict[str, Any], analysis: dict[str, Any]) -> None:
    """给 Step-0 合同标注证据来源，限制下游复核器的覆盖权限。

    运营提供核心卖点只证明“产品卖什么”，不证明“哪一个卖点应成为唯一 S4 主视觉信号”。
    后者若仍由 Step-0 模型排序/生成，就必须标为 inferred，避免视觉复核器把模型选尺子
    误当运营裁决，再反向否决视频中已经观察到的其他有效视觉效果。

    默认标为 inferred。只有 --primary-selling-point 能唯一对应证明计划 candidate 时才标为
    operator；模型输出或普通 core_selling_points 文本不能自行抬升来源等级。
    """
    profile = foundation.get("product_profile")
    if not isinstance(profile, dict):
        return
    profile["proof_contract_source"] = "inferred"
    product = analysis.get("product") if isinstance(analysis.get("product"), dict) else {}
    operator_point = str(product.get("primary_selling_point") or "").strip()
    plan = profile.get("short_video_proof_plan") if isinstance(profile.get("short_video_proof_plan"), dict) else {}
    candidates = plan.get("candidates") if isinstance(plan.get("candidates"), list) else []
    if not operator_point or not plan.get("valid"):
        return
    normalized_operator = re.sub(r"[^\w\u4e00-\u9fff]+", "", operator_point.lower())
    matches = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        selling_point = str(candidate.get("selling_point") or "").strip()
        normalized_candidate = re.sub(r"[^\w\u4e00-\u9fff]+", "", selling_point.lower())
        if normalized_operator and (
            normalized_operator in normalized_candidate or normalized_candidate in normalized_operator
        ):
            matches.append(candidate)
    if len(matches) != 1:
        profile["proof_contract_validation_warning"] = "运营主卖点未能唯一对应 short_video_proof_plan candidate"
        return
    plan["primary_candidate_id"] = str(matches[0].get("id") or "")
    plan["selection_source"] = "operator_priority"
    plan["anchor_confidence"] = "high"
    profile["proof_contract_source"] = "operator"


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
        result_path = run_dir / f"video_facts_{role}.json"
        cache_key = _video_fact_cache_key(args, analysis, role)
        cache_path = _cache_path(run_dir, ".video_fact_cache", cache_key)
        cached = None if args.llm_dry_run else _read_cache_result(cache_path, "fact_result")
        if cached is not None:
            cached.setdefault("temporal_evidence_mode", "unknown")
            facts[role] = cached
            write_json(result_path, cached)
            continue
        visual_inputs = select_role_visual_inputs(videos[role], role, per_role_limit)
        payload = build_video_fact_payload(args.llm_model, role, analysis, visual_inputs)
        request_path = run_dir / f"llm_facts_{role}_request.json"
        response_path = run_dir / f"llm_facts_{role}_response.json"
        write_json(request_path, payload)
        if args.llm_dry_run:
            continue
        result_text = fetch_json_completion(args, api_key, request_path, response_path)
        fact_result = normalize_video_fact_result(role, parse_json_text(result_text), analysis)
        # 能力状态取自实际请求载荷，不让模型猜自己是否看到了连续视频。
        fact_result["temporal_evidence_mode"] = "full_temporal" if payload_has_video(payload) else "static_only"
        facts[role] = fact_result
        write_json(result_path, fact_result)
        _write_cache_result(cache_path, {**cache_key, "fact_result": fact_result})
    return facts


def run_video_identity_extraction(
    args: argparse.Namespace,
    analysis: dict[str, Any],
    run_dir: Path,
    api_key: str,
) -> dict[str, Any]:
    """scope 预检专用：每侧只取产品身份，避免完整事实抽取的原生视频和音频成本。"""
    identities: dict[str, Any] = {}
    videos = analysis.get("videos", {})
    for role in ("benchmark", "creator"):
        if role not in videos:
            continue
        payload = build_video_identity_payload(
            args.llm_model,
            role,
            analysis,
            select_role_visual_inputs(videos[role], role, image_limit=2),
        )
        request_path = run_dir / f"llm_identity_{role}_request.json"
        response_path = run_dir / f"llm_identity_{role}_response.json"
        result_path = run_dir / f"video_identity_{role}.json"
        write_json(request_path, payload)
        if args.llm_dry_run:
            continue
        result_text = fetch_json_completion(args, api_key, request_path, response_path)
        identity = {"product_identity": normalize_video_product_identity(parse_json_text(result_text).get("product_identity"))}
        identities[role] = identity
        write_json(result_path, identity)
    return identities
