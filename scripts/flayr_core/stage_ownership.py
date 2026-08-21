"""S1-S6 阶段归属的共享规则。"""

from __future__ import annotations

import re
from typing import Any


CERTIFICATION_OWNER_STAGE = "S5"
_CERTIFICATION_TERMS = r"(?:KKM|KKMA|认证|证书|kelulusan|halal|sirim|sijil|certificate(?:s)?)"
CERTIFICATION_PATTERN = re.compile(_CERTIFICATION_TERMS, flags=re.IGNORECASE)
# Stage1 facts deliberately record negative observations such as
# "无认证/证书等视觉背书".  A lexical ownership check must not turn those
# absence facts into positive certification claims.  Keep this bounded to the
# same clause so a later positive claim in the sentence remains detectable.
_NEGATED_CERTIFICATION_PATTERN = re.compile(
    r"(?:无|沒有|没有|没|缺乏|缺少|未(?:见|有|出现|发现|显示|提供|验证)?|"
    r"不具备|不含|tanpa|tiada|tidak\s*ada|bukan|without|\bno\b)"
    r"\s*(?:[^。；;.!?，,\n]{0,20}?)"
    + _CERTIFICATION_TERMS
    + r"(?:[^。；;.!?，,\n]{0,12}?" + _CERTIFICATION_TERMS + r")*",
    flags=re.IGNORECASE,
)
_POSTFIX_NEGATED_CERTIFICATION_PATTERN = re.compile(
    _CERTIFICATION_TERMS
    + r"\s*(?:未出现|不存在|未见|未显示|未验证|没有|没|无|缺少|"
    r"not\s+(?:shown|present|seen)|absent)",
    flags=re.IGNORECASE,
)
# A Stage1 observation may mention a badge or icon while explicitly saying it
# is too blurry to identify. That is an unresolved visual candidate, not a
# positive certification claim. Keep the match bounded to the same clause so
# a later, clearly identified certification remains visible to the ownership
# check.
_UNCERTAIN_CERTIFICATION_PATTERN = re.compile(
    r"(?:"
    + _CERTIFICATION_TERMS
    + r"(?:[^。；;.!?，,\n]{0,24}?)(?:模糊|无法辨识|无法识别|不可辨识|看不清|不清晰|"
    r"难以辨识|无法确认|不确定|疑似|可能是|unreadable|unclear|indistinct|"
    r"not\s+identifiable|cannot\s+identify)"
    r"(?:[^。；;.!?，,\n]{0,48})(?:"
    + _CERTIFICATION_TERMS
    + r")?"
    r"|(?:模糊|无法辨识|无法识别|不可辨识|看不清|不清晰|难以辨识|无法确认|不确定|"
    r"疑似|可能是|unreadable|unclear|indistinct|not\s+identifiable|cannot\s+identify)"
    r"(?:[^。；;.!?，,\n]{0,48}?)"
    + _CERTIFICATION_TERMS
    + r")",
    flags=re.IGNORECASE,
)
CERTIFICATION_OWNERSHIP_PROMPT = (
    "第三方认证/审批/权威机构背书（如 KKM、Halal、SIRIM、检测报告）按功能唯一归入 S5 信任放大，"
    "不归 S1 Hook 或 S2 产品引出；即使它与产品介绍同画面或出现在开头，也不得重复归因。"
    "S2 只能说明产品身份、角色或解决方案承接，不能把认证当作 S2 证据。"
    "只有机构的数据、实验、研究、证书或官方标识实际证明本产品价值时才算背书；"
    "仅提到机构名字、合作 logo 或自述功效不算背书。口播提及但画面未显示时，必须标明口播声称、画面未验证。"
)
CERTIFICATION_POSITION_EXCEPTION_PROMPT = (
    "开头评论/粉丝提问等社会认同若以留人为主归 S1；结尾保障或承诺归 S6。"
)

_LEGACY_POSITION_RULE = "位置优先——视频开头的此类背书内容算 S1 钩子（留人）、结尾算 S6 CTA，不要按语义把开头/结尾的背书塞进 S5；"
_LEGACY_OPENING_RULE = "开头的背书/认证类内容按钩子算（见 14b1 位置宪法）；"


def apply_certification_ownership_policy(text: str) -> str:
    """替换旧长 prompt 中遗留的认证位置优先说法，保证实际发给模型的规则唯一。"""
    return str(text).replace(
        _LEGACY_POSITION_RULE,
        CERTIFICATION_OWNERSHIP_PROMPT + CERTIFICATION_POSITION_EXCEPTION_PROMPT,
    ).replace(_LEGACY_OPENING_RULE, CERTIFICATION_POSITION_EXCEPTION_PROMPT)


def contains_certification(value: Any) -> bool:
    """判断文本或结构化值是否包含第三方认证主张。"""
    text = str(value or "")
    text = _NEGATED_CERTIFICATION_PATTERN.sub("", text)
    text = _POSTFIX_NEGATED_CERTIFICATION_PATTERN.sub("", text)
    text = _UNCERTAIN_CERTIFICATION_PATTERN.sub("", text)
    return bool(CERTIFICATION_PATTERN.search(text))


def is_certification_owner_stage(stage: Any) -> bool:
    """认证主张只能由 S5 信任放大承载。"""
    return str(stage or "").strip().upper().startswith(CERTIFICATION_OWNER_STAGE)
