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
from collections import Counter
from typing import Any

from .repair import has_real_endorsement

# ── 品类原型 → 阶段权重表 W（政策数据，待数据积累渐进拟合）──────────────────────
ARCHETYPE_W: dict[str, dict[str, float]] = {
    # 高决策门槛 + 功能理性（口服保健品/护肤等）：信任背书与效果验证是说服核心
    # 2026-06-12 任务5 首次重拟合（60 标签，dev_refit_weights.py，S1 锁框架红线不进搜索）：
    # 理性 S3 1.0→1.2、感官 S2/S3 1.0→1.6——中段呈现权重整体上调，36/60→40/60 severe 7→5
    # 2026-06-12 晃动信号上线后二次拟合：理性 S2/S3 1.2→1.4（45/60 severe 2）
    # 2026-06-13 repeat5 稳定口径三次拟合：理性 S3 1.4→1.6（众数语料 43→44/60 severe 2）
    "high_decision_rational": {"S1": 1.5, "S2": 1.4, "S3": 1.6, "S4": 1.4, "S5": 1.6, "S6": 1.2},
    # 低客单价冲动品（日用快消）：CTA 是转化口（客单越低 CTA 权重越高），背书必要性低
    "impulse_low_price": {"S1": 1.5, "S2": 1.2, "S3": 1.2, "S4": 1.0, "S5": 0.6, "S6": 1.8},
    # 高决策门槛 + 情绪/感官驱动（儿童用品等决策人分离品类）：感官效果可视化权重最高
    "high_decision_sensory": {"S1": 1.5, "S2": 1.6, "S3": 1.6, "S4": 1.5, "S5": 1.4, "S6": 1.2},
}
# severity 映射阈值：S≤1.2 → small；S≤2.5 → medium；S>2.5 → large
TH_SMALL, TH_MEDIUM = 1.2, 2.5
_STAGE_RE = re.compile(r"(S[1-6])")
# S4 动作演示词：效果验证的功能定义是"让用户看到并信服"，标杆动作演示 vs 达人口头宣称 = 验证功能未达成
_DEMO_RE = re.compile(r"闻|嗅|按压|挤出|涂抹|擦拭|冲水|冲洗|冲净|脱落|掉入|掉进|排空|实测|对比|试用|测试|前后")


def pool_creator_executions(results: list[dict[str, Any]]) -> None:
    """同达人多对：达人侧执行分按阶段跨对池化（众数，平手取较高），覆盖锚定漂移。

    锚定效应（round6 实证 carslan/colorkey + round4 youkoubo-c2）：模型打达人侧执行分时
    被标杆内容干扰，同一达人视频在不同对比对里得分漂移，违反独立打分纪律。达人侧打分
    本与标杆无关，跨对池化既复原独立性又降方差。平手取较高：锚定倾向把达人压低（显得更差），
    高值更接近真值。仅在同一达人视频出现于多个对比对时有意义（"一达人 vs 多标杆"批量场景）；
    须在 derive_severity_from_facts 之前调用。调用方负责按达人视频分组传入同组 results。
    """
    by_stage: dict[str, list[float]] = {}
    for res in results:
        for s in res.get("stage_analysis", []):
            match = _STAGE_RE.match(str(s.get("stage") or ""))
            ce = s.get("creator_execution")
            if match and ce is not None:
                by_stage.setdefault(match.group(1), []).append(ce)
    pooled = {}
    for sid, vals in by_stage.items():
        counts = Counter(vals)
        top_freq = counts.most_common(1)[0][1]
        pooled[sid] = max(v for v in vals if counts[v] == top_freq)
    for res in results:
        for s in res.get("stage_analysis", []):
            match = _STAGE_RE.match(str(s.get("stage") or ""))
            if match and match.group(1) in pooled:
                s["creator_execution"] = pooled[match.group(1)]


def _reconcile_operator_tier(profile: dict[str, Any] | None, analysis: dict[str, Any] | None) -> None:
    """运营档位优先（降级链）：运营给的 price_tier 覆盖模型世界知识判断。

    price_tier 需要的是"该品牌型号的实际市场价位"——视频通常不报价、模型对本地品牌
    价位无谱，运营（领域专家）最可靠。降级链：运营档位 > 模型判断（model_fallback）。
    触发器（2026-06-13）：impulse+high 时 impulse_low_price 原型（背书权重 0.6）可能不适用，
    告警人工复议——这是 price_tier 在当前架构唯一的非冗余价值点。
    """
    if not isinstance(profile, dict):
        return
    op = (analysis or {}).get("product") or {} if isinstance(analysis, dict) else {}
    op_tier = str(op.get("tier") or "").strip().lower()
    if op_tier in {"low", "mid", "high"}:
        profile["price_tier"] = op_tier
        profile["price_tier_source"] = "operator"
        price = op.get("price")
        if price and str(price) != "未填写":
            profile["price"] = str(price)
    if profile.get("decision_threshold") == "impulse" and profile.get("price_tier") == "high":
        profile["archetype_warning"] = (
            "impulse+high：impulse_low_price 原型(背书权重 0.6)可能不适用，建议人工复议"
        )


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
            # 拉丁词须 ≥2 字符防噪声；单个汉字是合法痛点词（脏/痛/香），不过滤（code review #6）
            if len(part) >= 2 or (len(part) == 1 and "一" <= part <= "鿿"):
                tokens.append(part)
    return tokens


