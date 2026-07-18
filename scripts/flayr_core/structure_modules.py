"""structure_library_full.md 的官方模块编号读取。"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def official_module_ids() -> set[str]:
    """从结构库标题提取唯一允许的 S1-S6 模块编号。"""
    path = ROOT / "structure_library_full.md"
    if not path.is_file():
        return set()
    text = path.read_text(encoding="utf-8", errors="ignore")
    return set(re.findall(r"^###\s+(S[1-6]-[A-Z])[:：]", text, flags=re.M))


def canonical_module_id(value: object, stage_index: int) -> str:
    """保留结构库中的本阶段模块，模型自造或错阶段编号统一为 unknown。"""
    candidate = str(value or "").strip().upper()
    if candidate == "UNKNOWN":
        return "unknown"
    if candidate in official_module_ids() and candidate.startswith(f"S{stage_index}-"):
        return candidate
    return "unknown"


def stage1_event_catalog() -> list[dict[str, Any]]:
    """Stage1 逐项核对的高价值可观察事件目录。"""
    return [
        {"id": "S3-A", "stage": "S3", "event": "单场景真实使用流程", "signals": "产品实际作用于目标对象，关键动作与状态变化连续可见", "priority": "high"},
        {"id": "S3-B", "stage": "S3", "event": "多场景使用覆盖", "signals": "多个真实使用场景服务不同卖点，切换有明确逻辑", "priority": "medium"},
        {"id": "S3-C", "stage": "S3", "event": "多人角色使用", "signals": "主导者与辅助者角色清楚，并有服务卖点的互动", "priority": "medium"},
        {"id": "S3-D", "stage": "S3", "event": "步骤拆解呈现", "signals": "使用步骤被清楚拆开，用户可跟随复现", "priority": "medium"},
        {"id": "S3-E", "stage": "S3", "event": "沉浸或感官使用", "signals": "第一视角、ASMR 或细节声画让实际使用可感知", "priority": "medium"},
        {"id": "S4-A", "stage": "S4", "event": "同对象前后状态对比", "signals": "使用前后在同一对象、近似构图或分屏中可见", "priority": "high"},
        {"id": "S4-B", "stage": "S4", "event": "局部细节特写对比", "signals": "同一细节区域的微距或近景变化可见", "priority": "high"},
        {"id": "S4-C", "stage": "S4", "event": "人对人效果对比", "signals": "使用者与未使用者的可比视觉差异可见", "priority": "medium"},
        {"id": "S4-D", "stage": "S4", "event": "本品与替代方案对比", "signals": "本品和传统/替代方案的过程或结果形成对照", "priority": "high"},
        {"id": "S4-E", "stage": "S4", "event": "日常参照物量化", "signals": "纸巾、水珠、硬币等参照物使效果可理解", "priority": "high"},
        {"id": "S4-F", "stage": "S4", "event": "过程可视化", "signals": "特写、慢镜或剖面使原本不明显的作用过程可见", "priority": "high"},
    ]
