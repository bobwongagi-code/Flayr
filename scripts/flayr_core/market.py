"""目标市场代码与市场知识路由。"""

from __future__ import annotations

import re
from pathlib import Path

from .utils import read_optional_text


ROOT = Path(__file__).resolve().parents[2]
SEA_MARKET_CODES = frozenset({"bn", "id", "kh", "la", "mm", "my", "ph", "sg", "th", "tl", "vn"})
_MARKET_CODE_RE = re.compile(r"^[a-z]{2}$")


def normalize_target_market(value: str) -> str:
    """接受 Flayr 覆盖范围内的东南亚市场代码，拒绝把未知市场误按 SEA 处理。"""
    market = str(value or "auto").strip().lower()
    if market in {"auto", "sea"} or market in SEA_MARKET_CODES:
        return market
    if _MARKET_CODE_RE.fullmatch(market):
        raise ValueError(f"暂不支持市场代码 {market}；当前仅支持 SEA 市场或 auto/sea")
    raise ValueError("目标市场必须是 auto、sea 或两位东南亚市场代码")


def _sea_common_knowledge() -> str:
    """从马来知识文件中取可跨东南亚复用的第一层，严禁带出马来专属层。"""
    text = read_optional_text(ROOT / "references" / "market-knowledge-my.md")
    if not text:
        return "（未找到市场知识库；仅按视频事实判断。）"
    marker = "## 第二层：马来西亚专属知识"
    common, _, _ = text.partition(marker)
    return common.strip()


def render_market_knowledge(target_market: str) -> str:
    """按市场返回可注入模型的知识；只有 my 可见马来西亚专属层。"""
    market = normalize_target_market(target_market)
    full_text = read_optional_text(ROOT / "references" / "market-knowledge-my.md")
    if market == "my":
        return (
            "目标市场已指定为马来西亚（my）。可使用东南亚共性层和马来西亚专属层，"
            "但不得把文化知识当作视频事实。\n\n"
            + (full_text or "（未找到马来西亚市场知识库；仅按视频事实判断。）")
        )
    label = "东南亚泛化（sea）" if market == "sea" else ("未确认（auto）" if market == "auto" else f"{market}（东南亚市场）")
    return (
        f"目标市场为 {label}。以下只提供东南亚共性知识；"
        "马来西亚专属规则不得用于该市场，也不得以文化常识替代视频事实。\n\n"
        + _sea_common_knowledge()
    )
