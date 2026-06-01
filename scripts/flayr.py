#!/usr/bin/env python3
"""Flayr MVP command line runner."""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sys
from pathlib import Path
from typing import Any

from flayr_core.llm.api import read_llm_api_key
from flayr_core.llm.pipeline import (
    merge_analysis_result,
    run_large_model_analysis,
)
from flayr_core.prompt import write_analysis_input
from flayr_core.proposal_clip import generate_proposal_clips
from flayr_core.proposal_video import config_from_args
from flayr_core.report import write_report
from flayr_core.shot_track import build_shot_track
from flayr_core.subtitle_track import build_subtitle_track
from flayr_core.translation import sync_chinese_translation, translate_transcript_with_llm
from flayr_core.utils import write_json, write_text
from flayr_core.video import (
    extract_audio,
    extract_frames,
    probe_duration_seconds,
)
from flayr_core.whisper import run_whisper


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS_DIR = ROOT / "runs"
STAGES = [
    ("S1 Hook", "0~3s", "用户凭什么停下来"),
    ("S2 产品引出", "3~6s", "产品为什么现在出现"),
    ("S3 使用过程", "6~15s", "用户能不能看懂怎么用"),
    ("S4 效果呈现", "15~23s", "用户能不能看见价值"),
    ("S5 信任放大", "23~27s", "用户凭什么相信"),
    ("S6 CTA", "最后 3~5s", "用户为什么现在下单"),
]


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    deps = check_dependencies(args)
    inputs = validate_inputs(args)
    run_dir = create_run_dir(args)

    videos: dict[str, dict[str, Any]] = {}
    for role, path in inputs.items():
        videos[role] = process_video(role, path, run_dir, deps, args)

    analysis = build_analysis(args, run_dir, deps, videos)
    analysis_input_path = write_analysis_input(run_dir, analysis)
    if args.llm_model and not args.analysis_result_json:
        llm_result_path = run_large_model_analysis(args, analysis, analysis_input_path, run_dir)
        if llm_result_path:
            args.analysis_result_json = llm_result_path
    if args.analysis_result_json:
        merge_analysis_result(analysis, args.analysis_result_json)
    if args.mode in {"compare", "improve"}:
        analysis["proposal_clips"] = generate_proposal_clips(run_dir, analysis, config_from_args(args))
    write_json(run_dir / "analysis.json", analysis)
    write_analysis_input(run_dir, analysis)

    if args.mode in {"compare", "improve"}:
        plan = build_improved_video_plan(analysis)
        write_json(run_dir / "improved_video_plan.json", plan)
    else:
        plan = None

    report_path = write_report(run_dir, analysis, plan)
    print_summary(run_dir, report_path, deps, videos, plan)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze and improve TikTok commerce short videos.",
    )
    parser.add_argument(
        "mode",
        choices=("breakdown", "compare", "improve"),
        help="Run mode.",
    )
    parser.add_argument("--benchmark-video", type=Path, help="Benchmark video path.")
    parser.add_argument("--creator-video", type=Path, help="Creator video path.")
    parser.add_argument("--product-name", default="未填写", help="Product name.")
    parser.add_argument("--product-category", default="", help="Product category from the structure-library category set.")
    parser.add_argument("--product-price", default="未填写", help="Product price.")
    parser.add_argument(
        "--target-market",
        choices=("auto", "sea", "my"),
        default="auto",
        help="Target market knowledge pack. auto loads SEA/MY seed as judging hints; my enables Malaysia-specific rules.",
    )
    parser.add_argument("--core-selling-points", default="", help="Verified product selling points and differentiation.")
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
        "--whisper-model",
        type=Path,
        default=Path("/Users/wangbo5/Library/Application Support/VidLingo/Models/ggml-large-v3-q5_0.bin"),
        help="Model path for whisper-cli or whisper-cpp. 默认本机的 VidLingo large-v3-q5_0 模型；部署给别人时改成相对路径或 env 变量。",
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
        "--llm-include-images",
        action="store_true",
        help="Attach selected dense focus frames to the LLM request for visual analysis.",
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
        "--with-ocr",
        action="store_true",
        help=(
            "Run subtitle OCR (DashScope qwen-vl-ocr) on sampled frames to build an "
            "authoritative subtitle track. Adds API cost (~18 calls/video). Default off."
        ),
    )
    parser.add_argument(
        "--proposal-video-backend",
        choices=("none", "dashscope-i2v", "dashscope-s2v"),
        default="none",
        help="Optional AI demo clip backend for Top improvements. Default: none.",
    )
    parser.add_argument(
        "--proposal-video-model",
        default="",
        help="Optional Wan model override. Defaults: wan2.6-i2v-flash for i2v, wan2.2-s2v for s2v.",
    )
    parser.add_argument(
        "--proposal-video-api-url",
        default="",
        help="Optional DashScope Wan endpoint override. Defaults to the Beijing endpoint for the selected backend.",
    )
    parser.add_argument(
        "--proposal-video-resolution",
        choices=("480P", "720P", "1080P"),
        default="720P",
        help="Resolution tier for generated proposal clips. Default: 720P.",
    )
    parser.add_argument(
        "--proposal-video-timeout",
        type=int,
        default=600,
        help="Seconds to wait for each DashScope video task when backend is enabled. Default: 600.",
    )
    parser.add_argument(
        "--proposal-video-poll-interval",
        type=int,
        default=15,
        help="Seconds between DashScope task polls. Default: 15.",
    )
    parser.add_argument(
        "--proposal-video-submit-only",
        action="store_true",
        help="Submit DashScope proposal video tasks but do not wait for completion.",
    )
    parser.add_argument(
        "--proposal-face-image-url",
        default="",
        help="Public face image URL fallback for dashscope-s2v. Per-improvement face_image_url overrides this.",
    )
    parser.add_argument(
        "--proposal-line-audio-url",
        default="",
        help="Public line audio URL fallback for dashscope-s2v. Per-improvement line_audio_url overrides this.",
    )
    parser.add_argument(
        "--proposal-skip-s2v-detect",
        action="store_true",
        help="Skip wan2.2-s2v-detect before dashscope-s2v generation.",
    )
    return parser


