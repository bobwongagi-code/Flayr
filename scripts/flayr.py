#!/usr/bin/env python3
"""Flayr MVP command line runner."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from flayr_core.audio_quality import analyze_audio_quality
from flayr_core.analysis_model import ANALYSIS_RESULT_CONTRACT, placeholder_stages
from flayr_core.bd_report import write_bd_report
from flayr_core.llm.api import can_analyze_native_audio, provider_capabilities, read_llm_api_key
from flayr_core.llm.pipeline import (
    apply_finalized_analysis_result,
    merge_analysis_result,
    run_comparison_scope_preflight,
    run_large_model_analysis,
)
from flayr_core.prompt import write_analysis_input
from flayr_core.creator_report import write_creator_report
from flayr_core.report import write_report
from flayr_core.resources import ResourceBudget, ResourceBudgetExceeded, finite_nonnegative
from flayr_core.run_manifest import SUCCESS_MANIFEST_NAME, command_digest, write_success_manifest
from flayr_core.motion import compute_shake_metric
from flayr_core.market import normalize_target_market
from flayr_core.shot_track import build_shot_track
from flayr_core.speech_mode import classify_speech_mode
from flayr_core.subtitle_track import build_subtitle_track
from flayr_core.translation import sync_chinese_translation, translate_transcript_with_llm
from flayr_core.utils import write_json, write_text
from flayr_core.video import (
    extract_audio,
    extract_frames,
    probe_duration_seconds,
    reserve_existing_media_artifacts,
)
from flayr_core.video_evidence import build_video_evidence_artifacts
from flayr_core.whisper import run_whisper


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS_DIR = ROOT / "runs"
PREPROCESS_CACHE_SCHEMA_VERSION = 2
PREPROCESS_PIPELINE_VERSION = "2026-07-18.1"
PREPROCESS_ARTIFACT_SCHEMA_VERSION = 1
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
        "postprocess_change_log.json",
        "product_foundation.json",
        "raw_model_response.json",
        "report.html",
        "validated_normalized_result.json",
    }
)
_RUN_OUTPUT_PREFIXES = (
    "absolute_execution_",
    "llm_",
    "video_facts_",
    "video_identity_",
)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    budget = ResourceBudget()
    # 所有预处理、OCR、LLM、下载、报告和子进程都从这个 run 级对象取预算。
    budget.activate()
    args._resource_budget = budget

    deps = check_dependencies(args)
    inputs = validate_inputs(args)
    source_durations: dict[str, float] = {}
    for role, path in inputs.items():
        budget.preflight_source(path)
        duration = probe_duration_seconds(path)
        source_durations[role] = budget.register_source(path, duration)
    deps["source_durations"] = source_durations
    run_dir = create_run_dir(args)
    # A direct rerun must invalidate an old completion marker before any new
    # artifact is written. The batch runner also removes invalid markers.
    (run_dir / SUCCESS_MANIFEST_NAME).unlink(missing_ok=True)

    videos: dict[str, dict[str, Any]] = {}
    for role, path in inputs.items():
        videos[role] = process_video(role, path, run_dir, deps, args, budget=budget)

    analysis = build_analysis(args, run_dir, deps, videos, budget=budget)
    analysis_input_path = write_analysis_input(run_dir, analysis)
    if args.mode == "scope":
        eligibility = run_comparison_scope_preflight(args, analysis, run_dir)
        analysis["resource_budget"] = budget.snapshot()
        write_json(run_dir / "analysis.json", analysis)
        write_analysis_input(run_dir, analysis)
        print_scope_summary(run_dir, deps, videos, eligibility)
        return 0
    if args.llm_model and not args.analysis_result_json:
        completed = run_large_model_analysis(args, analysis, analysis_input_path, run_dir)
        if completed:
            llm_result_path, normalized_result = completed
            apply_finalized_analysis_result(analysis, normalized_result, llm_result_path)
    elif args.analysis_result_json:
        merge_analysis_result(analysis, args.analysis_result_json, analysis_input_path.read_text(encoding="utf-8"))
    comparison_stopped = analysis.get("analysis_status") in {"not_comparable", "comparison_uncertain"}
    if args.mode in {"compare", "improve"} and analysis.get("analysis_run_state") == "not_run":
        if not getattr(args, "allow_degraded", False):
            write_json(run_dir / "analysis.json", analysis)
            raise SystemExit(
                "compare/improve 需要完成的 LLM 分析，但当前 analysis_run_state=not_run。"
                " 提供 --llm-model 跑分析，或加 --allow-degraded 在无分析时继续（severity 留空）。"
            )
        analysis["analysis_run_state"] = "degraded"
        write_json(
            run_dir / "degraded_manifest.json",
            {
                "analysis_run_state": "degraded",
                "reason": "LLM 分析未运行或未完成；severity/improvements 为占位，不可作为业务判断。",
                "stage_analysis": analysis.get("stage_analysis", []),
                "improvements": analysis.get("improvements", []),
            },
        )
    analysis["resource_budget"] = budget.snapshot()
    write_json(run_dir / "analysis.json", analysis)
    write_analysis_input(run_dir, analysis)

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
    print_summary(run_dir, report_path, deps, videos)
    return 0


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
        "--skip-whisper",
        action="store_true",
        help="Skip transcription even when Whisper exists.",
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
        "--whisper-model",
        type=Path,
        default=None,
        help="Model path for whisper-cli or whisper-cpp. Keep machine-specific model paths outside the repository.",
    )
    parser.add_argument(
        "--whisper-model-th",
        type=Path,
        default=None,
        help="Optional Thai Whisper model path. Keep machine-specific model paths outside the repository.",
    )
    parser.add_argument(
        "--whisper-language",
        default="auto",
        help="Speech language passed to Whisper. Default: auto. Use zh, ms, th, id, en only when known.",
    )
    parser.add_argument(
        "--analysis-result-json",
        type=Path,
        help="Optional large-model analysis JSON to merge into analysis.json and report.html.",
    )
    parser.add_argument(
        "--llm-model",
        help="Optional OpenAI-compatible chat model used to generate analysis_result.json.",
    )
    parser.add_argument(
        "--llm-api-url",
        default="https://api.openai.com/v1/chat/completions",
        help="OpenAI-compatible chat completions endpoint.",
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
            "Allow compare/improve to proceed without a completed LLM analysis. "
            "Without this flag, missing analysis exits non-zero. "
            "When set, severity stays null and a degraded manifest is written."
        ),
    )
    parser.add_argument(
        "--llm-include-images",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Use the full Step-0 + per-video fact extraction + multimodal comparison pipeline. "
            "Enabled by default; --no-llm-include-images is legacy text-only compatibility mode."
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
        help="Maximum total focus frames attached when --llm-include-images is used. Default: 12.",
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
    entries = list(run_dir.iterdir())
    if not entries:
        return
    if not reuse:
        raise SystemExit(
            f"--output-dir 已存在且非空：{run_dir}。为避免混入旧产物，请使用新目录，"
            "或显式添加 --reuse-preprocessing。"
        )
    for entry in entries:
        if entry.is_dir() and not entry.is_symlink() and entry.name in _RUN_ROLE_DIRS:
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


def check_dependencies(args: argparse.Namespace) -> dict[str, Any]:
    whisper_command = first_available(("whisper", "whisper-cpp", "whisper-cli"))
    whisper_model = validate_optional_file(args.whisper_model, "--whisper-model")
    # 泰语模型软解析：文件缺失时存 None，由 run_whisper 回退到通用模型，不在启动期硬崩。
    whisper_model_th = resolve_optional_model(args.whisper_model_th)
    return {
        "ffmpeg": shutil.which("ffmpeg"),
        "ffprobe": shutil.which("ffprobe"),
        "whisper": whisper_command,
        "whisper_model": str(whisper_model) if whisper_model else None,
        "whisper_model_th": str(whisper_model_th) if whisper_model_th else None,
        "whisper_language": args.whisper_language,
    }


def resolve_optional_model(path: Path | None) -> Path | None:
    """解析可选模型路径：存在则返回绝对路径，否则返回 None（用于优雅降级，不抛错）。"""
    if not path:
        return None
    resolved = path.expanduser().resolve()
    return resolved if resolved.is_file() else None


def first_available(commands: tuple[str, ...]) -> str | None:
    for command in commands:
        if shutil.which(command):
            return command
    return None


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


def _file_metadata(path: Any, include_sha256: bool = False) -> dict[str, Any] | None:
    """返回缓存判定所需的文件身份；源视频额外用 SHA-256 防止同路径误复用。"""
    if not path:
        return None
    candidate = Path(str(path)).expanduser().resolve()
    if not candidate.is_file():
        return {"path": str(candidate), "missing": True}
    stat = candidate.stat()
    metadata: dict[str, Any] = {
        "path": str(candidate),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
    if include_sha256:
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
) -> dict[str, Any]:
    """缓存只在源视频与所有会改变预处理产物的配置完全一致时命中。"""
    return {
        "cache_schema_version": PREPROCESS_CACHE_SCHEMA_VERSION,
        "pipeline_version": PREPROCESS_PIPELINE_VERSION,
        "code_commit": _git_commit_sha(),
        "source_video": _file_metadata(video_path, include_sha256=True),
        "media_tools": {
            "ffmpeg": _binary_version(deps, "ffmpeg"),
            "ffprobe": _binary_version(deps, "ffprobe"),
        },
        "transcription": {
            "skip_whisper": bool(getattr(args, "skip_whisper", False)),
            "requested_language": str(getattr(args, "whisper_language", "auto") or "auto"),
            "command": _binary_version(deps, "whisper"),
            "model": _file_metadata(deps.get("whisper_model")),
            "thai_model": _file_metadata(deps.get("whisper_model_th")),
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
        "frame_strategy": "base-1fps-focus-2fps-shot-track-v1",
    }


def load_existing_video_result(
    role_dir: Path,
    expected_fingerprint: dict[str, Any],
) -> dict[str, Any] | None:
    """复用上次预处理；缺少、不匹配、未完成的缓存一律重抽。"""
    cache = role_dir / "_preprocess.json"
    if not cache.is_file():
        return None
    try:
        info = json.loads(cache.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if info.get("preprocess_fingerprint") != expected_fingerprint:
        return None
    if info.get("preprocess_completed") is not True:
        return None
    if not _preprocess_artifacts_match(role_dir, info.get("preprocess_artifacts")):
        return None
    frames_dir = Path(str(info.get("frames_dir") or ""))
    transcript = Path(str(info.get("transcript_path") or ""))
    if not frames_dir.is_dir():
        return None
    if not transcript.is_file() or _is_stale_placeholder(transcript):
        return None
    segment_path = Path(str(info.get("transcript_segments_path") or transcript.with_name(transcript.stem + ".txt")))
    if not segment_path.is_file() or _is_stale_placeholder(segment_path):
        return None
    audio_value = str(info.get("audio_path") or "").strip()
    if audio_value and not Path(audio_value).is_file():
        return None
    return info


def _preprocess_artifact_metadata(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"size_bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def _build_preprocess_artifact_manifest(role_dir: Path) -> dict[str, Any]:
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
        files[relative] = _preprocess_artifact_metadata(resolved)
    return {"schema_version": PREPROCESS_ARTIFACT_SCHEMA_VERSION, "files": files}


def _preprocess_artifacts_match(role_dir: Path, value: Any) -> bool:
    if not isinstance(value, dict) or value.get("schema_version") != PREPROCESS_ARTIFACT_SCHEMA_VERSION:
        return False
    recorded = value.get("files")
    if not isinstance(recorded, dict) or not recorded:
        return False
    root = role_dir.expanduser().resolve()
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
    return text.startswith(("待转写", "待翻译", "待生成", "pending:"))


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
        fingerprint = build_preprocess_fingerprint(video_path, deps, args)
        cached = load_existing_video_result(role_dir, fingerprint)
        if cached is not None:
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
        "stage_frame_manifest_path": None,
        "stage_frames": [],
        "video_evidence": {},
        "duration_seconds": None,
        "frame_strategy": {
            "base": "1 fps across full video",
            "focus": "2 fps for first 5 seconds and final 5 seconds",
            "stage": "representative frames for S1-S6 from full-video frames",
        },
        "audio_path": None,
        "audio_quality": {},
        "transcript_path": None,
        "transcript_segments_path": None,
        "transcription_status": "not_started",
        "requested_language": args.whisper_language,
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
    if args.skip_whisper:
        write_text(transcript_path, "Whisper skipped by --skip-whisper.\n")
        result["transcription_status"] = "skipped"
    elif deps["whisper"] and result["audio_path"]:
        run_whisper(deps, Path(result["audio_path"]), role_dir, transcript_path, result)
    else:
        write_text(transcript_path, "Whisper unavailable or audio extraction failed.\n")
        result["transcription_status"] = "placeholder"

    result["transcript_path"] = str(transcript_path)
    sync_chinese_translation(role_dir, result)
    if args.translate_with_llm:
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
    should_ocr, ocr_key, ocr_disabled_reason = resolve_ocr_policy(args)
    if should_ocr:
        subtitle_track = build_subtitle_track(
            role_dir,
            result,
            ocr_key,
            api_url=args.llm_api_url,
            model=args.llm_model,
            budget=budget,
        )
        result["subtitle_track_status"] = subtitle_track.get("status")
        if subtitle_track.get("segments"):
            result["subtitle_track_path"] = str(role_dir / "subtitle_track.json")
    else:
        result["subtitle_track_status"] = ocr_disabled_reason

    result["speech_mode"] = classify_speech_mode(role_dir, result)

    # 二级证据视图：去重审计、顺序联系表、packed transcript、timeline view。
    # 这些 artifact 只用于复核和后续模型证据定位，不直接改变评分。
    result["video_evidence"] = build_video_evidence_artifacts(role_dir, result)
    result["preprocess_fingerprint"] = build_preprocess_fingerprint(video_path, deps, args)

    # 落盘预处理结果，供 --reuse-preprocessing 下次复用（即使本次 LLM 阶段后续失败也已写）。
    result["preprocess_completed"] = True
    result["preprocess_artifacts"] = _build_preprocess_artifact_manifest(role_dir)
    write_json(role_dir / "_preprocess.json", result)
    return result


def ensure_video_evidence_artifacts(role_dir: Path, info: dict[str, Any]) -> None:
    """Ensure reused preprocessing also has secondary evidence artifacts."""
    if not isinstance(info.get("audio_quality"), dict) or not info.get("audio_quality"):
        info["audio_quality"] = analyze_audio_quality(
            Path(str(info["audio_path"])) if info.get("audio_path") else None,
            info.get("duration_seconds"),
        )
    if not isinstance(info.get("speech_mode"), dict) or not info.get("speech_mode", {}).get("mode"):
        info["speech_mode"] = classify_speech_mode(role_dir, info)
    existing = info.get("video_evidence") if isinstance(info.get("video_evidence"), dict) else {}
    timeline_dir = Path(str(existing.get("timeline_views_dir") or role_dir / "timeline_views"))
    selection_report = Path(str(existing.get("frame_selection_report_path") or role_dir / "frames" / "selection_report.json"))
    audit_path = Path(str(existing.get("audit_path") or role_dir / "video_evidence_audit.json"))
    transcript_ready = not (role_dir / "transcript.srt").is_file() or Path(
        str(existing.get("transcript_pack_path") or role_dir / "transcript_packed.md")
    ).is_file()
    if existing and timeline_dir.is_dir() and selection_report.is_file() and audit_path.is_file() and transcript_ready:
        return
    info["video_evidence"] = build_video_evidence_artifacts(role_dir, info)
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
    print(f"whisper: {deps['whisper'] or 'missing'}")
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
    print(f"whisper: {deps['whisper'] or 'missing'}")
    for role, info in videos.items():
        print(
            f"{role}: frames={info['frame_count']} "
            f"transcript={info['transcription_status']} errors={len(info['errors'])}"
        )


if __name__ == "__main__":
    sys.exit(main())
