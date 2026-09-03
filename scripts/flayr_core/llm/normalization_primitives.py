"""Small, schema-neutral normalization primitives used by LLM parsers.

These helpers deliberately preserve the parser's historical behavior.  The
``parse`` module re-exports them as its compatibility facade.
"""

from __future__ import annotations

from typing import Any


def normalize_evidence(value: Any, *, max_items: int | None = None) -> list[str]:
    """Normalize evidence-like lists without silently dropping model output.

    Evidence limits are contract decisions, not parser behavior.  Callers that
    have an explicit, validated limit may pass ``max_items``; the default keeps
    every non-empty item so an audit or downstream validator can see the full
    provider response instead of receiving a truncated, apparently valid list.
    """
    if isinstance(value, list):
        evidence = [str(item).strip() for item in value if str(item).strip()]
        if max_items is not None:
            if max_items < 0:
                raise ValueError("max_items must be non-negative")
            return evidence[:max_items]
        return evidence
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def normalize_demo_flag(value: Any) -> bool | None:
    """归一可空观察布尔；缺失或无法解析保持 None。

    ``None`` 不能被解释为 ``False``。调用方可以据此区分“明确没有”和
    “没有采集到/无法判断”，避免未知事实被下游当作否定事实。
    容忍模型吐 bool 或 true/false/yes/no/1/0 字符串。"""
    if isinstance(value, bool):  # 必须先于 int 判（Python 中 bool 是 int 子类）
        return value
    if isinstance(value, (int, float)):  # 模型偶吐数字 0/1 当布尔
        return True if value == 1 else False if value == 0 else None
    token = str(value or "").strip().lower()
    if token in {"true", "yes", "1"}:
        return True
    if token in {"false", "no", "0"}:
        return False
    return None


def normalize_choice(value: Any, allowed: set[str], fallback: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else fallback


__all__ = ("normalize_evidence", "normalize_demo_flag", "normalize_choice")
