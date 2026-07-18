"""S1 Landing shadow 的共享合同。

本模块只保存字段、枚举与 Prompt 合同；不解析模型输出、不参与 severity 推导。
主分析、Phase C、Repair 和独立验证必须引用同一份文本，避免判据漂移。
"""

from __future__ import annotations


LANDING_SHADOW_CONDITIONS = (
    "immediately_understandable",
    "singular_and_concrete",
    "creates_stay_motivation",
    "effectively_received",
)

LANDING_MOTIVATION_MECHANISMS = {
    "pain", "desire", "result", "contrast", "curiosity",
    "identity", "scene", "other", "none", "unknown",
}

LANDING_SHADOW_CONDITION_DESCRIPTIONS = {
    "immediately_understandable": "冷启动用户无需品牌、SKU 或达人前史即可立即理解开头在讲什么",
    "singular_and_concrete": "只有一个连贯具体焦点；问题→方案、悬念→揭晓、Before→After 等强因果双段算一个焦点，平行卖点罗列不算",
    "creates_stay_motivation": "形成对冷启动受众有具体利害或可感收益的痛点、欲望、结果、反差、好奇、身份或场景动力；清楚可见且品类相关的结果、便利或感官收益本身可以成立，不强制再补负面痛点、紧迫感或悬念；仅听懂品类、只问两个 SKU 有什么不同、泛泛称赞、纯操作运动画面或只喊某类人群都不算",
    "effectively_received": "关键信息经画面、口播、字幕或声音至少一个主要信道清楚可接收，无需细看或脑补",
}

LANDING_SHADOW_PROMPT_CONTRACT = (
    '"landing_conditions": {"immediately_understandable": bool, "singular_and_concrete": bool, '
    '"creates_stay_motivation": bool, "effectively_received": bool}（独立 shadow，暂不进入 severity。'
    'immediately_understandable=冷启动用户无需品牌/SKU/达人前史即可理解开头；'
    'singular_and_concrete=一个连贯具体焦点，允许问题→方案、悬念→结果、Before→After 等强因果双段，'
    '不允许多个平行卖点/SKU/情绪同时争夺主信息；'
    'creates_stay_motivation=对冷启动受众形成有具体利害或可感收益的痛点、欲望、结果、反差、好奇、身份或场景动力；'
    '清楚可见且品类相关的结果、便利或感官收益本身可以成立，不强制再补负面痛点、紧迫感或悬念；'
    '仅听懂品类、只问两个 SKU 有什么不同、泛泛称赞、纯操作运动画面或只喊某类人群都不算；'
    'effectively_received=关键信息通过画面/口播/字幕/声音至少一个主要信道清楚可接收，无需细看或脑补。'
    '四项必须分别按 0 到 hook_boundary_seconds 内证据判断，不得由 landing_met 反填，不得用 S2/S3 后段补足）, '
    '"stay_motivation_mechanism": "pain|desire|result|contrast|curiosity|identity|scene|other|none|unknown", '
    '"landing_shadow_reason": "只引用 0 到 hook_boundary_seconds 内的时间戳证据，逐项解释四个条件；不输出 shadow 总布尔值", '
)

LANDING_SHADOW_REVIEW_RULES = (
    "landing_conditions 必须按统一 shadow 合同独立重判：冷启动即时理解、一个连贯具体焦点、"
    "对冷启动受众形成具体利害或可感收益的停留动力、主要信道有效接收，四项缺一不可。"
    "清楚可见且品类相关的结果、便利或感官收益本身可以成立，不强制再补负面痛点、紧迫感或悬念；"
    "仅听懂品类、SKU 差异提问、泛泛称赞、纯操作运动画面或只喊某类人群不自动形成停留动力；"
    "强因果双段算一个焦点。只引用 0 到 hook_boundary_seconds 内证据，不得用 S2/S3 补足，"
    "不得从 landing_met 反填；同时输出 stay_motivation_mechanism 和逐项 landing_shadow_reason。"
    "shadow 暂不进入 severity。"
)
