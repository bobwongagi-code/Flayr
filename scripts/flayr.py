#!/usr/bin/env python3
"""Flayr MVP command line runner."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from flayr_core.audio_quality import analyze_audio_quality
from flayr_core.analysis_model import ANALYSIS_RESULT_CONTRACT, placeholder_stages
from flayr_core.artifacts import resolve_artifact_path
from flayr_core.bd_report import write_bd_report
from flayr_core.asr import (
    ASR_FAILURE_PLACEHOLDER,
    DEFAULT_FUN_ASR_API_URL,
    DEFAULT_FUN_ASR_MODEL,
    read_asr_api_key,
    run_online_asr,
)
from flayr_core.llm.api import can_analyze_native_audio, provider_capabilities, read_llm_api_key
from flayr_core.llm.provider_artifacts import ProviderCallError, ProviderReplayError
from flayr_core.llm.pipeline import (
    AnalysisPipelineError,
    apply_finalized_analysis_result,
    merge_analysis_result,
    run_comparison_scope_preflight,
    run_large_model_analysis,
)
from flayr_core.prompt import write_analysis_input
from flayr_core.creator_report import write_creator_report
from flayr_core.report import write_report
from flayr_core.resources import ResourceBudget, ResourceBudgetExceeded, ResourceLimits, finite_nonnegative
from flayr_core.run_manifest import SUCCESS_MANIFEST_NAME, command_digest, write_success_manifest
from flayr_core.run_state import (
    COMPLETED,
    DEGRADED,
    PROCESSING,
    RUN_STATE_FILE,
    RunStateError,
    begin_report_generation,
    initialize_run_state,
    read_run_state,
    recover_run_state,
    reset_run_state,
    transition_run_state,
)
from flayr_core.motion import compute_shake_metric
from flayr_core.market import normalize_target_market
from flayr_core.shot_track import build_shot_track
from flayr_core.speech_mode import classify_speech_mode
from flayr_core.subtitle_track import build_subtitle_track
from flayr_core.translation import sync_chinese_translation, translate_transcript_with_llm
from flayr_core.utils import write_json, write_text
from flayr_core.verification_order import assert_verification_order
from flayr_core.video import (
    extract_audio,
    extract_anchor_frames,
    extract_frames,
    probe_duration_seconds,
    reserve_existing_media_artifacts,
)
from flayr_core.video_evidence import (
    TRANSCRIPT_WINDOW_CONTRACT_VERSION,
    build_video_evidence_artifacts,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS_DIR = ROOT / "runs"
PREPROCESS_CACHE_SCHEMA_VERSION = 4
PREPROCESS_PIPELINE_VERSION = "2026-08-03.1-online-asr"
PREPROCESS_ARTIFACT_SCHEMA_VERSION = 2
ASR_AUDIO_PLACEHOLDER = "Online ASR unavailable because audio extraction failed."
_RUN_ROLE_DIRS = frozenset({"benchmark", "creator"})
_RUN_OUTPUT_FILES = frozenset(
    {
        SUCCESS_MANIFEST_NAME,
        "analysis.json",
        "analysis_input.md",
        "analysis_result.json",
        "comparison_contract.json",
        "comparison_eligibility.json",
        "comparison_rejection.json",
        "bd_report.html",
        "creator_report.html",
        "degraded_manifest.json",
        "final_derived_result.json",
        "failure.json",
        "postprocess_change_log.json",
        "product_foundation.json",
        "raw_model_response.json",
        "report.html",
        "validated_normalized_result.json",
        "stage1_to_stage2_handoff.json",
        "stage_group_S1_S2.json",
        "stage_group_S3_S4.json",
        "stage_group_S5.json",
        "stage_group_S6.json",
    }
)
_RUN_OUTPUT_PREFIXES = (
    "absolute_execution_",
    "llm_",
    "stage2_provider_",
    "video_facts_",
    "video_identity_",
)


def _record_run_failure(
    run_dir: Path,
    reason: str,
    *,
    failure_kind: str = "pipeline",
    phase: str = "",
    cause_type: str = "",
) -> None:
    try:
        write_json(
            run_dir / "failure.json",
            {
                "schema_version": 1,
                "status": "failed",
                "failure_kind": str(failure_kind or "pipeline"),
                "phase": str(phase or ""),
                "cause_type": str(cause_type or ""),
                "reason": str(reason or "")[:500],
            },
        )
    except OSError:
        pass
    try:
        recover_run_state(run_dir, "FAILED", reason=reason[:500])
    except RunStateError:
        # The web worker performs the same recovery check after a child exits.
        # A CLI failure must never hide its original exception behind cleanup.
        pass


def _transcription_issues(videos: dict[str, dict[str, Any]]) -> list[str]:
    """Return stable, non-sensitive reasons for videos without completed ASR."""
    issues: list[str] = []
    for role, info in videos.items():
        status = str(info.get("transcription_status") or "missing").strip().lower()
        if status != "completed":
            issues.append(f"{role}: online Fun-ASR transcription_status={status}")
    return issues


def _mark_analysis_degraded(run_dir: Path, analysis: dict[str, Any], reasons: list[str]) -> None:
    """Publish an explicit degraded marker without claiming a completed run."""
    existing = analysis.get("degraded_flags")
    existing_items = existing if isinstance(existing, list) else []
    flags = [str(item).strip() for item in existing_items if str(item).strip()]
    for reason in reasons:
        if reason not in flags:
            flags.append(reason)
    analysis["degraded_flags"] = flags
    analysis["analysis_run_state"] = "degraded"
    write_json(
        run_dir / "degraded_manifest.json",
        {
            "analysis_run_state": "degraded",
            "degraded_flags": flags,
            "reason": "；".join(flags),
            "stage_analysis": analysis.get("stage_analysis", []),
            "improvements": analysis.get("improvements", []),
        },
    )


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        limits = ResourceLimits(max_total_wall_time=args.max_total_wall_time)
    except ValueError as exc:
        raise SystemExit(f"--max-total-wall-time 无效：{exc}") from exc
    budget = ResourceBudget(limits)
    # 所有预处理、OCR、LLM、下载、报告和子进程都从这个 run 级对象取预算。
    budget.activate()
    args._resource_budget = budget

    deps = check_dependencies(args)
    inputs = validate_inputs(args)
    if args.verification_stage:
        if args.verification_stage != "production":
            assert_verification_order(args.verification_root, args.verification_stage)
    source_durations: dict[str, float] = {}
    for role, path in inputs.items():
        budget.preflight_source(path)
        duration = probe_duration_seconds(path)
        source_durations[role] = budget.register_source(path, duration)
    deps["source_durations"] = source_durations
    run_dir = create_run_dir(args)
    initialize_run_state(run_dir)
    lifecycle = read_run_state(run_dir)
    if lifecycle and lifecycle.get("state") == "CREATED":
        transition_run_state(run_dir, PROCESSING)
    # A direct rerun must invalidate an old completion marker before any new
    # artifact is written. The batch runner also removes invalid markers.
    (run_dir / SUCCESS_MANIFEST_NAME).unlink(missing_ok=True)

    videos: dict[str, dict[str, Any]] = {}
    try:
        for role, path in inputs.items():
            videos[role] = process_video(role, path, run_dir, deps, args, budget=budget)
    except Exception as exc:
        if isinstance(exc, ProviderReplayError):
            failure_kind = "provider_replay"
        elif isinstance(exc, ProviderCallError):
            failure_kind = "provider_call"
        else:
            failure_kind = "media_processing"
        _record_run_failure(
            run_dir,
            f"素材处理失败：{exc}",
            failure_kind=failure_kind,
            phase="preprocessing",
            cause_type=exc.__class__.__name__,
        )
        raise

    try:
        analysis = build_analysis(args, run_dir, deps, videos, budget=budget)
    except Exception as exc:
        _record_run_failure(run_dir, f"分析初始化失败：{exc}")
        raise
    analysis_input_path = write_analysis_input(run_dir, analysis)
    transcription_issues = [] if args.legacy_import else _transcription_issues(videos)
    if transcription_issues and args.mode != "scope" and not getattr(args, "allow_degraded", False):
        analysis["degraded_flags"] = transcription_issues
        write_json(run_dir / "analysis.json", analysis)
        reason = "在线 Fun-ASR 未完成，当前模式不允许发布未完整转写的结果：" + "；".join(
            transcription_issues
        )
        _record_run_failure(run_dir, reason)
        raise SystemExit(reason)
    if args.mode == "scope":
        eligibility = run_comparison_scope_preflight(args, analysis, run_dir)
        analysis["resource_budget"] = budget.snapshot()
        write_json(run_dir / "analysis.json", analysis)
        write_analysis_input(run_dir, analysis)
        print_scope_summary(run_dir, deps, videos, eligibility)
        return 0
    if args.llm_model and not args.analysis_result_json:
        try:
            completed = run_large_model_analysis(args, analysis, analysis_input_path, run_dir)
        except AnalysisPipelineError as exc:
            _record_run_failure(
                run_dir,
                f"分析管线失败（{exc.phase}/{exc.failure_kind}）：{exc}",
                failure_kind=exc.failure_kind,
                phase=exc.phase,
                cause_type=exc.cause_type,
            )
            raise
        except (Exception, SystemExit) as exc:
            _record_run_failure(
                run_dir,
                f"分析编排失败：{exc}",
                failure_kind="analysis_orchestration",
                phase="analysis_entrypoint",
                cause_type=exc.__class__.__name__,
            )
            raise
        if completed:
            llm_result_path, normalized_result = completed
            apply_finalized_analysis_result(analysis, normalized_result, llm_result_path)
    elif args.analysis_result_json:
        merge_analysis_result(
            analysis,
            args.analysis_result_json,
            analysis_input_path.read_text(encoding="utf-8"),
            legacy_import=args.legacy_import,
        )
    if args.mode in {"compare", "improve"} and analysis.get("analysis_run_state") == "not_run":
        if not getattr(args, "allow_degraded", False):
            write_json(run_dir / "analysis.json", analysis)
            _record_run_failure(run_dir, "compare/improve 未运行完成的 LLM 分析。")
            raise SystemExit(
                "compare/improve 需要完成的 LLM 分析，但当前 analysis_run_state=not_run。"
                " 提供 --llm-model 跑分析，或加 --allow-degraded 在无分析时继续（severity 留空）。"
            )
        _mark_analysis_degraded(
            run_dir,
            analysis,
            ["LLM 分析未运行或未完成；severity/improvements 为占位，不可作为业务判断。"],
        )
    if transcription_issues:
        _mark_analysis_degraded(run_dir, analysis, transcription_issues)
    analysis["resource_budget"] = budget.snapshot()
    write_json(run_dir / "analysis.json", analysis)
    write_analysis_input(run_dir, analysis)

    report_path = _generate_reports_and_publish(
        run_dir,
        args,
        inputs,
        analysis,
        budget,
    )
    print_summary(run_dir, report_path, deps, videos)
    return 0


def _generate_reports_and_publish(
    run_dir: Path,
    args: argparse.Namespace,
    inputs: dict[str, Path],
    analysis: dict[str, Any],
    budget: ResourceBudget,
) -> Path:
    try:
        begin_report_generation(
            run_dir,
            artifacts=("analysis.json", "analysis_input.md"),
        )
    except RunStateError as exc:
        _record_run_failure(run_dir, f"无法进入报告生成状态：{exc}")
        raise SystemExit(f"无法进入报告生成状态：{exc}") from exc

    try:
        analysis["resource_budget"] = budget.snapshot()
        report_path = write_report(run_dir, analysis, budget=budget)
        if args.mode in {"compare", "improve"}:
            report_path = write_bd_report(run_dir, analysis, budget=budget)
            write_creator_report(run_dir, analysis, budget=budget)
        if args.mode in {"compare", "improve"} and analysis.get("analysis_run_state") == "completed":
            write_success_manifest(
                run_dir,
                {
                    "benchmark_video": inputs["benchmark"],
                    **({"creator_video": inputs["creator"]} if "creator" in inputs else {}),
                    **({"analysis_result_json": args.analysis_result_json} if args.analysis_result_json else {}),
                },
                analysis,
                {
                    "mode": args.mode,
                    "code_commit": _git_commit_sha(),
                    "argv_sha256": command_digest(sys.argv[1:]),
                    "llm_model": str(args.llm_model or ""),
                    "llm_api_url": str(args.llm_api_url or ""),
                },
            )
            transition_run_state(
                run_dir,
                COMPLETED,
                artifacts=(
                    SUCCESS_MANIFEST_NAME,
                    "analysis.json",
                    "report.html",
                    "bd_report.html",
                    "creator_report.html",
                ),
            )
        elif args.mode in {"compare", "improve"} and analysis.get("analysis_run_state") == "degraded":
            segmented = analysis.get("segmented_pipeline")
            unresolved = (
                segmented.get("unresolved_stages")
                if isinstance(segmented, dict)
                else []
            )
            reasons = list(analysis.get("degraded_flags") or [])
            if unresolved:
                reasons.append("Stage2 未闭合阶段：" + ",".join(str(item) for item in unresolved))
            if not reasons:
                reasons.append("分析链路已降级，不能发布 completed 成功标记。")
            _mark_analysis_degraded(run_dir, analysis, reasons)
            write_json(run_dir / "analysis.json", analysis)
            transition_run_state(
                run_dir,
                DEGRADED,
                reason="辅助产物已降级，不影响报告结论。",
                artifacts=("degraded_manifest.json", "analysis.json", "bd_report.html", "creator_report.html"),
            )
        return report_path
    except Exception as exc:
        _record_run_failure(run_dir, f"报告生成失败：{exc}")
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze and improve TikTok commerce short videos.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "mode",
        choices=("breakdown", "compare", "improve", "scope"),
        help="Run mode.",
    )
    parser.add_argument("--benchmark-video", type=Path, help="Benchmark video path.")
    parser.add_argument("--creator-video", type=Path, help="Creator video path.")
    parser.add_argument("--product-name", default="未填写", help="Product name.")
    parser.add_argument(
        "--proposition-key",
        default="",
        help="Explicit key in references/brand_propositions.json. Never inferred from an online run directory.",
    )
    parser.add_argument("--product-category", default="", help="Product category from the structure-library category set.")
    parser.add_argument(
        "--comparison-scope-override",
        choices=("same_task_structure",),
        help=(
            "运营确认的比较关系覆盖。same_task_structure 仅用于不同产品但共享消费者任务且具有替代关系的情况；"
            "各阶段仍由 stage_eligibility 单独判断，不自动开放固定阶段。"
        ),
    )
    parser.add_argument("--product-price", default="未填写", help="Product price.")
    parser.add_argument(
        "--product-tier",
        choices=("low", "mid", "high"),
        default=None,
        help="运营提供的客单价档（以 TikTok Shop 同品类为参照：low 走量/mid 主流/high 类目内溢价）。"
        "提供则覆盖模型对 price_tier 的世界知识判断（运营领域知识更可靠）；不提供则用模型判断兜底。",
    )
    parser.add_argument(
        "--target-market",
        type=normalize_target_market,
        default="auto",
        help="Target market: auto, sea, or a two-letter SEA market code (for example my, th, id). Only my loads Malaysia-specific rules.",
    )
    parser.add_argument("--core-selling-points", default="", help="Verified product selling points and differentiation.")
    parser.add_argument(
        "--primary-selling-point",
        default="",
        help="Operator-approved primary commercial selling point for this video route.",
    )
    parser.add_argument("--target-user", default="", help="Target audience profile and core pain point.")
    parser.add_argument(
        "--purchase-motivation",
        choices=("MO-解决问题", "MO-提升体验", "MO-情感满足", "MO-刚需补货"),
        help="Target user's primary purchase motivation.",
    )
    parser.add_argument("--creator-profile", default="", help="Optional creator account style or performance baseline.")
    parser.add_argument(
        "--product-notes",
        default="",
        help="Optional selling points, target user, or other product notes.",
    )
    parser.add_argument("--output-dir", type=Path, help="Output run directory.")
    parser.add_argument(
        "--max-total-wall-time",
        type=float,
        default=ResourceLimits().max_total_wall_time,
        help=(
            "单次运行总墙钟上限（秒），默认 1800；只影响本次 run 的资源预算，"
            "不改变单个 HTTP 请求的 timeout。慢模型验证可显式提高。"
        ),
    )
    parser.add_argument(
        "--reuse-preprocessing",
        action="store_true",
        help=(
            "复用 --output-dir 中已有的预处理（抽帧/转写/镜头轨/字幕轨），跳过重抽。"
            "用于实验迭代（同视频改 prompt/代码重跑）和 LLM 失败后补跑，大幅省时。"
        ),
    )
    parser.add_argument(
        "--asr-language",
        dest="asr_language",
        default="auto",
        help="Speech language hint passed to online Fun-ASR. Default: auto.",
    )
    parser.add_argument(
        "--asr-api-url",
        default=DEFAULT_FUN_ASR_API_URL,
        help="Online Fun-ASR endpoint. Defaults to the approved Beijing MaaS endpoint.",
    )
    parser.add_argument(
        "--asr-model",
        default=os.environ.get("FLAYR_ASR_MODEL", DEFAULT_FUN_ASR_MODEL),
        help="Online ASR model. Default: fun-asr-flash-2026-06-15.",
    )
    parser.add_argument(
        "--asr-api-key-env",
        default="DASHSCOPE_API_KEY",
        help="Environment variable for the Qwen/DashScope ASR key; only the same approved Qwen endpoint may provide the fallback.",
    )
    parser.add_argument(
        "--analysis-result-json",
        type=Path,
        help=(
            "Optional historical analysis JSON. It is accepted only with --legacy-import and is "
            "always published as audit-only degraded output."
        ),
    )
    parser.add_argument(
        "--legacy-import",
        action="store_true",
        help="Explicitly import --analysis-result-json as legacy audit data; never as a current completed run.",
    )
    stage2_reuse = parser.add_mutually_exclusive_group()
    stage2_reuse.add_argument(
        "--stage2-replay-from",
        type=Path,
        help=(
            "Strictly replay matching completed Stage2 provider artifacts from another run. "
            "Never calls the Stage2 provider when an artifact is missing or its request identity changed."
        ),
    )
    stage1_reuse = parser.add_mutually_exclusive_group()
    stage1_reuse.add_argument(
        "--stage1-replay-from",
        type=Path,
        help=(
            "严格重放匹配的 Stage1-A/B/C provider artifact；缺失、损坏或请求身份变化时直接失败，"
            "绝不调用 Stage1 provider。"
        ),
    )
    stage1_reuse.add_argument(
        "--stage1-resume-from",
        type=Path,
        help=(
            "优先复用匹配的 Stage1-A/B/C provider artifact；仅对缺失、失败或语义变化的阶段调用 provider。"
        ),
    )
    stage2_reuse.add_argument(
        "--stage2-resume-from",
        type=Path,
        help=(
            "Reuse matching completed Stage2 provider artifacts from another run and call the provider "
            "only for missing, failed, or semantically changed groups."
        ),
    )
    parser.add_argument(
        "--llm-model",
        help="Optional approved-provider chat model used to generate analysis_result.json.",
    )
    parser.add_argument(
        "--llm-api-url",
        default="https://api.openai.com/v1/chat/completions",
        help="Approved-provider Chat Completions endpoint; only allowlisted official domains are accepted.",
    )
    parser.add_argument(
        "--llm-api-key-env",
        default="OPENAI_API_KEY",
        help="Environment variable that contains the LLM API key.",
    )
    parser.add_argument(
        "--llm-api-key-keychain-service",
        help="macOS Keychain generic-password service used to read the LLM API key.",
    )
    parser.add_argument(
        "--llm-api-key-keychain-account",
        default="API_KEY",
        help="macOS Keychain account used with --llm-api-key-keychain-service. Default: API_KEY.",
    )
    parser.add_argument(
        "--llm-dry-run",
        action="store_true",
        help="Write the LLM request payload without calling the API.",
    )
    parser.add_argument(
        "--allow-degraded",
        action="store_true",
        help=(
            "Allow compare/improve to proceed without completed LLM or online ASR analysis. "
            "Without this flag, missing analysis exits non-zero. "
            "When set, the run is marked degraded and no success manifest is written."
        ),
    )
    parser.add_argument(
        "--provider-replay-from",
        type=Path,
        help=(
            "Strictly replay provider artifacts (including ASR, Step-0, Phase C, S4 verifier and postprocess). "
            "A missing or mismatched artifact never falls back to a live provider call."
        ),
    )
    parser.add_argument(
        "--verification-stage",
        choices=("production", "fixture", "offline_replay", "fake_provider", "ordinary_sample", "boundary_sample"),
        required=True,
        help=(
            "Execution intent. Use production for normal product runs; evaluation stages require "
            "the frozen prerequisite markers."
        ),
    )
    parser.add_argument(
        "--verification-root",
        type=Path,
        help="Directory containing passed verification-order markers.",
    )
    parser.add_argument(
        "--llm-include-images",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Use the full Step-0 + per-video fact extraction + multimodal comparison pipeline. "
            "Enabled by default; --no-llm-include-images is retained only as a rejected legacy flag."
        ),
    )
    parser.add_argument(
        "--absolute-execution-shadow",
        action="store_true",
        help=(
            "额外对两侧视频分别运行 S1-S4 单侧绝对执行审计；仅写 shadow 结果，"
            "不改变 severity。用于校准和检测跨配对锚定漂移。"
        ),
    )
    parser.add_argument(
        "--llm-image-limit",
        type=int,
        default=12,
        help="Maximum visual inputs attached to each per-video fact request. Default: 12.",
    )
    parser.add_argument(
        "--translate-with-llm",
        action="store_true",
        help="Translate local-language transcripts to Chinese with the configured LLM provider.",
    )
    parser.add_argument(
        "--translation-model",
        help="Optional model for transcript translation. Defaults to --llm-model.",
    )
    parser.add_argument(
        "--ocr-mode",
        choices=("auto", "on", "off"),
        default="auto",
        help=(
            "Subtitle OCR mode. auto reuses the configured multimodal LLM when an API key "
            "is available and this is not --llm-dry-run; on forces OCR; off disables OCR."
        ),
    )
    parser.add_argument(
        "--with-ocr",
        action="store_true",
        help="Backward-compatible alias for --ocr-mode on.",
    )
    parser.add_argument(
        "--no-ocr",
        action="store_true",
        help="Backward-compatible alias for --ocr-mode off.",
    )
    return parser


def create_run_dir(args: argparse.Namespace) -> Path:
    if args.output_dir:
        run_dir = args.output_dir.expanduser().resolve()
        _prepare_explicit_run_dir(run_dir, reuse=bool(args.reuse_preprocessing))
    else:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir = DEFAULT_RUNS_DIR / f"{stamp}-{args.mode}-{uuid.uuid4().hex[:8]}"
        run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _prepare_explicit_run_dir(run_dir: Path, *, reuse: bool) -> None:
    """Reject mixed output directories and remove only known stale run files."""
    if run_dir.exists() and not run_dir.is_dir():
        raise SystemExit(f"--output-dir 不是目录：{run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    for entry in run_dir.iterdir():
        if entry.is_dir() and not entry.is_symlink() and (
            entry.name.startswith(".benchmark.generation-")
            or entry.name.startswith(".creator.generation-")
            or entry.name.startswith(".benchmark.previous-")
            or entry.name.startswith(".creator.previous-")
        ):
            shutil.rmtree(entry, ignore_errors=True)
    entries = [entry for entry in run_dir.iterdir() if entry.name != RUN_STATE_FILE]
    if not entries:
        if reuse:
            reset_run_state(run_dir)
        return
    if not reuse:
        raise SystemExit(
            f"--output-dir 已存在且非空：{run_dir}。为避免混入旧产物，请使用新目录，"
            "或显式添加 --reuse-preprocessing。"
        )
    for entry in entries:
        if entry.is_dir() and not entry.is_symlink() and entry.name in _RUN_ROLE_DIRS:
            continue
        if entry.is_file() and entry.name == "product_foundation.json" and reuse:
            # Product foundation is a keyed, schema-checked cache. Keep it
            # across an explicit preprocessing reuse so a Stage2 retry does
            # not invoke the foundation model again before cache validation.
            continue
        if entry.is_file() and (
            entry.name in _RUN_OUTPUT_FILES
            or entry.name.startswith(_RUN_OUTPUT_PREFIXES)
        ):
            entry.unlink()
            continue
        raise SystemExit(
            f"--output-dir 含有未识别的旧内容：{entry}。请使用新的运行目录，"
            "不要把非 Flayr 产物与预处理缓存混用。"
        )
    reset_run_state(run_dir)


def check_dependencies(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "ffmpeg": shutil.which("ffmpeg"),
        "ffprobe": shutil.which("ffprobe"),
        "asr": {
            "provider": "dashscope",
            "api_url": args.asr_api_url,
            "model": args.asr_model,
            "language": args.asr_language,
        },
    }


def validate_inputs(args: argparse.Namespace) -> dict[str, Path]:
    inputs: dict[str, Path] = {}

    if args.mode in {"breakdown", "compare", "improve", "scope"}:
        if not args.benchmark_video:
            raise SystemExit("--benchmark-video is required.")
        inputs["benchmark"] = validate_video_path(args.benchmark_video)

    if args.mode in {"compare", "improve", "scope"}:
        if not args.creator_video:
            raise SystemExit("--creator-video is required for compare/improve mode.")
        inputs["creator"] = validate_video_path(args.creator_video)

    if args.analysis_result_json:
        args.analysis_result_json = validate_optional_file(args.analysis_result_json, "--analysis-result-json")
    if bool(args.analysis_result_json) != bool(args.legacy_import):
        raise SystemExit("--analysis-result-json 必须与显式 --legacy-import 一起使用，不能把旧结果伪装成当前完成结果。")

    if args.provider_replay_from:
        args.provider_replay_from = args.provider_replay_from.expanduser().resolve()
        if not args.provider_replay_from.is_dir():
            raise SystemExit(f"--provider-replay-from must be an existing run directory: {args.provider_replay_from}")
        if args.output_dir and args.output_dir.expanduser().resolve() == args.provider_replay_from:
            raise SystemExit(
                "--output-dir must differ from --provider-replay-from; "
                "in-place replay could overwrite replay artifacts"
            )
    if args.verification_stage != "production":
        if not args.verification_root:
            raise SystemExit("evaluation --verification-stage requires --verification-root")
        args.verification_root = args.verification_root.expanduser().resolve()

    for option in (
        "stage1_replay_from",
        "stage1_resume_from",
        "stage2_replay_from",
        "stage2_resume_from",
    ):
        value = getattr(args, option, None)
        if not value:
            continue
        resolved = value.expanduser().resolve()
        if not resolved.is_dir():
            raise SystemExit(f"--{option.replace('_', '-')} must be an existing run directory: {resolved}")
        setattr(args, option, resolved)

    if args.comparison_scope_override and args.mode not in {"compare", "improve", "scope"}:
        raise SystemExit("--comparison-scope-override 仅可用于 compare、improve 或 scope 模式。")

    return inputs


def validate_video_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise SystemExit(f"Video does not exist: {resolved}")
    if not resolved.is_file():
        raise SystemExit(f"Video path is not a file: {resolved}")
    return resolved


def analysis_scope(args: argparse.Namespace) -> dict[str, Any]:
    context = {
        "品类": args.product_category,
        "价格/价格带": "" if args.product_price == "未填写" else args.product_price,
        "核心卖点/差异化": args.core_selling_points,
        "目标用户/核心痛点": args.target_user,
        "购买动机": args.purchase_motivation,
    }
    missing = [label for label, value in context.items() if not str(value or "").strip()]
    if not missing:
        return {
            "level": "strategy",
            "label": "策略增强分析",
            "missing_context": [],
            "boundary": "可结合已确认的产品与人群策略，对成交阻力和 GMV 优先级作完整判断。",
        }
    return {
        "level": "evidence",
        "label": "视频证据分析",
        "missing_context": missing,
        "boundary": (
            "结论仅基于视频中可听、可读、可见事实及与标杆的表达差异；"
            "卖点真实性、目标人群适配、价格策略与最终 GMV 优先级需待业务信息确认。"
        ),
    }


def validate_optional_file(path: Path | None, label: str) -> Path | None:
    if not path:
        return None
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise SystemExit(f"{label} does not exist: {resolved}")
    if not resolved.is_file():
        raise SystemExit(f"{label} is not a file: {resolved}")
    return resolved


def _file_probe_from_stat(stat: os.stat_result) -> dict[str, int]:
    return {
        "size_bytes": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "ctime_ns": int(stat.st_ctime_ns),
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
    }


def _file_metadata(
    path: Any,
    include_sha256: bool = False,
    *,
    include_path: bool = True,
    include_mtime: bool = True,
    cached_probe: dict[str, Any] | None = None,
    cached_sha256: str = "",
) -> dict[str, Any] | None:
    """Return cache identity metadata, optionally reusing a verified fast probe."""
    if not path:
        return None
    candidate = Path(str(path)).expanduser().resolve()
    if not candidate.is_file():
        metadata: dict[str, Any] = {"missing": True}
        if include_path:
            metadata["path"] = str(candidate)
        return metadata
    stat = candidate.stat()
    metadata = {"size_bytes": stat.st_size}
    if include_path:
        metadata["path"] = str(candidate)
    if include_mtime:
        metadata["mtime_ns"] = stat.st_mtime_ns
    if include_sha256:
        current_probe = _file_probe_from_stat(stat)
        if cached_probe == current_probe and re.fullmatch(r"[0-9a-f]{64}", str(cached_sha256 or "")):
            metadata["sha256"] = str(cached_sha256)
        else:
            digest = hashlib.sha256()
            with candidate.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            metadata["sha256"] = digest.hexdigest()
    return metadata


def _git_commit_sha() -> str:
    """返回当前 git commit short hash，不可用时回退 'unknown'。"""
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _binary_version(bin_deps: dict[str, Any], key: str) -> str:
    """返回工具路径 + 版本第一行，不可用返回 'missing'。"""
    path = bin_deps.get(key) if isinstance(bin_deps, dict) else None
    if not path:
        return "missing"
    try:
        result = subprocess.run([str(path), "-version"], capture_output=True, text=True, timeout=5)
        line = result.stdout.split('\n')[0] if result.stdout else "unknown"
        return f"{path}:{line.strip()}"
    except Exception:
        return f"{path}:run_failed"


def build_preprocess_fingerprint(
    video_path: Path,
    deps: dict[str, Any],
    args: argparse.Namespace,
    *,
    cached_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """缓存只在源视频与所有会改变预处理产物的配置完全一致时命中。"""
    cached_fingerprint = cached_info.get("preprocess_fingerprint") if isinstance(cached_info, dict) else None
    cached_source = cached_fingerprint.get("source_video") if isinstance(cached_fingerprint, dict) else None
    cached_probe = cached_info.get("preprocess_source_probe") if isinstance(cached_info, dict) else None
    cached_sha256 = cached_source.get("sha256") if isinstance(cached_source, dict) else ""
    return {
        "cache_schema_version": PREPROCESS_CACHE_SCHEMA_VERSION,
        "pipeline_version": PREPROCESS_PIPELINE_VERSION,
        "code_commit": _git_commit_sha(),
        # Content identity is deliberately independent of the input path and mtime.
        "source_video": _file_metadata(
            video_path,
            include_sha256=True,
            include_path=False,
            include_mtime=False,
            cached_probe=cached_probe if isinstance(cached_probe, dict) else None,
            cached_sha256=str(cached_sha256 or ""),
        ),
        "media_tools": {
            "ffmpeg": _binary_version(deps, "ffmpeg"),
            "ffprobe": _binary_version(deps, "ffprobe"),
        },
        "transcription": {
            "backend": "fun-asr",
            "api_url": str(getattr(args, "asr_api_url", "") or ""),
            "model": str(getattr(args, "asr_model", DEFAULT_FUN_ASR_MODEL) or DEFAULT_FUN_ASR_MODEL),
            "requested_language": str(getattr(args, "asr_language", "auto") or "auto"),
        },
        "translation": {
            "enabled": bool(getattr(args, "translate_with_llm", False)),
            "model": str(getattr(args, "translation_model", "") or getattr(args, "llm_model", "") or ""),
            "api_url": str(getattr(args, "llm_api_url", "") or ""),
            "product_name": str(getattr(args, "product_name", "") or ""),
            "product_notes": str(getattr(args, "product_notes", "") or ""),
        },
        "ocr": {
            "mode": str(getattr(args, "ocr_mode", "auto") or "auto"),
            "with_ocr": bool(getattr(args, "with_ocr", False)),
            "no_ocr": bool(getattr(args, "no_ocr", False)),
            "dry_run": bool(getattr(args, "llm_dry_run", False)),
        },
        "frame_strategy": "base-adaptive-2fps-focus-2fps-canonical-analysis-manifest-v4-anchor-frames",
    }


def load_existing_video_result(
    role_dir: Path,
    expected_fingerprint: dict[str, Any],
    cached_info: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """复用上次预处理；缺少、不匹配、未完成的缓存一律重抽。"""
    cache = role_dir / "_preprocess.json"
    if not cache.is_file():
        return None
    info = cached_info
    if info is None:
        try:
            info = json.loads(cache.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
    if not isinstance(info, dict):
        return None
    if info.get("preprocess_fingerprint") != expected_fingerprint:
        return None
    if info.get("preprocess_completed") is not True:
        return None
    if not _preprocess_artifacts_match(role_dir, info.get("preprocess_artifacts")):
        return None
    role_root = {"work_dir": str(role_dir)}
    frames_dir = resolve_artifact_path(role_root, info.get("frames_dir"), require_root=True)
    transcript = resolve_artifact_path(
        role_root,
        info.get("transcript_path"),
        require_file=True,
        require_root=True,
    )
    if frames_dir is None or transcript is None:
        return None
    if not frames_dir.is_dir():
        return None
    if str(info.get("transcription_status") or "").strip().lower() != "completed":
        return None
    if not transcript.is_file() or _is_stale_placeholder(transcript):
        return None
    segment_value = str(info.get("transcript_segments_path") or "").strip()
    if segment_value:
        segment_path = _current_role_artifact(info, "transcript_segments_path", role_dir)
        if segment_path is None or _is_stale_placeholder(segment_path):
            return None
    elif info.get("transcript_segments_available") is True:
        return None
    words_value = str(info.get("transcript_words_path") or "").strip()
    if words_value and _current_role_artifact(info, "transcript_words_path", role_dir) is None:
        return None
    audio_value = str(info.get("audio_path") or "").strip()
    if audio_value and resolve_artifact_path(
        role_root,
        audio_value,
        require_file=True,
        require_root=True,
    ) is None:
        return None
    return info


def _current_role_artifact(info: dict[str, Any], key: str, role_dir: Path) -> Path | None:
    """Resolve an explicitly recorded artifact without directory fallbacks."""
    raw = str(info.get(key) or "").strip()
    if not raw:
        return None
    return resolve_artifact_path(
        {"work_dir": str(role_dir)},
        raw,
        require_file=True,
        require_root=True,
    )


def _preprocess_artifact_metadata(path: Path, *, include_sha256: bool = True) -> dict[str, Any]:
    metadata: dict[str, Any] = _file_probe_from_stat(path.stat())
    if include_sha256:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        metadata["sha256"] = digest.hexdigest()
    return metadata


def _build_preprocess_artifact_manifest(
    role_dir: Path,
    *,
    include_sha256: bool = True,
) -> dict[str, Any]:
    """Hash every generated role artifact except the manifest that contains it."""
    root = role_dir.expanduser().resolve()
    files: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "_preprocess.json" or path.is_symlink():
            continue
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(root).as_posix()
        except ValueError:
            continue
        files[relative] = _preprocess_artifact_metadata(resolved, include_sha256=include_sha256)
    return {"schema_version": PREPROCESS_ARTIFACT_SCHEMA_VERSION, "files": files}


def _preprocess_artifacts_match(role_dir: Path, value: Any) -> bool:
    if not isinstance(value, dict) or value.get("schema_version") != PREPROCESS_ARTIFACT_SCHEMA_VERSION:
        return False
    recorded = value.get("files")
    if not isinstance(recorded, dict) or not recorded:
        return False
    root = role_dir.expanduser().resolve()
    if all(
        isinstance(metadata, dict)
        and all(key in metadata for key in ("size_bytes", "mtime_ns", "ctime_ns", "device", "inode"))
        for metadata in recorded.values()
    ):
        current_probe = _build_preprocess_artifact_manifest(root, include_sha256=False).get("files")
        recorded_probe = {
            relative: {
                key: metadata[key]
                for key in ("size_bytes", "mtime_ns", "ctime_ns", "device", "inode")
            }
            for relative, metadata in recorded.items()
        }
        if current_probe == recorded_probe:
            return True
    current = _build_preprocess_artifact_manifest(root).get("files")
    if current != recorded:
        return False
    for relative, metadata in recorded.items():
        candidate = (root / str(relative)).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return False
        if not isinstance(metadata, dict) or not candidate.is_file() or candidate.is_symlink():
            return False
    return True


def _is_stale_placeholder(path: Path) -> bool:
    """Return whether a cached text artifact is empty or still a pending placeholder."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return True
    if not text:
        return True
    lowered = text.lower()
    return lowered.startswith(
        (
            "待转写",
            "待翻译",
            "待生成",
            "pending:",
            ASR_FAILURE_PLACEHOLDER.lower(),
            ASR_AUDIO_PLACEHOLDER.lower(),
        )
    )


