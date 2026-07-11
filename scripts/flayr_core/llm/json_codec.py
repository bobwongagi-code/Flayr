"""LLM JSON 文本的容错解析。

这个模块只处理传输层文本，不知道分析结果 schema 或任何业务规则。
"""

from __future__ import annotations

import json
import re
from typing import Any


def parse_json_text(text: str) -> dict[str, Any]:
    """解析 LLM 返回的 JSON 文本，必要时做轻度修复。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = remove_trailing_commas(cleaned)
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        repaired = remove_trailing_commas(escape_unquoted_string_quotes(cleaned))
        try:
            result = json.loads(repaired)
        except json.JSONDecodeError:
            raise SystemExit(f"LLM output is not valid JSON: {exc}") from exc
    if not isinstance(result, dict):
        raise SystemExit("LLM output JSON must be an object.")
    return result


def remove_trailing_commas(text: str) -> str:
    return re.sub(r",(\s*[}\]])", r"\1", text)


def escape_unquoted_string_quotes(text: str) -> str:
    """转义 LLM 常误产生的字符串内部未转义引号。"""
    repaired: list[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if not in_string:
            repaired.append(char)
            if char == '"':
                in_string = True
            continue
        if escaped:
            repaired.append(char)
            escaped = False
            continue
        if char == "\\":
            repaired.append(char)
            escaped = True
            continue
        if char != '"':
            repaired.append(char)
            continue

        remainder = text[index + 1 :]
        next_nonspace = next((item for item in remainder if not item.isspace()), "")
        if next_nonspace in {":", ",", "}", "]"}:
            repaired.append(char)
            in_string = False
        else:
            repaired.append('\\"')
    return "".join(repaired)
