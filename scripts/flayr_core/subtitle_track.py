"""flayr_core.subtitle_track：字幕轨预处理（读光 OCR）。

为什么存在：omni 看视频能"理解字幕想表达什么"，但逐字认字会错
（实测把 TikTok 读成 Daiso），且把多行字幕糊成一长串。带货视频的字幕条
承载了大量核心卖点（年龄段、功效、价格、优惠），需要一条"权威字幕轨"——
和 transcript.srt（权威口播轨）完全对称：专用 OCR 负责认字，omni 负责理解。

范围（验证后诚实框定）：只做"屏幕字幕条"识别。读光对规整、水平、高对比的
字幕条识别准且能逐行切分；但对产品瓶身倾斜小字不稳（会退回纯坐标无文字），
那部分不在本模块职责内，仍由 omni 理解 + 人工兜底。

调用方式：稀疏抽帧（默认每 ~2.5s 取 1 帧）调读光 qwen-vl-ocr，
合并相邻相同字幕，产出 subtitle_track.json。返回纯坐标无文字时重试一次，
仍失败则该帧标记 ocr_unreadable 跳过，不中断整条 pipeline。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .artifacts import format_seconds, get_frame_entries, sample_evenly
from .llm.api import call_llm_api, extract_chat_completion_text, image_to_data_url
from .utils import write_json


OCR_MODEL = "qwen-vl-ocr"
# 稀疏抽帧目标间隔（秒）：字幕变化通常持续数秒，2.5s 采样够用且把调用量砍半。
SAMPLE_INTERVAL_SEC = 2.5
# 读光 OCR 的 OpenAI 兼容端点（与主分析同一个 base，模型不同）。
OCR_API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
OCR_REQUEST_MAX_TIME_SECONDS = 90
OCR_REQUEST_LOW_SPEED_TIME_SECONDS = 45
# ⚠️ 关键：实测中文指令"请识别…按行输出"会触发读光的【检测模式】（返回纯坐标无文字），
# 导致约 40% 帧假阴性。简洁英文指令稳定走【识别模式】，且比无指令多读出小字（如瓶身 12hrs）。
# 改这句前务必用真实帧回归测试，别再写"按行/逐行/输出位置"这类暗示检测的措辞。
OCR_INSTRUCTION = "Output the text content of this image."


def build_subtitle_track(
    role_dir: Path,
    info: dict[str, Any],
    api_key: str,
    api_url: str = OCR_API_URL,
    model: str = OCR_MODEL,
    interval_sec: float = SAMPLE_INTERVAL_SEC,
) -> dict[str, Any]:
    """对单个视频的抽帧做字幕 OCR，产出 subtitle_track.json 并返回结果。

    ffmpeg 抽帧已由 video.py 完成；本模块只复用 frames manifest，不重新抽帧。
    没有 api_key 或没有帧时返回 disabled 状态，由调用方决定是否跳过。
    """
    frames = get_frame_entries(info)
    if not frames:
        return _empty_track("no_frames")
    if not api_key.strip():
        return _empty_track("no_api_key")

    duration = info.get("duration_seconds")
    sampled = sample_frames_by_interval(frames, duration, interval_sec)

    raw_dir = role_dir / "ocr_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    frame_results: list[dict[str, Any]] = []
    for index, entry in enumerate(sampled):
        frame_path = Path(str(entry.get("path") or ""))
        timestamp = float(entry.get("timestamp_seconds") or 0.0)
        if not frame_path.is_file():
            continue
        lines, status = ocr_frame_with_retry(
            frame_path, api_key, api_url, model, raw_dir, index
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


def sample_frames_by_interval(
    frames: list[dict[str, Any]],
    duration: Any,
    interval_sec: float,
) -> list[dict[str, Any]]:
    """按目标时间间隔稀疏取帧。帧已是 1fps，所以约等于每 interval_sec 取 1 帧。"""
    if interval_sec <= 0:
        return frames
    dur = float(duration) if isinstance(duration, (int, float)) and duration else 0.0
    if dur <= 0:
        # 没时长信息时退化为按帧数估算
        target = max(1, round(len(frames) / max(1.0, interval_sec)))
        return sample_evenly(frames, target)
    target = max(1, int(dur // interval_sec) + 1)
    return sample_evenly(frames, target)


def ocr_frame_with_retry(
    frame_path: Path,
    api_key: str,
    api_url: str,
    model: str,
    raw_dir: Path,
    index: int,
) -> tuple[list[str], str]:
    """对单帧 OCR；返回纯坐标无文字时重试一次，仍失败则标 ocr_unreadable。"""
    for attempt in range(2):
        request_path = raw_dir / f"ocr_{index:03d}_req.json"
        response_path = raw_dir / f"ocr_{index:03d}_resp.json"
        write_json(request_path, build_ocr_payload(frame_path, model))
        try:
            raw = call_llm_api(
                api_url,
                api_key,
                request_path,
                response_path,
                max_time_seconds=OCR_REQUEST_MAX_TIME_SECONDS,
                low_speed_time_seconds=OCR_REQUEST_LOW_SPEED_TIME_SECONDS,
                retries=0,
            )
        except SystemExit as exc:
            if attempt == 0:
                continue
            return [], f"ocr_request_failed: {str(exc)[:80]}"
        text = extract_chat_completion_text(json.loads(raw))
        lines = parse_ocr_lines(text)
        if lines:
            return lines, "ocr_ready"
        # 无文字（多半是退回纯坐标检测模式）→ 重试一次
    return [], "ocr_unreadable"


def build_ocr_payload(frame_path: Path, model: str) -> dict[str, Any]:
    """读光 qwen-vl-ocr 请求体。min/max_pixels 控制识别分辨率。"""
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": image_to_data_url(frame_path)},
                        "min_pixels": 3136,
                        "max_pixels": 1003520,
                    },
                    {"type": "text", "text": OCR_INSTRUCTION},
                ],
            }
        ],
        "max_tokens": 600,
    }


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