def _generation_path_variants(root: Path) -> set[str]:
    variants = {str(root.absolute())}
    try:
        variants.add(str(root.resolve()))
    except (OSError, RuntimeError, ValueError):
        pass
    return variants


def _rewrite_generation_paths(value: Any, old_root: Path, new_root: Path) -> Any:
    old_variants = sorted(_generation_path_variants(old_root), key=len, reverse=True)
    if isinstance(value, dict):
        return {key: _rewrite_generation_paths(item, old_root, new_root) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite_generation_paths(item, old_root, new_root) for item in value]
    if isinstance(value, str):
        for old in old_variants:
            if not os.path.isabs(value):
                continue
            try:
                relative = os.path.relpath(value, old)
                if relative == os.pardir or relative.startswith(os.pardir + os.sep):
                    continue
                common = os.path.commonpath((os.path.abspath(value), os.path.abspath(old)))
                if os.path.normcase(common) != os.path.normcase(os.path.abspath(old)):
                    continue
            except ValueError:
                continue
            return str((new_root / relative).resolve())
    return value


def _rewrite_generation_json_artifacts(
    root: Path,
    old_root: Path,
    new_root: Path,
) -> None:
    """Rewrite staging paths embedded in generated JSON artifacts.

    The role result contains paths as well, but several secondary manifests
    are written independently during preprocessing. They must be rewritten
    before the staging directory is published, otherwise the published
    artifacts retain references to a directory that is about to disappear.
    """
    old_variants = _generation_path_variants(old_root)
    serialized_old_variants = set(old_variants)
    serialized_old_variants.update(item.replace("\\", "/") for item in old_variants)
    serialized_old_variants.update(item.replace("\\", "\\\\") for item in old_variants)
    for path in sorted(root.rglob("*.json")):
        if path.name == "_preprocess.json":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise OSError(f"cannot read generated JSON artifact: {path}") from exc
        if not any(old in text for old in serialized_old_variants):
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"generated JSON artifact contains a staging path but is invalid: {path}") from exc
        rewritten = _rewrite_generation_paths(value, old_root, new_root)
        write_json(path, rewritten)
        if any(old in path.read_text(encoding="utf-8") for old in serialized_old_variants):
            raise ValueError(f"staging path remains in generated JSON artifact: {path}")