def _hits(text: str, words: list[str]) -> bool:
    lowered = text.lower()
    return any(w.lower() in lowered for w in words if w)


# 晃动封顶作用域：视觉依赖阶段（S5 背书看视觉但有自己的门槛；S6 促单主要靠口播指令）
_SHAKE_CAPPED_STAGES = {"S1", "S2", "S3", "S4"}

# S4 赋分降维查表（2026-06-13）：单侧 S4 执行分 = TABLE[IU档][proof_strength]。
# IU档 = 视觉证据点计数（has_side_by_side/macro/instrument/process 求和）；初值待拟合。
# 把"S4 做得好不好"（模型凭感觉、两极）降维成"清点几个视觉证据 + 效果强度"（可数、稳）。
_S4_SCORE_TABLE: dict[int, dict[str, float]] = {
    0: {"weak": 0.0, "moderate": 0.0, "strong": 0.5},   # 无视觉证据（纯口播/静态结果图）
    1: {"weak": 0.5, "moderate": 1.0, "strong": 1.0},   # 单一证据
    2: {"weak": 1.0, "moderate": 1.0, "strong": 2.0},   # 基础对比+过程
    3: {"weak": 1.0, "moderate": 2.0, "strong": 2.0},   # 丰富证据链（3-4 合并）
}
_S4_IU_KEYS = ("has_side_by_side", "has_macro_detail", "has_instrument_proof", "has_process_reveal")


def _derive_s4_execution(evidence: dict[str, Any] | None) -> float | None:
    """从单侧 S4 视觉证据清点查表得执行分；evidence 缺失返回 None（调用方退回模型直接给）。"""
    if not isinstance(evidence, dict):
        return None
    iu_sum = sum(1 for k in _S4_IU_KEYS if evidence.get(k))
    proof = str(evidence.get("proof_strength") or "moderate")
    return _S4_SCORE_TABLE[min(iu_sum, 3)].get(proof, _S4_SCORE_TABLE[min(iu_sum, 3)]["moderate"])


def _derive_one(stage_id: str, stage: dict[str, Any], weights: dict[str, float] | None,
                painpoints: list[str], shake: dict[str, bool] | None = None) -> dict[str, Any]:
    """推导单阶段 severity。返回 severity_derivation 溯源 dict（status=derived 时含新 severity）。"""
    creator_exec = stage.get("creator_execution")
    bench_exec = stage.get("benchmark_execution")
    s4_notes = []
    # S4 赋分降维：有视觉证据清点则查表覆盖模型直接给的执行分（治"做了展示就打 2"两极病）
    if stage_id == "S4":
        c_s4 = _derive_s4_execution(stage.get("creator_s4_evidence"))
        b_s4 = _derive_s4_execution(stage.get("benchmark_s4_evidence"))
        if c_s4 is not None:
            creator_exec = c_s4
            s4_notes.append(f"达人 S4 证据清点→执行分 {c_s4}")
        if b_s4 is not None:
            bench_exec = b_s4
            s4_notes.append(f"标杆 S4 证据清点→执行分 {b_s4}")
    if creator_exec is None or bench_exec is None:
        return {"status": "skipped", "reason": "执行分缺失，保留模型 severity"}

    # 晃动确定性封顶（2026-06-12 用户判例：晃动=无法有效接收）：severe 侧在视觉依赖阶段
    # 执行分封顶 0.5，只降不升、双侧对称。指标由 flayr_core.motion 在预处理算出（零 LLM）。
    shake_notes = []
    if shake and stage_id in _SHAKE_CAPPED_STAGES:
        if shake.get("creator") and float(creator_exec) > 0.5:
            creator_exec = 0.5
            shake_notes.append("达人侧晃动实测 severe→执行分封顶 0.5")
        if shake.get("benchmark") and float(bench_exec) > 0.5:
            bench_exec = 0.5
            shake_notes.append("标杆侧晃动实测 severe→执行分封顶 0.5")

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
    if s4_notes:
        reason += "；" + "；".join(s4_notes)
    if shake_notes:
        reason += "；" + "；".join(shake_notes)

    # 痛点命中：优先用模型事实枚举（round3 实证词法匹配跨语言/跨粒度不可靠），缺失退回词法兜底
    relevance = stage.get("painpoint_relevance")
    if relevance is None and painpoints:
        lever_text = f"{stage.get('gap_summary') or ''} {stage.get('gap') or ''} {bench_text}"
        if _hits(lever_text, painpoints):
            relevance = "both" if _hits(creator_text, painpoints) else "benchmark_only"

    # 事实覆盖层（取 E 下限）：观察事实 > 打分漂移
    b_vis = " ".join(str(v) for v in stage.get("benchmark_visual_evidence") or [])
    c_vis = " ".join(str(v) for v in stage.get("creator_visual_evidence") or [])
    if stage_id == "S4" and not s4_notes and e > 0 and _DEMO_RE.search(b_vis) and not _DEMO_RE.search(c_vis):
        # 仅在无 s4_evidence 清点（降级路径）时用旧正则覆盖；有清点则新机制已接管
        e, reason = max(e, 2.0), reason + "；S4 标杆动作演示 vs 达人口头宣称（验证=让用户看到）"
    elif stage_id == "S1" and e > 0 and relevance == "benchmark_only":
        e, reason = max(e, 2.0), reason + "；S1 标杆钩子命中品类痛点、达人未命中"

    # 极性红线：达人持平或更优 → small（达人优势记亮点，绝不是差距）
    if e <= 0:
        return {"status": "derived", "severity": "small", "E": 0,
                "reason": reason + "；达人持平或更优（亮点，零差距红线）"}

    w = (weights or {}).get(stage_id, 1.0)
    # 痛点命中系数：差距落在核心决策因素上 → 放大；与痛点无关 → 衰减；事实完全缺失 → 中性。
    # 只作用于卖点链相关阶段（S1 钩子选题 + S2-S5）：S6 促单功能与产品痛点正交
    # （CTA 差距永远不会"命中痛点"，按 0.8 惩罚是范畴错误——round4 kakwan S6 实证），
    # 促单的消费者侧权重已由客单价编入 W（冲动品 1.8）。
    if stage_id == "S6":
        c_factor = 1.0
    elif relevance in {"benchmark_only", "both"}:
        c_factor = 1.2
    elif relevance in {"creator_only", "none"}:
        c_factor = 0.8
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
            "painpoint_relevance": relevance, "S": score, "reason": reason}