def create_run_dir(args: argparse.Namespace) -> Path:
    if args.output_dir:
        run_dir = args.output_dir.expanduser().resolve()
    else:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir = DEFAULT_RUNS_DIR / f"{stamp}-{args.mode}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def check_dependencies(args: argparse.Namespace) -> dict[str, Any]:
    whisper_command = first_available(("whisper", "whisper-cpp", "whisper-cli"))
    whisper_model = validate_optional_file(args.whisper_model, "--whisper-model")
    return {
        "ffmpeg": shutil.which("ffmpeg"),
        "ffprobe": shutil.which("ffprobe"),
        "whisper": whisper_command,
        "whisper_model": str(whisper_model) if whisper_model else None,
        "whisper_language": args.whisper_language,
    }


def first_available(commands: tuple[str, ...]) -> str | None:
    for command in commands:
        if shutil.which(command):
            return command
    return None


def validate_inputs(args: argparse.Namespace) -> dict[str, Path]:
    inputs: dict[str, Path] = {}

    if args.mode in {"breakdown", "compare", "improve"}:
        if not args.benchmark_video:
            raise SystemExit("--benchmark-video is required.")
        inputs["benchmark"] = validate_video_path(args.benchmark_video)

    if args.mode in {"compare", "improve"}:
        if not args.creator_video:
            raise SystemExit("--creator-video is required for compare/improve mode.")
        inputs["creator"] = validate_video_path(args.creator_video)

    if args.analysis_result_json:
        args.analysis_result_json = validate_optional_file(args.analysis_result_json, "--analysis-result-json")

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


def process_video(
    role: str,
    video_path: Path,
    run_dir: Path,
    deps: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    role_dir = run_dir / role
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
        "duration_seconds": None,
        "frame_strategy": {
            "base": "1 fps across full video",
            "focus": "2 fps for first 5 seconds and final 5 seconds",
            "stage": "representative frames for S1-S6 from full-video frames",
        },
        "audio_path": None,
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
        "errors": [],
    }

    if deps["ffmpeg"]:
        if deps["ffprobe"]:
            result["duration_seconds"] = probe_duration_seconds(video_path)
        extract_frames(video_path, frames_dir, focus_frames_dir, result)
        extract_audio(video_path, role_dir / "audio.wav", result)
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

    # 镜头轨：本地 ffmpeg 自适应切分，默认跑（无成本）。供 omni 拿精确镜头边界。
    shot_track = build_shot_track(role_dir, video_path, result.get("duration_seconds"))
    result["shot_track_status"] = shot_track.get("status")
    result["shot_track_path"] = str(role_dir / "shot_track.json") if shot_track.get("shots") else None

    # 字幕轨：读光 OCR，有 API 成本，靠 --with-ocr 显式开启（默认关）。
    result["subtitle_track_status"] = "disabled_by_flag"
    result["subtitle_track_path"] = None
    if getattr(args, "with_ocr", False):
        api_key = read_llm_api_key(args).strip()
        subtitle_track = build_subtitle_track(role_dir, result, api_key)
        result["subtitle_track_status"] = subtitle_track.get("status")
        if subtitle_track.get("segments"):
            result["subtitle_track_path"] = str(role_dir / "subtitle_track.json")

    return result


