"""flayr_core.motion：画面晃动确定性信号（零 LLM 成本，本地 ffmpeg）。

背景（2026-06-12 round5 实证）：糊弄视频（画面晃动到观众抓不住重点）模型执行分照样给 2，
prompt 锚点治不住——晃动是帧间运动事实，该由代码确定性计算，不该指望模型看出来
（与 product_visibility 同款思路：模型供观察，确定性统计归代码）。

指标：ffmpeg vmafmotion（320px 降采样 + 6fps），全片平均运动分。
经验校准（2026-06-12，12 条视频）：糊弄组 22.9/26.4 vs 正常组全部 ≤19.0（含快剪标杆），
空档清晰；阈值 22.0 取空档内偏保守值。属数据校准初值，随样本积累复核。

消费方：postprocess/derive.py——severe 晃动侧在视觉依赖阶段（S1-S4）执行分封顶 0.5
（用户判例：晃动=无法有效接收，观众可看性视角非镜头美学）。
架构不变量：任何失败返回 status 说明，绝不抛错拖垮预处理。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# severe 阈值：见模块头注的经验校准记录
SEVERE_SHAKE_THRESHOLD = 22.0
_MOTION_RE = re.compile(r"VMAF Motion avg:\s*([0-9.]+)")


def compute_shake_metric(video_path: Path | str) -> dict:
    """全片晃动指标。成功返回 {vmafmotion_avg, level, method}；失败返回 {status, note}。"""
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-i", str(video_path),
                "-vf", "scale=320:-2,fps=6,vmafmotion",
                "-an", "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        match = _MOTION_RE.search(proc.stderr or "")
        if not match:
            return {"status": "unavailable", "note": "ffmpeg 无 vmafmotion 输出，晃动信号跳过"}
        avg = float(match.group(1))
        return {
            "vmafmotion_avg": avg,
            "level": "severe" if avg >= SEVERE_SHAKE_THRESHOLD else "normal",
            "method": "vmafmotion@320px6fps",
        }
    except Exception as exc:  # 架构不变量：可选信号失败不拖垮主流程
        return {"status": "failed", "note": f"晃动指标计算失败已跳过：{exc}"}
