"""flayr_core.subtitle_track：字幕轨预处理（多模态 OCR）。

为什么存在：omni 看视频能"理解字幕想表达什么"，但逐字认字会错
（实测把 TikTok 读成 Daiso），且把多行字幕糊成一长串。带货视频的字幕条
承载了大量核心卖点（年龄段、功效、价格、优惠），需要一条"权威字幕轨"——
和 transcript.srt（权威口播轨）完全对称：专用 OCR 负责认字，omni 负责理解。

范围（验证后诚实框定）：只做"屏幕字幕条"识别。读光对规整、水平、高对比的
字幕条识别准且能逐行切分；但对产品瓶身倾斜小字不稳（会退回纯坐标无文字），
那部分不在本模块职责内，仍由 omni 理解 + 人工兜底。

调用方式：稀疏抽帧（默认每 ~2.5s 取 1 帧）调用配置的视觉模型，
合并相邻相同字幕，产出 subtitle_track.json。返回纯坐标无文字时重试一次，
仍失败则该帧标记 ocr_unreadable 跳过，不中断整条 pipeline。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .artifacts import (
    format_seconds,
    get_focus_frame_entries,
    get_frame_entries,
    parse_timestamp_seconds,
    resolve_artifact_path,
    sample_evenly,
)
from .llm.api import call_llm_api, extract_chat_completion_text, image_to_data_url
from .llm.provider_artifacts import provider_call_with_artifact, provider_role_replay_root
from .resources import ResourceBudget, ResourceBudgetExceeded, current_budget, finite_nonnegative
from .utils import write_json


OCR_MODEL = ""
# 稀疏抽帧目标间隔（秒）：字幕变化通常持续数秒，2.5s 采样够用且把调用量砍半。
SAMPLE_INTERVAL_SEC = 2.5
OCR_API_URL = ""
OCR_REQUEST_MAX_TIME_SECONDS = 90
OCR_REQUEST_LOW_SPEED_TIME_SECONDS = 45
MAX_OCR_ANCHOR_FRAMES = 24
# Qwen vision endpoints reject images below this tokenized pixel budget.
OCR_MIN_PIXELS = 65536
OCR_MAX_PIXELS = 1003520
# 只取短视频内容字幕，避免把平台 UI、水印和包装字混进权威字幕轨。
OCR_INSTRUCTION = (
    "只输出画面中用于视频内容表达的屏幕字幕原文，每行一条。"
    "忽略软件界面、状态栏、按钮、产品包装和水印。不要解释；没有则输出 NONE。"
)


def build_subtitle_track(
    role_dir: Path,
    info: dict[str, Any],
    api_key: str,
    api_url: str = OCR_API_URL,
    model: str = OCR_MODEL,
    interval_sec: float = SAMPLE_INTERVAL_SEC,
    budget: ResourceBudget | None = None,
    provider_replay_from: Path | None = None,
    replay_role_name: str | None = None,
) -> dict[str, Any]:
    """对单个视频的抽帧做字幕 OCR，产出 subtitle_track.json 并返回结果。

    ffmpeg 抽帧已由 video.py 完成；本模块只复用 frames manifest，不重新抽帧。
    没有 api_key 或没有帧时返回 disabled 状态，由调用方决定是否跳过。
    """
    budget = budget or current_budget() or ResourceBudget()
    frames = _merge_ocr_frame_entries(info)
    if not frames:
        return _empty_track("no_frames")
    if not api_key.strip() and provider_replay_from is None:
        return _empty_track("no_api_key")
    if not str(api_url or "").strip() or not str(model or "").strip():
        return _empty_track("no_vision_provider")

    duration = info.get("duration_seconds")
    try:
        interval_sec = finite_nonnegative(interval_sec, "OCR sample interval")
    except ValueError:
        return _empty_track("invalid_interval")
    sampled = sample_frames_by_interval(frames, duration, interval_sec)

    raw_dir = role_dir / "ocr_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    replay_raw_dir = (
        provider_role_replay_root(provider_replay_from, str(replay_role_name or role_dir.name)) / "ocr_raw"
        if provider_replay_from is not None
        else None
    )

    frame_results: list[dict[str, Any]] = []
    for index, entry in enumerate(sampled):
        if budget is not None and budget.ocr_calls >= budget.limits.max_ocr_calls:
            break
        frame_path = Path(str(entry.get("path") or ""))
        timestamp = parse_timestamp_seconds(entry.get("timestamp_seconds"))
        if timestamp is None:
            continue
        if not frame_path.is_file():
            continue
        lines, status = ocr_frame_with_retry(
            frame_path,
            api_key,
            api_url,
            model,
            raw_dir,
            index,
            budget=budget,
            provider_replay_from=replay_raw_dir,
        )
        frame_results.append(
            {
                "timestamp_sec": round(timestamp, 2),
                "timestamp": format_seconds(timestamp),
                "frame_path": str(frame_path),  # 保留帧路径，便于人工核对 OCR 准不准
                "lines": lines,
                "ocr_status": status,
            }
        )

    segments = merge_adjacent_subtitles(frame_results)
    track = {
        "version": "0.1",
        "model": model,
        "sample_interval_sec": interval_sec,
        "frame_count": len(frame_results),
        "segment_count": len(segments),
        "status": "ready" if segments else "empty",
        "frames": frame_results,
        "segments": segments,
    }
    write_json(role_dir / "subtitle_track.json", track)
    return track


def _merge_ocr_frame_entries(info: dict[str, Any]) -> list[dict[str, Any]]:
    """Use base/focus frames plus scene-boundary anchors without duplicates."""
    merged: list[dict[str, Any]] = []
    by_path: dict[str, dict[str, Any]] = {}
    for entry in [*get_frame_entries(info), *get_focus_frame_entries(info)]:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "")
        if not path or path in by_path:
            continue
        by_path[path] = dict(entry)

    timed = [
        entry for entry in by_path.values()
        if parse_timestamp_seconds(entry.get("timestamp_seconds")) is not None
    ]
    if timed:
        sorted_timed = sorted(timed, key=lambda entry: float(parse_timestamp_seconds(entry["timestamp_seconds"]) or 0.0))
        for entry in (sorted_timed[0], sorted_timed[-1]):
            entry["ocr_anchor"] = "edge"

    raw_shot_path = str(info.get("shot_track_path") or "").strip()
    if not raw_shot_path and isinstance(info.get("video_evidence"), dict):
        raw_shot_path = str(info["video_evidence"].get("shot_track_path") or "").strip()
    shot_path = resolve_artifact_path(info, raw_shot_path, require_file=True)
    if shot_path is not None:
        try:
            shot_track = json.loads(shot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            shot_track = None
        for shot in shot_track.get("shots", []) if isinstance(shot_track, dict) else []:
            if not isinstance(shot, dict):
                continue
            for key in ("start_sec", "end_sec"):
                target = parse_timestamp_seconds(shot.get(key))
                if target is None or not timed:
                    continue
                nearest = min(
                    timed,
                    key=lambda entry: abs(
                        float(parse_timestamp_seconds(entry.get("timestamp_seconds")) or 0.0) - target
                    ),
                )
                nearest["ocr_anchor"] = "scene_boundary"

    merged = list(by_path.values())
    return sorted(
        merged,
        key=lambda entry: parse_timestamp_seconds(entry.get("timestamp_seconds")) or 0.0,
    )


def sample_frames_by_interval(
    frames: list[dict[str, Any]],
    duration: Any,
    interval_sec: float,
) -> list[dict[str, Any]]:
    """按目标时间间隔稀疏取帧；基础帧已按预算自适应抽取。"""
    if interval_sec <= 0:
        return frames
    if duration is None or str(duration).strip() == "":
        dur = None
    else:
        try:
            dur = finite_nonnegative(duration, "video duration")
        except ValueError:
            return []
    if dur is None:
        # 没有时长信息时才允许按帧数估算；非法时长不能伪装成缺失。
        target = max(1, round(len(frames) / max(1.0, interval_sec)))
        return _sample_with_anchors(frames, target)
    if dur <= 0:
        return []
    target = max(1, int(dur // interval_sec) + 1)
    return _sample_with_anchors(frames, target)


def _sample_with_anchors(frames: list[dict[str, Any]], target: int) -> list[dict[str, Any]]:
    """Keep structural OCR anchors, then fill the remaining interval budget."""
    if target <= 0 or not frames:
        return []
    anchors = [entry for entry in frames if entry.get("ocr_anchor")]
    anchors = sample_evenly(anchors, min(target, MAX_OCR_ANCHOR_FRAMES))
    used = {str(entry.get("path") or "") for entry in anchors}
    remaining = [entry for entry in frames if str(entry.get("path") or "") not in used]
    selected = [*anchors, *sample_evenly(remaining, max(0, target - len(anchors)))]
    return sorted(
        selected,
        key=lambda entry: parse_timestamp_seconds(entry.get("timestamp_seconds"))
        if parse_timestamp_seconds(entry.get("timestamp_seconds")) is not None
        else float("inf"),
    )


def ocr_frame_with_retry(
    frame_path: Path,
    api_key: str,
    api_url: str,
    model: str,
    raw_dir: Path,
    index: int,
    budget: ResourceBudget | None = None,
    provider_replay_from: Path | None = None,
) -> tuple[list[str], str]:
    """对单帧 OCR；返回纯坐标无文字时重试一次，仍失败则标 ocr_unreadable。"""
    for attempt in range(2):
        request_path = raw_dir / f"ocr_{index:03d}_req.json"
        response_path = raw_dir / f"ocr_{index:03d}_resp.json"
        meta_path = raw_dir / f"ocr_{index:03d}_attempt{attempt + 1}_meta.json"
        response_meta: dict[str, Any] = {}
        live_meta: dict[str, Any] = {}
        payload = build_ocr_payload(frame_path, model)
        write_json(request_path, payload)
        try:
            provider_response, response_meta, execution_source = provider_call_with_artifact(
                artifact_path=raw_dir / f"provider_ocr_{index:03d}_attempt{attempt + 1}.json",
                replay_root=provider_replay_from,
                call_kind=f"ocr:{index:03d}:{attempt + 1}",
                payload=payload,
                model=model,
                api_url=api_url,
                response_meta=live_meta,
                call=lambda: (
                    json.loads(
                        call_llm_api(
                            api_url,
                            api_key,
                            request_path,
                            response_path,
                            max_time_seconds=OCR_REQUEST_MAX_TIME_SECONDS,
                            low_speed_time_seconds=OCR_REQUEST_LOW_SPEED_TIME_SECONDS,
                            retries=0,
                            output_expansions=0,
                            budget=budget,
                            call_kind="ocr",
                            request_id=f"ocr-{index:03d}-{attempt + 1}",
                            response_meta=live_meta,
                        )
                    ),
                    live_meta,
                ),
            )
        except (Exception, SystemExit) as exc:  # noqa: BLE001 — live OCR is optional; replay remains strict.
            if provider_replay_from is not None:
                raise
            write_json(
                meta_path,
                {
                    "schema_version": 1,
                    "attempt": attempt + 1,
                    "status": "failed",
                    "error": str(exc)[:500],
                    "provider_meta": response_meta,
                    "provider_artifact": f"provider_ocr_{index:03d}_attempt{attempt + 1}.json",
                },
            )
            if "budget" in str(exc).lower() or "exceeded" in str(exc).lower():
                return [], f"ocr_budget_exhausted: {str(exc)[:80]}"
            if attempt == 0:
                continue
            return [], f"ocr_request_failed: {str(exc)[:80]}"
        try:
            text = extract_chat_completion_text(provider_response)
        except (Exception, SystemExit) as exc:  # noqa: BLE001 — malformed live OCR is retried.
            if provider_replay_from is not None:
                raise
            write_json(
                meta_path,
                {
                    "schema_version": 1,
                    "attempt": attempt + 1,
                    "status": "invalid_response",
                    "error": str(exc)[:500],
                    "provider_meta": response_meta,
                    "provider_artifact": f"provider_ocr_{index:03d}_attempt{attempt + 1}.json",
                },
            )
            if attempt == 0:
                continue
            return [], f"ocr_request_failed: {str(exc)[:80]}"
        lines = parse_ocr_lines(text)
        write_json(
            meta_path,
            {
                "schema_version": 1,
                "attempt": attempt + 1,
                "status": "completed" if lines else "empty_ocr",
                "line_count": len(lines),
                "provider_meta": response_meta,
                "provider_artifact": f"provider_ocr_{index:03d}_attempt{attempt + 1}.json",
                "execution_source": execution_source,
            },
        )
        if lines:
            return lines, "ocr_ready"
        # 无文字（多半是退回纯坐标检测模式）→ 重试一次
    return [], "ocr_unreadable"


def build_ocr_payload(frame_path: Path, model: str) -> dict[str, Any]:
    """Approved-provider vision OCR request payload."""
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": image_to_data_url(frame_path)},
                        "min_pixels": OCR_MIN_PIXELS,
                        "max_pixels": OCR_MAX_PIXELS,
                    },
                    {"type": "text", "text": OCR_INSTRUCTION},
                ],
            }
        ],
        "temperature": 0.0,
        "max_tokens": 2048,
    }
    return payload


def parse_ocr_lines(text: str) -> list[str]:
    """从读光返回里抽出文字行。

    读光两种返回：① 纯坐标 "x,y,w,h,angle"（检测模式，无文字）→ 视为空；
    ② "x,y,w,h,angle, 文字" 或纯文字行。提取文字部分，丢掉纯坐标行。
    """
    lines: list[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # 纯坐标行：全是数字和逗号 → 跳过（检测模式无文字）
        if re.fullmatch(r"[\d,\.\s]+", line):
            continue
        # 形如 "498,120,41,605,90,文字" → 取最后一段逗号后的文字
        match = re.match(r"^(?:\d+\s*,\s*){4,5}(.+)$", line)
        text_part = match.group(1).strip() if match else line
        if text_part and not re.fullmatch(r"[\d,\.\s]+", text_part):
            lines.append(text_part)
    return lines


def merge_adjacent_subtitles(frame_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """合并相邻帧中重复的字幕行，形成带起止时间的字幕段。

    带货视频同一句字幕常持续数秒（跨多个采样帧），合并后更接近"一句字幕一条"。
    """
    segments: list[dict[str, Any]] = []
    for frame in frame_results:
        ts = frame["timestamp_sec"]
        for line in frame.get("lines", []):
            normalized = normalize_line(line)
            if not normalized:
                continue
            existing = _find_recent_segment(segments, normalized, ts)
            if existing is not None:
                existing["end_sec"] = ts
                existing["frame_count"] += 1
            else:
                segments.append(
                    {
                        "text": line,
                        "normalized": normalized,
                        "start_sec": ts,
                        "end_sec": ts,
                        "frame_count": 1,
                    }
                )
    for seg in segments:
        seg["start"] = format_seconds(seg["start_sec"])
        seg["end"] = format_seconds(seg["end_sec"])
    return segments


def _find_recent_segment(
    segments: list[dict[str, Any]],
    normalized: str,
    ts: float,
) -> dict[str, Any] | None:
    """在已有段里找同一字幕（归一化后相同）且时间相邻（≤6s 间隔）的段。"""
    for seg in reversed(segments):
        if seg["normalized"] == normalized and ts - seg["end_sec"] <= 6.0:
            return seg
    return None


def normalize_line(line: str) -> str:
    """归一化字幕行用于去重：去空格、转小写、去标点。"""
    return re.sub(r"[\s\W]+", "", str(line or "").lower())


def render_subtitle_track_markdown(track: dict[str, Any]) -> str:
    """把字幕轨渲染成给 omni 看的 markdown（喂进 analysis_input）。"""
    segments = track.get("segments") or []
    if not segments:
        return "（未识别到字幕，或 OCR 未启用）"
    lines = []
    for seg in segments:
        lines.append(f"- {seg.get('start')} - {seg.get('end')}: {seg.get('text')}")
    return "\n".join(lines)


def _empty_track(reason: str) -> dict[str, Any]:
    return {
        "version": "0.1",
        "status": "disabled",
        "disabled_reason": reason,
        "frame_count": 0,
        "segment_count": 0,
        "frames": [],
        "segments": [],
    }
