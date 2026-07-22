"""S1-S6 的唯一阶段目录。

默认时间只用于占位报告和预处理阶段帧回退；LLM 输出的真实阶段边界始终优先。
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class StageDefinition:
    code: str
    label: str
    default_time_range: str
    core_question: str
    artifact_label: str
    fallback_start: float | None
    fallback_end: float | None

    @property
    def name(self) -> str:
        return f"{self.code} {self.label}"


DEFAULT_STAGES = (
    StageDefinition("S1", "Hook", "0~3s", "用户凭什么停下来", "Hook", 0.0, 3.0),
    StageDefinition("S2", "产品引出", "3~6s", "产品为什么现在出现", "Product intro", 3.0, 6.0),
    StageDefinition("S3", "使用过程", "6~15s", "用户能不能看懂怎么用", "Usage", 6.0, 15.0),
    StageDefinition("S4", "效果呈现", "15~23s", "用户能不能看见价值", "Result", 15.0, 23.0),
    StageDefinition("S5", "信任放大", "23~27s", "用户凭什么相信", "Trust", 23.0, 27.0),
    StageDefinition("S6", "CTA", "最后 3~5s", "用户为什么现在下单", "CTA", None, None),
)


def stage_tuples() -> list[tuple[str, str, str]]:
    """兼容现有 normalize/repair 代码所需的三元组视图。"""
    return [(stage.name, stage.default_time_range, stage.core_question) for stage in DEFAULT_STAGES]


def fallback_artifact_ranges(duration: float) -> list[tuple[str, str, float, float]]:
    """为预处理阶段帧提供回退窗口；不参与最终阶段判定。"""
    try:
        end = float(duration)
    except (TypeError, ValueError):
        return []
    if not math.isfinite(end) or end < 0:
        return []
    ranges: list[tuple[str, str, float, float]] = []
    for stage in DEFAULT_STAGES:
        if stage.code == "S6":
            start, stop = max(0.0, end - 5.0), end
        else:
            start = min(float(stage.fallback_start or 0.0), end)
            stop = min(float(stage.fallback_end or end), end)
        ranges.append((stage.name, stage.artifact_label, start, stop))
    return ranges