def build_analysis(
    args: argparse.Namespace,
    run_dir: Path,
    deps: dict[str, Any],
    videos: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    stage_analysis = [stage_placeholder(name, time_range, question) for name, time_range, question in STAGES]
    improvements = default_improvements(args.mode)

    # improvements_status 取值：
    #   not_applicable  —— breakdown 模式不需要提升点
    #   llm_unavailable —— compare/improve 模式但 LLM 未跑或失败（初始默认值）
    #   llm_completed   —— LLM 分析已成功合并（由 merge_analysis_result 改写）
    improvements_status = "not_applicable" if args.mode == "breakdown" else "llm_unavailable"

    return {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "mode": args.mode,
        "run_dir": str(run_dir),
        "analysis_scope": analysis_scope(args),
        "product": {
            "name": args.product_name,
            "category": args.product_category,
            "price": args.product_price,
            "target_market": args.target_market,
            "core_selling_points": args.core_selling_points,
            "target_user": args.target_user,
            "purchase_motivation": args.purchase_motivation or "",
            "creator_profile": args.creator_profile,
            "notes": args.product_notes,
        },
        "dependencies": deps,
        "videos": videos,
        "stage_analysis": stage_analysis,
        "improvements": improvements if args.mode in {"compare", "improve"} else [],
        "improvements_status": improvements_status,
        "status": {
            "video_rendered": False,
            "reason": "MVP creates an assembly plan first; final improved.mp4 requires timed replacement audio and subtitles.",
        },
    }


def stage_placeholder(name: str, time_range: str, question: str) -> dict[str, Any]:
    return {
        "stage": name,
        "time_range": time_range,
        "core_question": question,
        "benchmark_summary": "待基于关键帧和转录补充。",
        "creator_summary": "待基于关键帧和转录补充。",
        "gap": "待人工或模型分析后填写。",
        "severity": "medium",
    }


def default_improvements(mode: str) -> list[dict[str, Any]]:
    # LLM 未运行或失败时的占位返回。
    # 真正的"未跑 LLM"提示由 build_analysis 写入 improvements_status，
    # 并在 report.render_improvement_cards 中根据 status 渲染警告区块。
    # 保留此函数仅为兼容现有调用点，后续如需兜底数据可在此恢复。
    return []


def build_improved_video_plan(analysis: dict[str, Any]) -> dict[str, Any]:
    edits = []
    for item in analysis["improvements"]:
        edits.append(
            {
                "type": "visual_note",
                "start": item["time_range"],
                "end": item["time_range"],
                "problem": item["problem"],
                "change": item["suggestion"],
                "gmv_reason": item["gmv_reason"],
                "evidence": item.get("evidence", []),
                "creator_script": item.get("creator_script", ""),
                "requires": ["manual timestamp refinement", "subtitle rewrite", "optional TTS"],
            }
        )

    return {
        "can_render_improved_mp4": False,
        "reason": "Timed replacement scripts, TTS audio, and exact edit points are not complete yet.",
        "source_video": analysis["videos"].get("creator", {}).get("path"),
        "planned_output": str(Path(analysis["run_dir"]) / "improved.mp4"),
        "edits": edits,
    }


def print_summary(
    run_dir: Path,
    report_path: Path,
    deps: dict[str, Any],
    videos: dict[str, dict[str, Any]],
    plan: dict[str, Any] | None,
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
    if plan:
        print("improved.mp4: not rendered; improved_video_plan.json created")


if __name__ == "__main__":
    sys.exit(main())