def _promote_preprocess_generation(
    staging_dir: Path,
    role_dir: Path,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Publish a complete generation while keeping old artifacts out of the build."""
    published = _rewrite_generation_paths(result, staging_dir, role_dir)
    if not isinstance(published, dict):
        raise TypeError("preprocess generation result must be a mapping")
    _rewrite_generation_json_artifacts(staging_dir, staging_dir, role_dir)

    backup_dir = role_dir.parent / f".{role_dir.name}.previous-{uuid.uuid4().hex}"
    if role_dir.exists():
        role_dir.replace(backup_dir)
    try:
        staging_dir.replace(role_dir)
    except Exception:
        if not role_dir.exists() and backup_dir.exists():
            backup_dir.replace(role_dir)
        raise
    if backup_dir.exists():
        shutil.rmtree(backup_dir, ignore_errors=True)
    # Rewriting JSON artifacts changes their bytes, so the manifest must be
    # rebuilt after publication instead of carrying staging-time hashes.
    published["preprocess_artifacts"] = _build_preprocess_artifact_manifest(role_dir)
    write_json(role_dir / "_preprocess.json", published)
    return published


def process_video(
    role: str,
    video_path: Path,
    run_dir: Path,
    deps: dict[str, Any],
    args: argparse.Namespace,
    budget: ResourceBudget | None = None,
) -> dict[str, Any]:
    role_dir = run_dir / role
    budget = budget or getattr(args, "_resource_budget", None)
    if getattr(args, "reuse_preprocessing", False):
        cached_info = None
        cache_path = role_dir / "_preprocess.json"
        if cache_path.is_file():
            try:
                loaded = json.loads(cache_path.read_text(encoding="utf-8"))
                cached_info = loaded if isinstance(loaded, dict) else None
            except (OSError, json.JSONDecodeError):
                cached_info = None
        fingerprint = build_preprocess_fingerprint(video_path, deps, args, cached_info=cached_info)
        cached = load_existing_video_result(role_dir, fingerprint, cached_info=cached_info)
        if cached is not None:
            if cached.get("path") != str(video_path):
                cached["path"] = str(video_path)
                write_json(role_dir / "_preprocess.json", cached)
            try:
                cached_frames = int(
                    finite_nonnegative(cached.get("frame_count") or 0, "cached frame count")
                    + finite_nonnegative(cached.get("focus_frame_count") or 0, "cached focus frame count")
                )
            except (TypeError, ValueError) as exc:
                raise ResourceBudgetExceeded(f"cached frame counts are invalid: {exc}") from exc
            if budget is not None:
                budget.reserve_frames(max(0, cached_frames))
                reserve_existing_media_artifacts(role_dir, budget)
            ensure_video_evidence_artifacts(role_dir, cached)
            print(f"[reuse] {role}: 复用已有预处理（跳过抽帧/转写/OCR）")
            return cached

    staging_dir = run_dir / f".{role}.generation-{uuid.uuid4().hex}"
    staging_dir.mkdir(parents=True, exist_ok=False)
    try:
        result = _process_video_generation(role, video_path, staging_dir, deps, args, budget=budget)
        return _promote_preprocess_generation(staging_dir, role_dir, result)
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise


def _process_video_generation(
    role: str,
    video_path: Path,
    role_dir: Path,
    deps: dict[str, Any],
    args: argparse.Namespace,
    budget: ResourceBudget | None = None,
) -> dict[str, Any]:
    """Build one isolated role generation; callers publish it only after completion."""
    legacy_import = bool(getattr(args, "legacy_import", False))
    frames_dir = role_dir / "frames"
    focus_frames_dir = role_dir / "focus_frames"
    role_dir.mkdir(parents=True, exist_ok=True)
    frames_dir.mkdir(parents=True, exist_ok=True)
    focus_frames_dir.mkdir(parents=True, exist_ok=True)

    result: dict[str, Any] = {
        "path": str(video_path),
        "work_dir": str(role_dir),
        "frames_dir": str(frames_dir),
        "focus_frames_dir": str(focus_frames_dir),
        "frame_count": 0,
        "frame_manifest_path": None,
        "frames": [],
        "focus_frame_count": 0,
        "focus_frame_manifest_path": None,
        "focus_frames": [],
        "analysis_anchor_frame_count": 0,
        "stage_frame_manifest_path": None,
        "stage_frames": [],
        "transcript_words_path": None,
        "transcript_words_available": False,
        "video_evidence": {},
        "duration_seconds": None,
        "frame_strategy": {
            "base": "adaptive sampling up to 2 fps under the shared frame budget",
            "focus": "2 fps for first 5 seconds and final 5 seconds",
            "structural_anchors": "accurate frames at stage, scene-cut, and subtitle boundaries",
            "stage": "representative frames for S1-S6 from canonical analysis manifest",
            "selection": "global + local + action signals with scene/subtitle/focus anchors",
        },
        "audio_path": None,
        "audio_quality": {},
        "transcript_path": None,
        "transcript_segments_path": None,
        "transcript_segments_available": False,
        "transcription_status": "not_started",
        "requested_language": args.asr_language,
        "detected_language": None,
        "detected_language_confidence": None,
        "transcription_language": None,
        "translation_language": "zh",
        "translation_path": None,
        "translation_status": "not_started",
        "speech_mode": {},
        "errors": [],
    }

    if deps["ffmpeg"]:
        if deps["ffprobe"]:
            result["duration_seconds"] = deps.get("source_durations", {}).get(role) or probe_duration_seconds(video_path)
        extract_frames(video_path, frames_dir, focus_frames_dir, result)
        extract_audio(video_path, role_dir / "audio.wav", result)
        result["audio_quality"] = analyze_audio_quality(
            Path(result["audio_path"]) if result.get("audio_path") else None,
            result.get("duration_seconds"),
        )
    else:
        result["errors"].append("ffmpeg missing: skipped frame and audio extraction")

    transcript_path = role_dir / "transcript.txt"
    if legacy_import:
        # Historical JSON import is an audit-only path. It may still build
        # deterministic local media artifacts for the report, but it must not
        # start a new ASR/OCR/translation request.
        write_text(transcript_path, ASR_AUDIO_PLACEHOLDER + "\n")
        result["transcription_status"] = "not_requested_legacy_import"
        result["errors"].append("online ASR skipped for legacy import")
    elif result["audio_path"]:
        run_online_asr(
            args.asr_api_url,
            args.asr_model,
            "" if getattr(args, "provider_replay_from", None) else read_asr_api_key(args),
            args.asr_language,
            Path(result["audio_path"]),
            role_dir,
            transcript_path,
            result,
            budget=budget,
            provider_replay_from=(
                Path(args.provider_replay_from) if getattr(args, "provider_replay_from", None) else None
            ),
        )
    else:
        write_text(transcript_path, ASR_AUDIO_PLACEHOLDER + "\n")
        result["transcription_status"] = "placeholder"
        result["errors"].append("online ASR skipped because audio extraction failed")

    result["transcript_path"] = str(transcript_path)
    if legacy_import:
        result["translation_status"] = "not_requested_legacy_import"
    else:
        sync_chinese_translation(role_dir, result)
    if args.translate_with_llm and not legacy_import:
        translate_transcript_with_llm(args, role, role_dir, result)

    # 晃动信号：本地 ffmpeg vmafmotion 确定性指标（零成本）。severe 时 derive 对
    # 视觉依赖阶段执行分封顶 0.5——晃动=无法有效接收（2026-06-12 用户判例）。
    result["shake"] = compute_shake_metric(video_path)

    # 镜头轨：本地 ffmpeg 自适应切分，默认跑（无成本）。供 omni 拿精确镜头边界。
    shot_track = build_shot_track(role_dir, video_path, result.get("duration_seconds"))
    result["shot_track_status"] = shot_track.get("status")
    result["shot_track_path"] = str(role_dir / "shot_track.json") if shot_track.get("shots") else None

    # 字幕轨：多模态 OCR。默认 auto：有兼容视觉模型 key 且非 dry-run 时自动开启；
    # 没 key/调试时降级为 disabled，不影响主流程。
    result["subtitle_track_status"] = "disabled_by_policy"
    result["subtitle_track_path"] = None
    if legacy_import:
        result["subtitle_track_status"] = "not_requested_legacy_import"
        should_ocr, ocr_key, ocr_disabled_reason = False, "", "not_requested_legacy_import"
    else:
        should_ocr, ocr_key, ocr_disabled_reason = resolve_ocr_policy(args)
    if should_ocr:
        subtitle_track = build_subtitle_track(
            role_dir,
            result,
            ocr_key,
            api_url=args.llm_api_url,
            model=args.llm_model,
            budget=budget,
            provider_replay_from=getattr(args, "provider_replay_from", None),
        )
        result["subtitle_track_status"] = subtitle_track.get("status")
        if subtitle_track.get("segments"):
            result["subtitle_track_path"] = str(role_dir / "subtitle_track.json")
    elif not legacy_import:
        result["subtitle_track_status"] = ocr_disabled_reason

    # The adaptive base corpus is bounded, not the final evidence corpus. Add
    # accurate frames at structural boundaries discovered after extraction so
    # short cuts and subtitle transitions are not forced onto the nearest
    # sampled second.
    extract_anchor_frames(video_path, frames_dir, result, budget=budget)
    result["speech_mode"] = classify_speech_mode(role_dir, result)

    # 证据索引与视图：canonical analysis frames、去重审计、联系表、转写包、timeline view。
    # 这些产物改变模型可见输入，但不直接改写业务评分。
    result["video_evidence"] = build_video_evidence_artifacts(role_dir, result)
    result["preprocess_fingerprint"] = build_preprocess_fingerprint(video_path, deps, args)
    result["preprocess_source_probe"] = _file_probe_from_stat(video_path.stat()) if video_path.is_file() else None

    # 落盘预处理结果，供 --reuse-preprocessing 下次复用（即使本次 LLM 阶段后续失败也已写）。
    result["preprocess_completed"] = True
    result["preprocess_artifacts"] = _build_preprocess_artifact_manifest(role_dir)
    write_json(role_dir / "_preprocess.json", result)
    return result


def ensure_video_evidence_artifacts(role_dir: Path, info: dict[str, Any]) -> None:
    """Ensure reused preprocessing also has secondary evidence artifacts."""
    role_root = {"work_dir": str(role_dir)}
    if not isinstance(info.get("audio_quality"), dict) or not info.get("audio_quality"):
        audio_path = resolve_artifact_path(
            role_root,
            info.get("audio_path"),
            require_file=True,
            require_root=True,
        )
        info["audio_quality"] = analyze_audio_quality(
            audio_path,
            info.get("duration_seconds"),
        )
    if not isinstance(info.get("speech_mode"), dict) or not info.get("speech_mode", {}).get("mode"):
        info["speech_mode"] = classify_speech_mode(role_dir, info)
    existing = info.get("video_evidence") if isinstance(info.get("video_evidence"), dict) else {}
    timeline_dir = resolve_artifact_path(
        role_root,
        existing.get("timeline_views_dir") or role_dir / "timeline_views",
        require_root=True,
    ) or role_dir / "timeline_views"
    selection_report = resolve_artifact_path(
        role_root,
        existing.get("frame_selection_report_path") or role_dir / "frames" / "selection_report.json",
        require_file=True,
        require_root=True,
    )
    audit_path = resolve_artifact_path(
        role_root,
        existing.get("audit_path") or role_dir / "video_evidence_audit.json",
        require_file=True,
        require_root=True,
    )
    segment_path = _current_role_artifact(info, "transcript_segments_path", role_dir)
    transcript_pack = resolve_artifact_path(
        role_root,
        existing.get("transcript_pack_path"),
        require_file=True,
        require_root=True,
    )
    transcript_window_contract_ready = (
        existing.get("transcript_window_contract_version") == TRANSCRIPT_WINDOW_CONTRACT_VERSION
    )
    transcript_ready = not str(info.get("transcript_segments_path") or "").strip() or (
        segment_path is not None and transcript_pack is not None
    )
    analysis_manifest = resolve_artifact_path(
        role_root,
        existing.get("analysis_frame_manifest_path") or role_dir / "frames" / "analysis_manifest.json",
        require_file=True,
        require_root=True,
    )
    analysis_stage_manifest = resolve_artifact_path(
        role_root,
        existing.get("analysis_stage_frame_manifest_path") or role_dir / "frames" / "analysis_stage_frames.json",
        require_file=True,
        require_root=True,
    )
    if (
        existing
        and timeline_dir.is_dir()
        and selection_report is not None
        and analysis_manifest is not None
        and analysis_stage_manifest is not None
        and audit_path is not None
        and transcript_ready
        and transcript_window_contract_ready
    ):
        return
    info["video_evidence"] = build_video_evidence_artifacts(role_dir, info)
    info["preprocess_artifacts"] = _build_preprocess_artifact_manifest(role_dir)
    write_json(role_dir / "_preprocess.json", info)


def resolve_ocr_policy(args: argparse.Namespace) -> tuple[bool, str, str]:
    if getattr(args, "no_ocr", False):
        return False, "", "disabled_by_policy"
    if getattr(args, "with_ocr", False):
        mode = "on"
    else:
        mode = getattr(args, "ocr_mode", "auto")
    if mode == "off" or getattr(args, "llm_dry_run", False):
        return False, "", "disabled_by_policy"
    if getattr(args, "provider_replay_from", None):
        # A strict technical replay must not require a live provider secret.
        # The artifact identity still binds the replay to the exact model and
        # endpoint, so this does not weaken provider selection.
        return True, "", ""
    api_key = read_llm_api_key(args).strip()
    if not api_key:
        return False, "", "disabled_no_ocr_key"
    if not looks_like_vision_config(args):
        return False, "", "disabled_non_vision_config"
    if mode == "on":
        return True, api_key, ""
    return True, api_key, ""


def looks_like_vision_config(args: argparse.Namespace) -> bool:
    values = [
        str(getattr(args, "llm_api_url", "") or "").lower(),
        str(getattr(args, "llm_api_key_keychain_service", "") or "").lower(),
        str(getattr(args, "llm_model", "") or "").lower(),
    ]
    return any(
        marker in value
        for value in values
        for marker in ("dashscope", "qwen")
    )


# Compatibility alias for external scripts that imported the old helper.
looks_like_dashscope_config = looks_like_vision_config


def build_analysis(
    args: argparse.Namespace,
    run_dir: Path,
    deps: dict[str, Any],
    videos: dict[str, dict[str, Any]],
    budget: ResourceBudget | None = None,
) -> dict[str, Any]:
    stage_analysis = placeholder_stages()
    improvements = default_improvements(args.mode)

    # improvements_status 取值：
    #   not_applicable  —— breakdown 模式不需要提升点
    #   llm_unavailable —— compare/improve 模式但 LLM 未跑或失败（初始默认值）
    #   llm_completed   —— LLM 分析已成功合并（由 merge_analysis_result 改写）
    improvements_status = "not_applicable" if args.mode == "breakdown" else "llm_unavailable"

    capabilities = provider_capabilities(args.llm_api_url, args.llm_model)
    native_audio = can_analyze_native_audio(args.llm_api_url, args.llm_model)
    return {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "mode": args.mode,
        "run_dir": str(run_dir),
        "analysis_scope": analysis_scope(args),
        "product": {
            "name": args.product_name,
            "proposition_key": str(args.proposition_key or "").strip(),
            "category": args.product_category,
            "price": args.product_price,
            "tier": args.product_tier,  # 运营客单价档；None 时 derive 退回模型判断
            "target_market": args.target_market,
            "core_selling_points": args.core_selling_points,
            "primary_selling_point": args.primary_selling_point,
            "target_user": args.target_user,
            "purchase_motivation": args.purchase_motivation or "",
            "creator_profile": args.creator_profile,
            "notes": args.product_notes,
        },
        "dependencies": deps,
        "analysis_result_contract": ANALYSIS_RESULT_CONTRACT.metadata(),
        "resource_budget": budget.snapshot() if budget is not None else {},
        "audio_assessment": {
            "native_audio_analysis": native_audio,
            "provider_profile": capabilities.profile,
            "capability_confidence": capabilities.confidence,
            "mode": "native_audio_observation" if native_audio else "transcript_plus_local_qc",
            "commercial_contribution": "observation_only",
            "severity_policy": "excluded",
        },
        "videos": videos,
        "stage_analysis": stage_analysis,
        "improvements": improvements if args.mode in {"compare", "improve"} else [],
        "improvements_status": improvements_status,
        "analysis_run_state": "not_run",
    }


def default_improvements(mode: str) -> list[dict[str, Any]]:
    # LLM 未运行或失败时的占位返回。
    # 真正的"未跑 LLM"提示由 build_analysis 写入 improvements_status，
    # 并在 report.render_improvement_cards 中根据 status 渲染警告区块。
    # 保留此函数仅为兼容现有调用点，后续如需兜底数据可在此恢复。
    return []


def print_summary(
    run_dir: Path,
    report_path: Path,
    deps: dict[str, Any],
    videos: dict[str, dict[str, Any]],
) -> None:
    print(f"Run directory: {run_dir}")
    print(f"Report: {report_path}")
    print(f"ffmpeg: {'ok' if deps['ffmpeg'] else 'missing'}")
    asr = deps.get("asr") if isinstance(deps.get("asr"), dict) else {}
    print(f"asr: {asr.get('model') or 'missing'} @ {asr.get('api_url') or 'unconfigured'}")
    for role, info in videos.items():
        print(
            f"{role}: frames={info['frame_count']} "
            f"transcript={info['transcription_status']} errors={len(info['errors'])}"
        )
def print_scope_summary(
    run_dir: Path,
    deps: dict[str, Any],
    videos: dict[str, dict[str, Any]],
    eligibility: dict[str, Any],
) -> None:
    """scope 模式不生成报告，只回显可审计的资格结果。"""
    print(f"Run directory: {run_dir}")
    print(f"identity relation: {eligibility.get('identity_relation', 'uncertain')}")
    print(f"substitution relation: {eligibility.get('substitution_relation', 'uncertain')}")
    print(f"comparison status: {eligibility.get('overall_status', 'uncertain')}")
    print(f"comparable stages: {','.join(eligibility.get('comparable_stages') or []) or 'none'}")
    print(f"reason: {eligibility.get('reason') or '未提供'}")
    print(f"ffmpeg: {'ok' if deps['ffmpeg'] else 'missing'}")
    asr = deps.get("asr") if isinstance(deps.get("asr"), dict) else {}
    print(f"asr: {asr.get('model') or 'missing'} @ {asr.get('api_url') or 'unconfigured'}")
    for role, info in videos.items():
        print(
            f"{role}: frames={info['frame_count']} "
            f"transcript={info['transcription_status']} errors={len(info['errors'])}"
        )


if __name__ == "__main__":
    sys.exit(main())
