"""flayr_core.voice_sample：从达人视频自动挑选最干净的口播样本。

为什么存在：音色克隆的质量命脉是注册样本。直接喂全片音频（含停顿、
情绪起伏、底噪）克隆出的音色"平均化"、不像本人。这里用 transcript.srt
的口播分段选出一段最长、最连续的口播窗口（目标 10-20s），只拿这段去注册音色。

判据只看"音频是否连续口播"，不看镜头转场——同一个人说话时镜头切换
不会改变音色，转场不等于音频变脏。

零新依赖：只读已有的 transcript.srt，用 ffmpeg 切片。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from .utils import run_command


# 音色克隆的理想样本时长窗口（秒）。太短音色信息不足，太长易混入杂音/情绪波动。
MIN_SAMPLE_SEC = 10.0
MAX_SAMPLE_SEC = 20.0
# 相邻口播句的间隔超过这个秒数，视为"断开"，不算连续说话。
MAX_GAP_SEC = 1.0


def parse_srt_segments(srt_path: Path) -> list[tuple[float, float, str]]:
    """解析 srt，返回 [(start_sec, end_sec, text)]。"""
    if not srt_path.is_file():
        return []
    text = srt_path.read_text(encoding="utf-8", errors="ignore")
    segments: list[tuple[float, float, str]] = []
    pattern = re.compile(
        r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
    )
    blocks = re.split(r"\n\s*\n", text.strip())
    for block in blocks:
        m = pattern.search(block)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        # 文本是时间行之后的内容
        lines = block.splitlines()
        spoken = " ".join(line.strip() for line in lines if "-->" not in line and not line.strip().isdigit())
        segments.append((start, end, spoken.strip()))
    return segments


def find_continuous_windows(
    segments: list[tuple[float, float, str]],
    max_gap: float = MAX_GAP_SEC,
) -> list[tuple[float, float]]:
    """把口播分段聚成"连续说话窗口"：句间隔 ≤ max_gap 的合并成一段。"""
    if not segments:
        return []
    windows: list[tuple[float, float]] = []
    cur_start, cur_end, _ = segments[0]
    for start, end, _ in segments[1:]:
        if start - cur_end <= max_gap:
            cur_end = end
        else:
            windows.append((cur_start, cur_end))
            cur_start, cur_end = start, end
    windows.append((cur_start, cur_end))
    return windows


def select_voice_sample_window(
    srt_path: Path,
    min_sec: float = MIN_SAMPLE_SEC,
    max_sec: float = MAX_SAMPLE_SEC,
) -> dict[str, Any]:
    """选出最佳音色样本窗口：最长、最连续的口播段，截到 [min,max] 区间。

    返回 {status, start_sec, end_sec, duration_sec, reason}；无 srt 时 status=no_srt。
    """
    segments = parse_srt_segments(srt_path)
    if not segments:
        return {"status": "no_srt", "start_sec": None, "end_sec": None}

    windows = find_continuous_windows(segments)
    # 取最长的连续口播窗口
    windows.sort(key=lambda w: w[1] - w[0], reverse=True)
    start, end = windows[0]
    dur = end - start
    # 太长则从头截 max_sec（开头语气通常最自然，未进入快节奏推销段）
    if dur > max_sec:
        end = start + max_sec
        dur = max_sec

    reason = f"最长连续口播段 {dur:.1f}s"
    if dur < min_sec:
        reason += f"，短于理想 {min_sec:.0f}s，音色信息可能不足"

    return {
        "status": "ready" if dur >= min_sec else "short_sample",
        "start_sec": round(start, 2),
        "end_sec": round(end, 2),
        "duration_sec": round(dur, 2),
        "reason": reason,
    }


def cut_voice_sample(
    audio_path: Path,
    window: dict[str, Any],
    output_path: Path,
) -> bool:
    """按窗口从 audio.wav 切出样本（mp3，给注册音色用）。成功返回 True。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not audio_path.is_file():
        return False
    start = window.get("start_sec")
    dur = window.get("duration_sec")
    if start is None or not dur:
        return False
    command = [
        ffmpeg, "-y",
        "-ss", f"{float(start):.2f}",
        "-t", f"{float(dur):.2f}",
        "-i", str(audio_path),
        "-acodec", "libmp3lame", "-b:a", "128k",  # 128k：音色克隆要保真度
        str(output_path),
    ]
    completed = run_command(command)
    return completed.returncode == 0 and output_path.is_file()