CRITICAL_BAND = 0.2


def critical_severity_stages(result: dict[str, Any]) -> list[str]:
    """临界分值触发（Phase C P3）：推导分 S 落在 small/medium 或 medium/large 阈值邻域的阶段。

    S 确定性化后才可行（4d 红利）：边界 case 不靠拟合阈值解决，靠回看原生素材
    复核事实再重推导（实证：tasha S1 连续两轮 S=1.2 恰好压线）。
    须在 derive_severity_from_facts 之后调用（依赖 severity_derivation 溯源）。
    """
    out: list[str] = []
    for stage in result.get("stage_analysis", []):
        if not isinstance(stage, dict):
            continue
        trace = stage.get("severity_derivation") or {}
        score = trace.get("S")
        if trace.get("status") != "derived" or not isinstance(score, (int, float)):
            continue
        if abs(score - TH_SMALL) <= CRITICAL_BAND or abs(score - TH_MEDIUM) <= CRITICAL_BAND:
            match = _STAGE_RE.match(str(stage.get("stage") or ""))
            if match:
                out.append(match.group(1))
    return out


def derive_severity_from_facts(result: dict[str, Any], analysis: dict[str, Any] | None = None) -> None:
    """4d 主入口：用执行分 + 品类权重表确定性推导各阶段 severity，覆盖模型直出值。

    每阶段把算法溯源写入 stage["severity_derivation"]（含被覆盖前的 model_severity）。
    analysis 可选：提供时读取预处理的晃动信号（videos[role].shake）做执行分封顶。
    任何异常优雅降级：跳过该阶段并记录原因，绝不中断主流程。
    """
    stages = result.get("stage_analysis")
    if not isinstance(stages, list):
        return
    shake = None
    if isinstance(analysis, dict):
        levels = {
            side: (((analysis.get("videos") or {}).get(side) or {}).get("shake") or {}).get("level")
            for side in ("creator", "benchmark")
        }
        if any(v == "severe" for v in levels.values()):
            shake = {side: levels[side] == "severe" for side in levels}
    profile = result.get("category_profile") if isinstance(result.get("category_profile"), dict) else None
    _reconcile_operator_tier(profile, analysis)
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
            trace = _derive_one(match.group(1), stage, weights, painpoints, shake)
        except Exception as exc:  # 架构不变量：推导绝不拖垮主流程
            trace = {"status": "error", "reason": f"推导异常已降级：{exc}"}
        if archetype:
            trace.setdefault("archetype", archetype)
        if trace.get("status") == "derived":
            # 优先用归一时定格的模型直判快照；stage["severity"] 此刻已被 stabilize 改写过
            trace["model_severity"] = stage.get("model_severity") or stage.get("severity")
            stage["severity"] = trace["severity"]
        stage["severity_derivation"] = trace
