"""Improvement proposal clip generation for Flayr reports."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .artifacts import (
    parse_time_range_seconds,
    parse_timestamp_seconds,
    select_frame_for_time_range,
    select_frame_near_timestamp,
)
from .proposal_video import ClipRefs, ProposalVideoConfig, maybe_generate_ai_clip
from .utils import run_command, write_json
from .voice_clone import clone_voice_and_synthesize


MAX_UNITS = 3
DEFAULT_CLIP_SECONDS = 4.0
MAX_CLIP_SECONDS = 5.0
# 无任何话术字段时的占位文案。不能拿去做音色合成（会克隆出"达人念占位"的废样片）。
PLACEHOLDER_LINE = "待补充本地语言话术。"


def generate_proposal_clips(
    run_dir: Path,
    analysis: dict[str, Any],
    video_config: ProposalVideoConfig | None = None,
    limit: int = MAX_UNITS,
    default_duration: float = DEFAULT_CLIP_SECONDS,
    voice_clone_api_key: str = "",
) -> dict[str, Any]:
    """Build proposal micro-units and cut matching creator source clips.

    The module is intentionally downstream of analysis: it does not decide what
    the problems are; it packages the top improvements into report-ready evidence
    and proposal units.

    voice_clone_api_key 非空时，对达人做一次音色克隆，合成各提升点话术音频，
    供 i2v 做口型同步（达人音色）。为空或依赖缺失时静默跳过，不影响切片产出。
    """

    output_dir = run_dir / "proposal_clips"
    output_dir.mkdir(parents=True, exist_ok=True)

    creator = analysis.get("videos", {}).get("creator", {})
    creator_video = Path(str(creator.get("path") or ""))
    creator_duration = numeric_duration(creator.get("duration_seconds"))
    ffmpeg = shutil.which("ffmpeg")
    improvements = sorted(
        [item for item in analysis.get("improvements", []) if isinstance(item, dict)],
        key=lambda item: numeric_priority(item.get("priority")),
    )[: max(0, limit)]

    # voice clone：循环前一次性注册音色 + 合成所有话术（避免每条重复注册）。
    voice_audio_by_id = _maybe_clone_voice(
        run_dir, creator, improvements, voice_clone_api_key,
    )

    units = []
    for rank, item in enumerate(improvements, start=1):
        start, duration = clip_window(item, creator_duration, default_duration)
        original_clip = output_dir / f"proposal_{rank:02d}_original.mp4"
        ai_clip = output_dir / f"proposal_{rank:02d}_ai.mp4"
        anchor_frame = select_anchor_frame(creator, item)
        clip_status, clip_uri, clip_error = cut_original_clip(
            ffmpeg,
            creator_video,
            original_clip,
            start,
            duration,
        )
        prompt = item.get("aigc_prompt") or proposal_prompt(item)
        cloned_audio = voice_audio_by_id.get(f"p{rank}")
        ai_result = maybe_generate_ai_clip(
            video_config or ProposalVideoConfig(),
            ClipRefs(
                prompt=prompt,
                anchor_frame_path=anchor_frame,
                output_path=ai_clip,
                duration_sec=duration,
                face_image_url=str(item.get("face_image_url") or ""),
                line_audio_url=str(item.get("line_audio_url") or ""),
                line_audio_path=Path(cloned_audio) if cloned_audio else None,
            ),
            output_dir / f"proposal_{rank:02d}_ai",
        )
        unit = {
            "rank": rank,
            "stage": item.get("target_stage") or stage_from_title(item.get("title")),
            "source_improvement_priority": item.get("priority", rank),
            "source_time_range": item.get("creator_time_range") or item.get("time_range") or "",
            "clip_start_sec": round(start, 2),
            "duration_sec": round(duration, 2),
            "clip_original_uri": clip_uri,
            "line": local_line(item),
            "line_zh": chinese_line(item),
            "rationale": proposal_rationale(item),
            "source_clip_status": clip_status,
            "generation_status": overall_status(clip_status, ai_result.get("ai_generation_status")),
            "degrade_reason": clip_error or ai_result.get("ai_generation_error") or "",
            "base_frame_evidence_id": item.get("base_frame_evidence_id") or "",
            "anchor_frame_path": str(anchor_frame) if anchor_frame else "",
            "aigc_prompt": prompt,
        }
        unit.update(ai_result)
        units.append(unit)

    result = {
        "version": "0.1",
        "status": "generated" if units else "empty",
        "requires_talent_confirmation": True,
        "max_units": limit,
        "clip_policy": {
            "default_duration_sec": default_duration,
            "max_duration_sec": MAX_CLIP_SECONDS,
            "total_max_duration_sec": limit * MAX_CLIP_SECONDS,
        },
        "ai_backend": video_config.backend if video_config else "none",
        "source_creator_video": str(creator_video) if creator_video else "",
        "units": units,
    }
    write_json(run_dir / "proposal_clips.json", result)
    return result


def clip_window(item: dict[str, Any], video_duration: float, default_duration: float) -> tuple[float, float]:
    duration = max(0.5, min(float(default_duration), MAX_CLIP_SECONDS))
    raw_range = item.get("creator_time_range") or item.get("time_range")
    if raw_range:
        start, end = parse_time_range_seconds(raw_range, video_duration)
        if end > start:
            midpoint = (start + end) / 2
            return clamp_window(midpoint - duration / 2, duration, video_duration)

    timestamp = parse_timestamp_seconds(item.get("best_base_frame_time"))
    if timestamp is not None:
        return clamp_window(timestamp - duration / 2, duration, video_duration)
    return clamp_window(0.0, duration, video_duration)


def clamp_window(start: float, duration: float, video_duration: float) -> tuple[float, float]:
    if video_duration <= 0:
        return max(0.0, start), duration
    duration = min(duration, max(0.5, video_duration))
    start = max(0.0, min(start, max(0.0, video_duration - duration)))
    return start, duration


def select_anchor_frame(info: dict[str, Any], item: dict[str, Any]) -> Path | None:
    best_time = item.get("best_base_frame_time")
    frame = select_frame_near_timestamp(info, best_time) if best_time else None
    if not frame:
        frame = select_frame_for_time_range(info, item.get("creator_time_range") or item.get("time_range") or "")
    if not frame:
        return None
    path = Path(str(frame.get("path") or ""))
    return path if path.is_file() else None


def cut_original_clip(
    ffmpeg: str | None,
    creator_video: Path,
    output_path: Path,
    start: float,
    duration: float,
) -> tuple[str, str | None, str]:
    if not ffmpeg:
        return "source_clip_unavailable", None, "ffmpeg missing; cannot cut source clip."
    if not creator_video.is_file():
        return "source_clip_unavailable", None, "creator video missing; cannot cut source clip."

    command = [
        ffmpeg,
        "-y",
        "-ss",
        f"{start:.2f}",
        "-t",
        f"{duration:.2f}",
        "-i",
        str(creator_video),
        "-vf",
        "scale=720:-2",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    result = run_command(command)
    if result.returncode != 0 or not output_path.is_file():
        error = (result.stderr or result.stdout or "ffmpeg failed").strip().splitlines()
        return "source_clip_unavailable", None, error[-1] if error else "ffmpeg failed."
    return "source_clip_ready", f"{output_path.parent.name}/{output_path.name}", ""


def local_line(item: dict[str, Any]) -> str:
    for key in ("creator_script", "line", "suggested_line", "suggestion"):
        value = str(item.get(key) or "").strip()
        if value:
            return compact_sentence(value, 180)
    return PLACEHOLDER_LINE


def chinese_line(item: dict[str, Any]) -> str:
    for key in ("creator_script_zh", "line_zh", "suggestion_zh", "expected_effect"):
        value = str(item.get(key) or "").strip()
        if value:
            return compact_sentence(value, 140)
    return ""


def proposal_rationale(item: dict[str, Any]) -> str:
    for key in ("gmv_reason", "expected_effect", "problem", "base_frame_reason"):
        value = str(item.get(key) or "").strip()
        if value:
            return compact_sentence(value, 180)
    return "该提案用于把提升点落到可拍、可剪、可确认的 3-5 秒片段。"


def proposal_prompt(item: dict[str, Any]) -> str:
    line = local_line(item)
    suggestion = str(item.get("suggestion") or "").strip()
    return (
        "生成一段 9:16 竖屏带货短视频提案样片，保留达人本人、产品和原场景质感；"
        f"改造方向：{suggestion or proposal_rationale(item)}；"
        f"若出现口播字幕，仅使用这句本地话术：{line}。"
    )


def overall_status(source_status: str, ai_status: Any) -> str:
    if ai_status == "ready":
        return "ai_clip_ready"
    if source_status == "source_clip_ready":
        return "source_clip_ready"
    return str(ai_status or source_status or "unknown")


def compact_sentence(text: str, limit: int) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip("，,。.;； ") + "…"


def numeric_priority(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 999


def numeric_duration(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def stage_from_title(value: Any) -> str:
    text = str(value or "")
    for index in range(1, 7):
        marker = f"S{index}"
        if marker in text:
            return marker
    return "待确认"


def _maybe_clone_voice(
    run_dir: Path,
    creator: dict[str, Any],
    improvements: list[dict[str, Any]],
    api_key: str,
) -> dict[str, str]:
    """对达人做一次音色克隆，合成各提升点话术。返回 {pN: 本地音频路径}。

    api_key 为空、依赖缺失、或合成失败时返回空 dict（i2v 自动退回无配音）。
    话术取每条 improvement 的本地语言话术（local_line），id 对齐 rank（p1/p2/...）。
    """
    if not api_key.strip() or not improvements:
        return {}
    role_dir = run_dir / "creator"
    audio_path = role_dir / "audio.wav"
    srt_path = role_dir / "transcript.srt"
    # 只合成有真实话术的提升点；占位文案跳过（id 仍按 rank 对齐 p1/p2/...）。
    lines = []
    for rank, item in enumerate(improvements, start=1):
        text = local_line(item)
        if text == PLACEHOLDER_LINE:
            continue
        lines.append({"id": f"p{rank}", "text": text})
    if not lines:
        return {}
    # voice clone 任何意外都不应拖垮主分析流程（兑现本模块"不抛错"契约）。
    try:
        result = clone_voice_and_synthesize(role_dir, audio_path, srt_path, lines, api_key)
    except Exception as exc:  # noqa: BLE001 — 网络/SDK/响应畸形等异常类型多，统一兜底
        result = {"status": "failed", "error": f"voice clone 异常: {str(exc)[:160]}", "outputs": []}
    write_json(run_dir / "voice_clone.json", result)
    mapping: dict[str, str] = {}
    for out in result.get("outputs", []):
        if out.get("audio_path"):
            mapping[str(out.get("id"))] = str(out["audio_path"])
    return mapping
