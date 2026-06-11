"""flayr_core.postprocess.derive：severity 确定性推导（4d 架构落地）。

设计依据（2026-06-11，两轮门禁 + 离线校验 dev_derive_severity.py r1 14/18 / r2 16/18）：
模型直出 severity 不收敛（prompt 两轮校准 11/18→9/18），但事实层稳定。
分工改为：模型供事实（两侧独立执行分 + 品类画像 + 证据文本），代码定政策（权重表 + 推导）。

三条定稿原则（用户裁决）：
  ① E 由代码从稳定事实推导，模型不直出对比性差距分。
     单侧执行分标尺：0=不执行，0.5=敷衍，1=合格，2=好；E = 标杆执行分 − 达人执行分。
  ③ 品类痛点清单是数据（category_profile.painpoints 由模型按世界知识输出，命中由代码查表），
     权重政策（W 表）在代码。
  ④ 事实不支撑则不判断：双方执行分均为 0、或 S5 双方均无真背书 → "均未涉及"，不进公式。

架构不变量：推导失败（字段缺失/不合法/任何异常）必须优雅降级——保留模型 severity 和
既有 stabilize 护栏结果，把原因写进 severity_derivation.status，绝不抛错拖垮主分析流程。
每阶段附 severity_derivation 算法溯源（E/W/C/S/依据），满足可解释证据链要求。

权重初值来自离线校验（同批 18 标签拟合，属可行性初值非定稿）；
后续随对比数据 + 人工裁决积累，对存量 facts 零 LLM 成本离线重拟合。
"""

from __future__ import annotations

import re
from typing import Any

from .repair import has_real_endorsement

# ── 品类原型 → 阶段权重表 W（政策数据，待数据积累渐进拟合）──────────────────────
ARCHETYPE_W: dict[str, dict[str, float]] = {
    # 高决策门槛 + 功能理性（口服保健品等）：信任背书与效果验证是说服核心
    "high_decision_rational": {"S1": 1.5, "S2": 1.2, "S3": 1.0, "S4": 1.4, "S5": 1.6, "S6": 1.2},
    # 低客单价冲动品（日用快消）：CTA 是转化口（客单越低 CTA 权重越高），背书必要性低
    "impulse_low_price": {"S1": 1.5, "S2": 1.2, "S3": 1.2, "S4": 1.0, "S5": 0.6, "S6": 1.8},
    # 高决策门槛 + 情绪/感官驱动（儿童用品等决策人分离品类）：感官效果可视化权重最高
    "high_decision_sensory": {"S1": 1.5, "S2": 1.0, "S3": 1.0, "S4": 1.5, "S5": 1.4, "S6": 1.2},
}
# severity 映射阈值：S≤1.2 → small；S≤2.5 → medium；S>2.5 → large
TH_SMALL, TH_MEDIUM = 1.2, 2.5
_STAGE_RE = re.compile(r"(S[1-6])")
# S4 动作演示词：效果验证的功能定义是"让用户看到并信服"，标杆动作演示 vs 达人口头宣称 = 验证功能未达成
_DEMO_RE = re.compile(r"闻|嗅|按压|挤出|涂抹|擦拭|冲水|冲洗|冲净|脱落|掉入|掉进|排空|实测|对比|试用|测试|前后")


def _select_archetype(profile: dict[str, Any] | None) -> str | None:
    if not isinstance(profile, dict):
        return None
    if profile.get("decision_threshold") == "impulse":
        return "impulse_low_price"
    # 框架"客单越低 CTA 权重越高"：低客单+功能性日用品按冲动品原型处理——
    # round3 实测模型对马桶刷的 decision_threshold 在 considered/impulse 间摆（4:1），
    # 而 price_tier=low 稳定，政策锚定在稳的事实上。
    if profile.get("price_tier") == "low" and profile.get("drive_type") == "functional":
        return "impulse_low_price"
    if profile.get("drive_type") in {"emotional", "mixed"}:
        return "high_decision_sensory"
    return "high_decision_rational"


def _side_text(stage: dict[str, Any], side: str) -> str:
    keys = [f"{side}_summary", f"{side}_key_message", f"{side}_quote_zh", f"{side}_quote"]
    return " ".join(str(stage.get(k) or "") for k in keys)


def _painpoint_tokens(painpoints: list[str]) -> list[str]:
    """痛点词条分词：模型常输出 'kebersihan (卫生)' 复合串，整串匹配永不命中。

    按括号/分隔符拆成独立 token（马来语短语 + 中文词各自成条），过滤过短噪声。
    """
    tokens: list[str] = []
    for entry in painpoints:
        for part in re.split(r"[()（）/、,，;；|]", str(entry)):
            part = part.strip()
            if len(part) >= 2:
                tokens.append(part)
    return tokens


