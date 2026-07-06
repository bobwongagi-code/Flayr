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
from typing import Any, NamedTuple

from .repair import has_hard_endorsement

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


class _Endorsement(NamedTuple):
    """该侧硬背书聚合结果。具名避免 (verbal, visual, available) 位置元组解包错位。"""
    verbal: bool      # 口播/字幕出现硬背书来源词
    visual: bool      # 画面出现独立硬背书视觉证据
    available: bool   # 该侧 unit 有无结构化 endorsement 字段（无→derive 退回硬背书正则兜底）


_NO_ENDORSEMENT = _Endorsement(False, False, False)


def _side_endorsement(result: dict[str, Any], side: str) -> _Endorsement:
    """从 Stage1 facts 聚合该侧硬背书存在性（口播/画面各一）：代码聚合、不让 Stage2 重判（绕过判断层）。
    作用域：该侧【全部 unit】——证书/背书出现在任一 unit 即算，不依赖 functions 阶段标记
    （functions 是模型 descriptive 输出、会误标，挂上去会漏检真背书；背书归不归 S5 由本就只在 S5 闸消费保证）。"""
    vu = result.get("video_understanding")
    side_vu = vu.get(side) if isinstance(vu, dict) else None
    units = side_vu.get("evidence_units") if isinstance(side_vu, dict) else None
    if not isinstance(units, list):
        return _NO_ENDORSEMENT
    units = [u for u in units if isinstance(u, dict)]
    verbal = any(u.get("endorsement_verbal") is True for u in units)
    visual = any(u.get("endorsement_visual") is True for u in units)
    available = any(("endorsement_verbal" in u or "endorsement_visual" in u) for u in units)
    return _Endorsement(verbal, visual, available)


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


def _s1_hook_exec(stage: dict[str, Any]) -> dict[str, Any] | None:
    """S1 Hook flag 化：四维 bool 命中数 → 执行分（met/4×2，落回现有 0-2 尺度）。
    两侧 hook flag 任一缺失 → 返回 None（derive 回退模型执行分，优雅降级）。
    hook_exists 是红线/前置（达人无 Hook、标杆有 Hook → large），不混进四维执行分。"""
    c = stage.get("creator_hook")
    b = stage.get("benchmark_hook")
    if not isinstance(c, dict) or not isinstance(b, dict):
        return None
    if b.get("exists") is True and c.get("exists") is False:
        return {"redline": True}
    c_met = sum(1 for v in (c.get("dims") or {}).values() if v is True)
    b_met = sum(1 for v in (b.get("dims") or {}).values() if v is True)
    c_exec, b_exec = c_met / 4 * 2, b_met / 4 * 2
    # landing 封顶：钩子没打穿（landing_met=false）→ 该侧执行分最高 1.0，结构件齐全也不算"出色"。
    if c.get("landing_met") is False:
        c_exec = min(c_exec, 1.0)
    if b.get("landing_met") is False:
        b_exec = min(b_exec, 1.0)
    return {"redline": False, "creator_exec": c_exec, "bench_exec": b_exec}


def _s1_landing_floor(stage: dict[str, Any]) -> bool:
    """landing 下限：标杆钩子立住、达人没立住 → S1 至少 medium（结构件齐全但钩子没打穿的 case）。
    双方都没立住则不触发（同样没打穿，差距小）。"""
    c = stage.get("creator_hook")
    b = stage.get("benchmark_hook")
    if not isinstance(c, dict) or not isinstance(b, dict):
        return False
    return c.get("landing_met") is False and b.get("landing_met") is True


def _s1_bench_anchors_only(stage: dict[str, Any], relevance: str | None) -> bool:
    """S1 命题锚放大判据：标杆钩子锚定品命题、达人未锚定。
    flag 在 → 读 hook_anchors_proposition（命题不止痛点）；flag 缺 → 回退旧痛点 relevance 口径。"""
    b = stage.get("benchmark_hook")
    c = stage.get("creator_hook")
    if isinstance(b, dict) and isinstance(c, dict):
        return b.get("anchors_proposition") is True and c.get("anchors_proposition") is not True
    return relevance == "benchmark_only"


def _s1_bench_highlight(stage: dict[str, Any]) -> bool:
    """残差亮点门：标杆四维全 met 且 hook_type≠unknown 才开（防模型每次硬写亮点）。
    只决定'是否允许亮点描述'，进 trace 不进 severity。"""
    b = stage.get("benchmark_hook")
    if not isinstance(b, dict):
        return False
    dims = b.get("dims") or {}
    return (b.get("type") not in (None, "", "unknown")
            and len(dims) == 4 and all(v is True for v in dims.values()))


