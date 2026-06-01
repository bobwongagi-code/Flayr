"""flayr_core.shot_track：镜头切分预处理（ffmpeg 场景检测）。

为什么存在：omni 看连续帧能"感觉到"画面变化，但给不出精确的镜头切点时间戳
（它只会说"大概第几秒有个转场"）。镜头切点是带货视频的硬结构信号——
阶段切分、Phase C 回看、提案样片切片都需要切在真正的镜头边界上，
不能切出半个转场。

和 subtitle_track（字幕轨）、transcript.srt（口播轨）一样，这是一条
"用确定性工具补 omni 测不准"的预处理轨：ffmpeg 算精确切点，omni 理解镜头含义。

零新依赖：复用 ffmpeg 的 scene 滤镜，与 video.py 同一套 run_command 调用。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from .artifacts import format_seconds
from .utils import run_command, write_json


# 场景切分阈值：实测 0.3 对带货视频适中（0.2 过敏感把镜头内大动作也算切点，
# 0.4 过钝会漏真实切换）。改这个值前用真实视频回归两端：别切太碎、也别漏。
DEFAULT_SCENE_THRESHOLD = 0.3
# 两个切点间隔小于这个秒数时视为同一次切换的抖动，合并掉。
MIN_SHOT_GAP_SEC = 0.4


def build_shot_track(
    role_dir: Path,
    video_path: Path,
    duration_seconds: Any,
    threshold: float = DEFAULT_SCENE_THRESHOLD,
) -> dict[str, Any]:
    """对单个视频做镜头切分，产出 shot_track.json 并返回结果。

    ffmpeg 不可用或视频缺失时返回 disabled 状态，由调用方决定是否跳过，
    不中断整条 pipeline（与 subtitle_track 的降级策略一致）。
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return _empty_track("ffmpeg_missing")
    if not video_path.is_file():
        return _empty_track("video_missing")

    cut_points = detect_scene_cuts(ffmpeg, video_path, threshold)
    cut_points = merge_close_cuts(cut_points, MIN_SHOT_GAP_SEC)

    duration = float(duration_seconds) if isinstance(duration_seconds, (int, float)) else 0.0
    shots = build_shots(cut_points, duration)

    track = {
        "version": "0.1",
        "threshold": threshold,
        "status": "ready" if shots else "empty",
        "duration_sec": round(duration, 2),
        "cut_count": len(cut_points),
        "shot_count": len(shots),
        "cut_points_sec": [round(c, 2) for c in cut_points],
        "shots": shots,
    }
    write_json(role_dir / "shot_track.json", track)
    return track


def detect_scene_cuts(ffmpeg: str, video_path: Path, threshold: float) -> list[float]:
    """用 ffmpeg scene 滤镜检测镜头切点，返回切点时间（秒，升序）。"""
    command = [
        ffmpeg,
        "-hide_banner",
        "-i",
        str(video_path),
        "-filter:v",
        f"select='gt(scene,{threshold})',showinfo",
        "-f",
        "null",
        "-",
    ]
    completed = run_command(command)
    # showinfo 输出在 stderr。失败时返回空列表（上游按 empty 处理，不抛错）。
    text = completed.stderr or ""
    times = [float(m) for m in re.findall(r"pts_time:([0-9.]+)", text)]
    return sorted(set(times))


def merge_close_cuts(cut_points: list[float], min_gap: float) -> list[float]:
    """合并间隔过近的切点（转场抖动会连报几个相邻切点）。"""
    merged: list[float] = []
    for point in cut_points:
        if not merged or point - merged[-1] >= min_gap:
            merged.append(point)
    return merged


def build_shots(cut_points: list[float], duration: float) -> list[dict[str, Any]]:
    """把切点列表转成镜头段（每段含起止时间）。"""
    if duration <= 0:
        return []
    boundaries = [0.0] + [c for c in cut_points if 0.0 < c < duration] + [duration]
    shots: list[dict[str, Any]] = []
    for index in range(len(boundaries) - 1):
        start = boundaries[index]
        end = boundaries[index + 1]
        if end - start <= 0:
            continue
        shots.append(
            {
                "index": index + 1,
                "start_sec": round(start, 2),
                "end_sec": round(end, 2),
                "duration_sec": round(end - start, 2),
                "start": format_seconds(start),
                "end": format_seconds(end),
            }
        )
    return shots


def render_shot_track_markdown(track: dict[str, Any]) -> str:
    """把镜头轨渲染成给 omni 看的 markdown（喂进 analysis_input）。"""
    shots = track.get("shots") or []
    if not shots:
        return "（未检测到镜头切换，或镜头切分未启用）"
    lines = []
    for shot in shots:
        lines.append(
            f"- 镜头 {shot['index']}: {shot['start']} - {shot['end']}"
            f"（时长 {shot['duration_sec']}s）"
        )
    return "\n".join(lines)


def _empty_track(reason: str) -> dict[str, Any]:
    return {
        "version": "0.1",
        "status": "disabled",
        "disabled_reason": reason,
        "cut_count": 0,
        "shot_count": 0,
        "cut_points_sec": [],
        "shots": [],
    }
