"""flayr_core.shot_track：镜头切分预处理（ffmpeg 场景检测）。

为什么存在：omni 看连续帧能"感觉到"画面变化，但给不出精确的镜头切点时间戳
（它只会说"大概第几秒有个转场"）。镜头切点是带货视频的硬结构信号——
阶段切分、Phase C 回看、证据片段切片都需要切在真正的镜头边界上，
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

from .artifacts import format_seconds, parse_timestamp_seconds
from .resources import finite_nonnegative
from .utils import run_command, write_json


# 候选切点的最低分数门槛：低于这个分基本是画面微动/噪声，不可能是真镜头切换。
# 用它捞全部候选（含分数），再按目标密度自适应筛选，而不是用固定阈值一刀切。
CANDIDATE_FLOOR = 0.2
# 目标镜头密度：平均每个镜头约这么多秒。带货视频镜头通常 5-12 秒，取 8 居中。
# 自适应据此算"该保留几个切点"，从而对快剪/单镜头/不同时长都收敛到合理密度。
TARGET_SHOT_SECONDS = 8.0
# 镜头数硬上下限，防止极端视频算出离谱的目标数。
MIN_TARGET_SHOTS = 1
MAX_TARGET_SHOTS = 12
# 两个切点间隔小于这个秒数时视为同一次切换的抖动，合并掉。
MIN_SHOT_GAP_SEC = 0.4


def build_shot_track(
    role_dir: Path,
    video_path: Path,
    duration_seconds: Any,
) -> dict[str, Any]:
    """对单个视频做自适应镜头切分，产出 shot_track.json 并返回结果。

    自适应逻辑：先用低门槛 CANDIDATE_FLOOR 捞出所有候选切点（带分数），
    再按时长算出"目标镜头数"，取分数最高的若干切点。这样不依赖固定阈值，
    快剪视频自动多切、单镜头视频自动少切，对不同时长/风格都泛化。

    ffmpeg 不可用或视频缺失时返回 disabled 状态，由调用方决定是否跳过，
    不中断整条 pipeline（与 subtitle_track 的降级策略一致）。
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return _empty_track("ffmpeg_missing")
    if not video_path.is_file():
        return _empty_track("video_missing")

    try:
        duration = finite_nonnegative(duration_seconds, "shot track duration", maximum=24 * 60 * 60.0)
    except ValueError:
        return _empty_track("invalid_duration")

    candidates = detect_scene_candidates(ffmpeg, video_path)
    cut_points, target = select_adaptive_cuts(candidates, duration)
    cut_points = merge_close_cuts(cut_points, MIN_SHOT_GAP_SEC)
    shots = build_shots(cut_points, duration)

    track = {
        "version": "0.2",
        "method": "adaptive_density",
        "candidate_floor": CANDIDATE_FLOOR,
        "target_shot_seconds": TARGET_SHOT_SECONDS,
        "target_cut_count": target,
        "status": "ready" if shots else "empty",
        "duration_sec": round(duration, 2),
        "candidate_count": len(candidates),
        "cut_count": len(cut_points),
        "shot_count": len(shots),
        "cut_points_sec": [round(c, 2) for c in cut_points],
        "shots": shots,
    }
    write_json(role_dir / "shot_track.json", track)
    return track


def detect_scene_candidates(ffmpeg: str, video_path: Path) -> list[tuple[float, float]]:
    """用低门槛捞所有候选切点，返回 [(时间, 分数)]，按时间升序。

    metadata=print 把每个超过门槛的帧的 scene_score 和时间一起打到 stderr，
    供自适应筛选用分数排序。
    """
    command = [
        ffmpeg,
        "-hide_banner",
        "-i",
        str(video_path),
        "-filter:v",
        f"select='gt(scene,{CANDIDATE_FLOOR})',metadata=print",
        "-f",
        "null",
        "-",
    ]
    completed = run_command(command)
    text = completed.stderr or ""
    # metadata=print 输出形如：
    #   frame:.. pts_time:16.9667
    #   lavfi.scene_score=0.431470
    # 两行配对，按出现顺序把 time 和紧随的 score 组合起来。
    candidates: list[tuple[float, float]] = []
    pending_time: float | None = None
    for time_str, score_str in re.findall(r"pts_time:([0-9.]+)|scene_score=([0-9.]+)", text):
        if time_str:
            pending_time = parse_timestamp_seconds(time_str)
        elif score_str and pending_time is not None:
            score = parse_timestamp_seconds(score_str)
            if score is None:
                pending_time = None
                continue
            candidates.append((pending_time, score))
            pending_time = None
    candidates.sort(key=lambda item: item[0])
    return candidates


def select_adaptive_cuts(
    candidates: list[tuple[float, float]],
    duration: float,
) -> tuple[list[float], int]:
    """按目标镜头密度从候选里挑分数最高的切点。返回 (切点时间列表, 目标切点数)。

    目标镜头数 = 时长 / TARGET_SHOT_SECONDS（夹在上下限内）；
    目标切点数 = 目标镜头数 - 1。取分数最高的前若干个，再按时间排序。
    """
    if not candidates or duration <= 0:
        return [], 0
    target_shots = round(duration / TARGET_SHOT_SECONDS)
    target_shots = max(MIN_TARGET_SHOTS, min(MAX_TARGET_SHOTS, target_shots))
    target_cuts = max(0, target_shots - 1)
    if target_cuts == 0:
        return [], 0
    # 按分数降序取前 target_cuts 个；候选不足则全要。
    top = sorted(candidates, key=lambda item: item[1], reverse=True)[:target_cuts]
    times = sorted(time for time, _ in top)
    return times, target_cuts


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