def _s2_contract_exec(stage: dict[str, Any]) -> dict[str, Any] | None:
    """S2 产品引出契约 flag：只校准"自然承接 + 产品身份/角色明确"，不做四维主观分。

    任一侧缺 flag → 返回 None，derive 保留模型执行分。merged_with_s3=true 时不因独立 S2 短/弱扣分。
    """
    c = stage.get("creator_s2")
    b = stage.get("benchmark_s2")
    if not isinstance(c, dict) or not isinstance(b, dict):
        return None

    def side_exec(flag: dict[str, Any]) -> float:
        if flag.get("exists") is False:
            return 0.0
        # S1 缺失会让 S2 没有可承接对象，但如果产品身份和解决方案角色已经清楚，
        # 这个问题应由 S1 承担，避免在 S2 重复计罚。
        if (
            flag.get("merged_with_s3") is True
            and flag.get("product_identity_clear") is True
            and flag.get("product_role_clear") is True
        ):
            return 2.0
        met = sum(
            1
            for key in ("handoff_met", "s1_s2_compatible", "product_identity_clear", "product_role_clear")
            if flag.get(key) is True
        )
        if met >= 4:
            return 2.0
        if met >= 3:
            return 1.0
        if met >= 1:
            return 0.5
        return 0.0

    return {"creator_exec": side_exec(c), "bench_exec": side_exec(b)}


def _s2_contract_floor(stage: dict[str, Any]) -> tuple[bool, str]:
    """S2 下限：标杆完成承接/身份/角色，达人没完成关键契约 → 至少 medium。"""
    c = stage.get("creator_s2")
    b = stage.get("benchmark_s2")
    if not isinstance(c, dict) or not isinstance(b, dict):
        return False, ""
    if c.get("merged_with_s3") is True:
        return False, ""
    benchmark_complete = (
        b.get("handoff_met") is True
        and b.get("product_identity_clear") is True
        and b.get("product_role_clear") is True
    )
    if not benchmark_complete:
        return False, ""
    missing = []
    if c.get("handoff_met") is False:
        missing.append("未自然承接 S1")
    if c.get("product_identity_clear") is False:
        missing.append("产品身份不清")
    if c.get("product_role_clear") is False:
        missing.append("产品未成为解决方案/答案")
    if c.get("s1_s2_compatible") is False:
        missing.append("S1→S2 模块不兼容")
    if not missing:
        return False, ""
    return True, "；S2 契约下限：" + "、".join(missing)


def _s2_risky_module(stage: dict[str, Any]) -> bool:
    """S2 风险模块：达人使用结构库排除/高风险引出方式而标杆没有，需放大到 medium 起。"""
    c = stage.get("creator_s2")
    b = stage.get("benchmark_s2")
    if not isinstance(c, dict) or not isinstance(b, dict):
        return False
    return c.get("excluded_or_risky_module") is True and b.get("excluded_or_risky_module") is not True