def _hits(text: str, words: list[str]) -> bool:
    lowered = text.lower()
    return any(w.lower() in lowered for w in words if w)


def _derive_one(stage_id: str, stage: dict[str, Any], weights: dict[str, float] | None,
                painpoints: list[str]) -> dict[str, Any]:
    """推导单阶段 severity。返回 severity_derivation 溯源 dict（status=derived 时含新 severity）。"""
    creator_exec = stage.get("creator_execution")
    bench_exec = stage.get("benchmark_execution")
    if creator_exec is None or bench_exec is None:
        return {"status": "skipped", "reason": "执行分缺失，保留模型 severity"}

    bench_text = _side_text(stage, "benchmark")
    creator_text = _side_text(stage, "creator")

    # 原则④：事实不支撑则不判断
    if creator_exec == 0 and bench_exec == 0:
        return {"status": "derived", "severity": "small", "E": 0,
                "reason": "双方均未涉及（执行分均为 0），不进公式"}
    if stage_id == "S5" and not has_real_endorsement(bench_text) and not has_real_endorsement(creator_text):
        return {"status": "derived", "severity": "small", "E": 0,
                "reason": "S5 双方均无真背书 → 均未涉及（卖点类信息归卖点链）"}

    e = max(0.0, float(bench_exec) - float(creator_exec))
    reason = f"E = 标杆执行分 {bench_exec} − 达人执行分 {creator_exec}"

    # 事实覆盖层（取 E 下限）：观察事实 > 打分漂移
    b_vis = " ".join(str(v) for v in stage.get("benchmark_visual_evidence") or [])
    c_vis = " ".join(str(v) for v in stage.get("creator_visual_evidence") or [])
    if stage_id == "S4" and e > 0 and _DEMO_RE.search(b_vis) and not _DEMO_RE.search(c_vis):
        e, reason = max(e, 2.0), reason + "；S4 标杆动作演示 vs 达人口头宣称（验证=让用户看到）"
    elif stage_id == "S1" and e > 0 and painpoints and _hits(bench_text, painpoints) and not _hits(creator_text, painpoints):
        e, reason = max(e, 2.0), reason + "；S1 标杆钩子命中品类痛点、达人未命中"

    # 极性红线：达人持平或更优 → small（达人优势记亮点，绝不是差距）
    if e <= 0:
        return {"status": "derived", "severity": "small", "E": 0,
                "reason": reason + "；达人持平或更优（亮点，零差距红线）"}

    w = (weights or {}).get(stage_id, 1.0)
    # 痛点命中系数：无品类画像时取中性 1.0，不放大也不衰减
    if painpoints:
        lever_text = f"{stage.get('gap_summary') or ''} {stage.get('gap') or ''} {bench_text}"
        c_factor = 1.2 if _hits(lever_text, painpoints) else 0.8
    else:
        c_factor = 1.0
    score = round(e * w * c_factor, 2)

    if e >= 2 and stage_id in {"S1", "S6"}:
        severity = "large"
        reason += "；S1/S6 核心功能缺失红线"
    elif score > TH_MEDIUM:
        severity = "large"
    elif score > TH_SMALL:
        severity = "medium"
    else:
        severity = "small"
    return {"status": "derived", "severity": severity, "E": e, "W": w, "C": c_factor,
            "S": score, "reason": reason}


def derive_severity_from_facts(result: dict[str, Any]) -> None:
    """4d 主入口：用执行分 + 品类权重表确定性推导各阶段 severity，覆盖模型直出值。

    每阶段把算法溯源写入 stage["severity_derivation"]（含被覆盖前的 model_severity）。
    任何异常优雅降级：跳过该阶段并记录原因，绝不中断主流程。
    """
    stages = result.get("stage_analysis")
    if not isinstance(stages, list):
        return
    profile = result.get("category_profile") if isinstance(result.get("category_profile"), dict) else None
    archetype = _select_archetype(profile)
    weights = ARCHETYPE_W.get(archetype) if archetype else None
    painpoints = _painpoint_tokens([str(p) for p in (profile or {}).get("painpoints") or [] if str(p).strip()])

    for stage in stages:
        if not isinstance(stage, dict):
            continue
        match = _STAGE_RE.match(str(stage.get("stage") or ""))
        if not match:
            continue
        try:
            trace = _derive_one(match.group(1), stage, weights, painpoints)
        except Exception as exc:  # 架构不变量：推导绝不拖垮主流程
            trace = {"status": "error", "reason": f"推导异常已降级：{exc}"}
        if archetype:
            trace.setdefault("archetype", archetype)
        if trace.get("status") == "derived":
            trace["model_severity"] = stage.get("severity")
            stage["severity"] = trace["severity"]
        stage["severity_derivation"] = trace
