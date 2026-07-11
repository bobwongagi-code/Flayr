"""S1-S6 阶段归属的共享规则。"""

from __future__ import annotations

import re
from typing import Any


CERTIFICATION_OWNER_STAGE = "S5"
CERTIFICATION_PATTERN = re.compile(r"KKM|KKMA|认证|kelulusan|halal|sirim", flags=re.IGNORECASE)
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
    return bool(CERTIFICATION_PATTERN.search(str(value or "")))


def is_certification_owner_stage(stage: Any) -> bool:
    """认证主张只能由 S5 信任放大承载。"""
    return str(stage or "").strip().upper().startswith(CERTIFICATION_OWNER_STAGE)