def _derive_one(stage_id: str, stage: dict[str, Any], weights: dict[str, float] | None,
                painpoints: list[str], shake: dict[str, bool] | None = None,
                endorsement: dict[str, tuple[bool, bool, bool]] | None = None) -> dict[str, Any]:
    """推导单阶段 severity。返回 severity_derivation 溯源 dict（status=derived 时含新 severity）。"""
    creator_exec = stage.get("creator_execution")
    bench_exec = stage.get("benchmark_execution")
    # S1 Hook flag 化：四维 bool 在时由 flag 推执行分，替代模型 0-2 主观分；flag 缺则回退模型分（优雅降级）。
    # severity 仍走下方 e 差值/阈值/放大器/红线，不把 S1 变成孤立打分系统。
    if stage_id == "S1":
        s1 = _s1_hook_exec(stage)
        if s1 is not None:
            if s1.get("redline"):
                return {"status": "derived", "severity": "large", "E": 2,
                        "reason": "S1 达人无 Hook、标杆有 Hook（hook_exists 红线）"}
            creator_exec, bench_exec = s1["creator_exec"], s1["bench_exec"]
    elif stage_id == "S2":
        s2 = _s2_contract_exec(stage)
        if s2 is not None:
            creator_exec, bench_exec = s2["creator_exec"], s2["bench_exec"]
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
    if stage_id == "S5":
        b = (endorsement or {}).get("benchmark") or _NO_ENDORSEMENT
        c = (endorsement or {}).get("creator") or _NO_ENDORSEMENT
        if b.available or c.available:  # 有结构化 flag → 读 flag（绕过 Stage2 判断 + 脆弱正则）
            b_has, c_has = (b.verbal or b.visual), (c.verbal or c.visual)
            src = "结构化 flag"
        else:  # 老 facts 无 flag → 硬背书正则兜底（软背书不算，与 flag 口径一致：双方均无硬背书→small）
            b_has, c_has = has_hard_endorsement(bench_text), has_hard_endorsement(creator_text)
            src = "硬背书正则兜底"
        if not b_has and not c_has:
            return {"status": "derived", "severity": "small", "E": 0,
                    "reason": f"S5 双方均无硬背书 → 均未涉及（{src}）"}

    e = max(0.0, float(bench_exec) - float(creator_exec))
    reason = f"E = 标杆执行分 {bench_exec} − 达人执行分 {creator_exec}"
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
    # S4 效果呈现放大：优先读模型基于结构库 S4-A~F 判出的结构化布尔（稳，不随措辞抖）；
    # 布尔缺失（存量结果无此字段）才回退扫 _DEMO_RE 关键词（脆，仅兜底，见 TODO §ROOT/§0）
    b_demo = stage.get("benchmark_has_effect_demo")
    c_demo = stage.get("creator_has_effect_demo")
    if b_demo is None and c_demo is None:
        b_demo, c_demo = bool(_DEMO_RE.search(b_vis)), bool(_DEMO_RE.search(c_vis))
    # S3 使用过程放大：模型基于结构库 S3-A~E 判出的布尔。无正则兜底——布尔缺失（存量结果）
    # 则不触发，保留 S3 旧空白行为（derive.py 此前无任何 S3 专属逻辑）。用严格 is True/is False，
    # 仅在明确"标杆演示了使用、达人没演"时放大，不确定（None）不触发，保守。
    b_usage = stage.get("benchmark_has_usage_demo")
    c_usage = stage.get("creator_has_usage_demo")
    if stage_id == "S4" and e > 0 and b_demo is True and c_demo is False:
        e, reason = max(e, 2.0), reason + "；S4 标杆呈现了效果(S4-A~F)、达人未呈现（验证=让用户看到）"
    elif stage_id == "S3" and e > 0 and b_usage is True and c_usage is False:
        e, reason = max(e, 2.0), reason + "；S3 标杆把卖点演示出来(S3-A~E)、达人只口播未演示（演示即证据）"
    elif stage_id == "S1" and e > 0 and _s1_bench_anchors_only(stage, relevance):
        e, reason = max(e, 2.0), reason + "；S1 标杆钩子锚定品命题、达人未锚定（命题不止痛点）"
    elif stage_id == "S2" and e > 0 and _s2_risky_module(stage):
        e, reason = max(e, 1.0), reason + "；S2 达人使用结构库排除/高风险引出方式"

    # 极性红线：达人持平或更优 → small（达人优势记亮点，绝不是差距）
    if e <= 0:
        if stage_id == "S1" and _s1_bench_anchors_only(stage, relevance):
            return {"status": "derived", "severity": "medium", "E": 0,
                    "reason": reason + "；命题锚下限：标杆钩子锚定本品核心命题、达人只做泛留人"}
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
    # landing 下限：标杆钩子立住、达人未立住 → 至少 medium（件齐但钩子没打穿，不该判 small）
    if stage_id == "S1" and severity == "small" and _s1_landing_floor(stage):
        severity = "medium"
        reason += "；landing 下限：标杆钩子立住、达人未立住（结构件齐全但钩子没打穿）"
    if stage_id == "S1" and severity == "small" and _s1_bench_anchors_only(stage, relevance):
        severity = "medium"
        reason += "；命题锚下限：标杆钩子锚定本品核心命题、达人只做泛留人"
    if stage_id == "S2" and severity == "small":
        floor, floor_reason = _s2_contract_floor(stage)
        if floor:
            severity = "medium"
            reason += floor_reason
    trace = {"status": "derived", "severity": severity, "E": e, "W": w, "C": c_factor,
             "painpoint_relevance": relevance, "S": score, "reason": reason}
    # 残差亮点门（只进 trace 不进 severity）：标杆四维全 met 且类型明确才允许亮点描述，否则跳过
    if stage_id == "S1" and _s1_bench_highlight(stage):
        trace["hook_highlight_allowed"] = True
    return trace


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
    # S5 硬背书：从 Stage1 facts 代码聚合每侧 ①②，绕过 Stage2 判断。仅 S5 闸消费——无 S5 阶段则不算
    endorsement = ({side: _side_endorsement(result, side) for side in ("creator", "benchmark")}
                   if any("S5" in str(s.get("stage") or "") for s in stages if isinstance(s, dict)) else {})

    for stage in stages:
        if not isinstance(stage, dict):
            continue
        match = _STAGE_RE.match(str(stage.get("stage") or ""))
        if not match:
            continue
        try:
            trace = _derive_one(match.group(1), stage, weights, painpoints, shake, endorsement)
        except Exception as exc:  # 架构不变量：推导绝不拖垮主流程
            trace = {"status": "error", "reason": f"推导异常已降级：{exc}"}
        if archetype:
            trace.setdefault("archetype", archetype)
        if trace.get("status") == "derived":
            # 优先用归一时定格的模型直判快照；stage["severity"] 此刻已被 stabilize 改写过
            trace["model_severity"] = stage.get("model_severity") or stage.get("severity")
            stage["severity"] = trace["severity"]
        stage["severity_derivation"] = trace
