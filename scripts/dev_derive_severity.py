#!/usr/bin/env python3
"""离线校验：severity 由"稳定事实 + 品类权重表"确定性推导，能否复现 18 个人工标签。

背景（2026-06-11）：两轮门禁证明 prompt 调 severity 不收敛（11/18→9/18，对跳 4→6），
而事实层字段（task_completion 等）稳定。本脚本验证 TODO 4d / 用户 V2.1 设计的可行性上限：
  模型只供事实 → 代码推导 E（客观表达差距）→ S = E × L_link × L_product × L_consumer → severity。

三条定稿原则（2026-06-11 用户裁决）：
  ① E 不让模型输出，由代码从稳定事实推导（方向 + task_completion + 呈现支撑）。
  ③ 品类痛点清单写在权重表（数据），模型只报"主打了什么卖点"（事实），命中与否代码查表。
  ④ 事实不支撑则不判断：双方都没有该功能事实（如 S5 双方均无真背书）→"均未涉及"，不进公式。

零 LLM 成本：只读两轮门禁存量 raw（runs/sample-*/dev_stage2_result_raw_NN.json 及 gate_round1/），
与 dev_score_gate.LABELS 比对。L 系数为 V2.1 初值（用户标注非定稿），输出全量算法溯源供拟合。
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from dev_score_gate import LABELS, STAGES, load_raw, stage_map, mode_and_freq
from flayr_core.llm.parse import normalize_task_completion
from flayr_core.postprocess.repair import has_real_endorsement

ROOT = Path(__file__).resolve().parents[1]

# ── 品类权重表（V2.1 初值，待拟合）─────────────────────────────────────────────
# W：品类×阶段权重（V2.1 的 L_link×L_product 合并为单表——"客单价/决策门槛决定各阶段权重"
#   是按阶段分布的，标量系数表达不了：低客单冲动品 CTA 权重高、背书权重低）。
# painpoints：该品类消费者高优先级决策因素的关键词（含本地语）；low_priority：低优先级卖点词。
CATEGORY: dict[str, dict] = {
    "sample-are_xie": {
        "name": "女性生理期保健品（口服，高决策门槛）",
        # 高决策口服：信任背书与效果验证是说服核心
        "W": {"S1": 1.5, "S2": 1.2, "S3": 1.0, "S4": 1.4, "S5": 1.6, "S6": 1.2},
        "painpoints": ["生理期", "经期", "痛", "健康", "功效", "认证", "安全", "信任", "困扰"],
        "low_priority": ["省钱", "划算", "便宜", "性价比", "jimat", "murah"],
    },
    "sample-kakwanreview": {
        "name": "一次性马桶刷（低客单价冲动品）",
        # 低客单冲动：CTA 是转化口（客单越低 CTA 权重越高），背书必要性低
        "W": {"S1": 1.5, "S2": 1.2, "S3": 1.2, "S4": 1.0, "S5": 0.6, "S6": 1.8},
        "painpoints": ["脏", "卫生", "掉毛", "干净", "冲净", "厌恶", "细菌"],
        "low_priority": [],
    },
    "sample-tashadiyana": {
        "name": "儿童泡沫牙膏（高决策门槛，决策人分离）",
        # 高决策+感官驱动：效果验证（感官可视化）权重最高
        "W": {"S1": 1.5, "S2": 1.0, "S3": 1.0, "S4": 1.5, "S5": 1.4, "S6": 1.2},
        "painpoints": ["防蛀", "健康", "安全", "成分", "含氟", "无糖", "香", "味", "wangi", "氛围"],
        "low_priority": ["节省", "省", "jimat"],
    },
}
# severity 映射阈值（V2.1）：S≤1.2 small；S≤2.5 medium；S>2.5 large
TH_SMALL, TH_MEDIUM = 1.2, 2.5

import re

# 方向词表：从两轮门禁实测的 task_completion 自由文本归纳
_BOTH_MISSING_RE = re.compile(r"both[_\s]*missing|均未(完成|涉及|设计)|双方均未|neither", re.IGNORECASE)
_CREATOR_ADV_RE = re.compile(
    r"creator[_\s]*(only|superior|better|stronger)|达人(更优|优于|完胜|领先)|benchmark[_\s]*(missing|absent|none)|仅达人|no[_\s]*gap",
    re.IGNORECASE,
)
_CREATOR_MISSING_RE = re.compile(r"creator[_\s]*(missing|absent)|benchmark[_\s]*only|仅标杆", re.IGNORECASE)
_SUPPORT_RANK = {"supported": 2, "visual_only": 1}
# 极性护栏：gap 文本明说达人占优 → E=0（两轮门禁实锤：模型把"达人显著优于标杆"判 large）
_ADV_GAP_RE = re.compile(r"达人[^，。]{0,8}(优于|强于|领先|更优|完胜|超过)|不弱于标杆|达人(明显|显著)更", re.IGNORECASE)
# 背书层级：权威认证（政府/检测/临床）> 市场准入基线（清真等）——层级差按缺失处理（are_xie 判例）
_TIER_STRONG_RE = re.compile(r"KKM|FDA|卫生部|药监|SGS|SIRIM|TISI|实验室|临床|检测报告|政府", re.IGNORECASE)
# S4 动作演示词：效果验证的功能定义是"让用户看到并信服"，标杆动作演示 vs 达人口头宣称 = 验证功能未达成
_DEMO_RE = re.compile(r"闻|嗅|按压|挤出|涂抹|擦拭|冲水|冲洗|冲净|脱落|掉入|掉进|排空|实测|对比|试用|测试|前后")
# 弱 CTA 敷衍档线索（kakwan S6 判例：母语者三遍重听才确认的一句轻带——形式上有但几乎无效）
_WEAK_CTA_RE = re.compile(r"(没有|缺乏|缺少|不够|无)明确|仅.{0,6}(提及|带过)|简短|敷衍|轻描淡写|一句带过|顺带")


def _stage_texts(stage: dict, side: str) -> str:
    """拼接一侧的全部文本事实（摘要/关键信息/引述）。"""
    keys = [f"{side}_summary", f"{side}_key_message", f"{side}_quote_zh", f"{side}_quote"]
    return " ".join(str(stage.get(k) or "") for k in keys)


def _hits(text: str, words: list[str]) -> bool:
    return any(w.lower() in text.lower() for w in words)


def derive(stage_id: str, stage: dict, cat: dict) -> dict:
    """从单阶段原始事实推导 severity，返回含算法溯源的 dict。"""
    task_raw = str(stage.get("task_completion") or "")
    fit_raw = str(stage.get("module_fit") or "")
    direction_src = f"{task_raw} {fit_raw}"
    bench_text = _stage_texts(stage, "benchmark")
    creator_text = _stage_texts(stage, "creator")
    trace: dict = {"task_raw": task_raw[:40]}

    # 原则④：S5 事实门槛——双方都无真背书 → 均未涉及，不进公式（卖点类信息归卖点链，不算 S5 差距）
    if stage_id == "S5":
        bench_endorsed = has_real_endorsement(bench_text)
        creator_endorsed = has_real_endorsement(creator_text)
        trace["endorsed"] = f"b={bench_endorsed} c={creator_endorsed}"
        if not bench_endorsed and not creator_endorsed:
            return {**trace, "E": None, "severity": "na", "reason": "双方均无真背书→均未涉及"}
        if bench_endorsed and not creator_endorsed:
            direction = "creator_missing"  # 标杆有真背书达人没有：事实压过模型的方向文本
        elif creator_endorsed and not bench_endorsed:
            direction = "creator_advantage"
        elif _TIER_STRONG_RE.search(bench_text) and not _TIER_STRONG_RE.search(creator_text):
            direction = "creator_missing"  # 背书层级差：标杆权威认证 vs 达人仅基线合规（如清真）
        else:
            direction = _derive_direction(direction_src)
    else:
        direction = _derive_direction(direction_src)
    # 极性护栏：方向文本含糊但 gap 明说达人占优 → 达人≥标杆
    gap_text = f"{stage.get('gap_summary') or ''} {stage.get('gap') or ''}"
    if direction == "both_present" and _ADV_GAP_RE.search(gap_text):
        direction = "creator_advantage"
    trace["direction"] = direction

    # 事实覆盖层：可观察事实 > 模型方向词（tasha S4 实测：方向词五次两极漂移
    # creator_superior↔benchmark_better，而画面事实五次一致——推导必须锚定观察事实）
    b_vis = " ".join(str(v) for v in stage.get("benchmark_visual_evidence") or [])
    c_vis = " ".join(str(v) for v in stage.get("creator_visual_evidence") or [])
    forced: tuple[int, str] | None = None
    if direction != "both_missing":
        if stage_id == "S4" and _DEMO_RE.search(b_vis) and not _DEMO_RE.search(c_vis):
            # S4 功能定义是"让用户看到并信服"：标杆动作演示 vs 达人口头宣称 = 验证功能未达成
            forced = (2, "标杆动作演示 vs 达人口头宣称（验证=让用户看到）")
        elif stage_id == "S1" and _hits(bench_text, cat["painpoints"]) and not _hits(creator_text, cat["painpoints"]):
            # S1 钩子的功能 = 用品类痛点完成情绪抢夺：标杆命中、达人未命中 = 功能缺失
            forced = (2, "标杆钩子命中品类痛点，达人未命中")

    # 原则①：E 由代码推导
    if forced:
        e, reason = forced
    elif direction == "both_missing":
        return {**trace, "E": None, "severity": "na", "reason": "双方均未涉及"}
    elif direction == "creator_advantage":
        return {**trace, "E": 0, "severity": "highlight", "reason": "达人≥标杆（E=0 零差距红线）"}
    elif direction == "creator_missing":
        e, reason = 2, "达人完全未表达"
    else:  # both_present：按达人侧完成度 + 呈现支撑 + 卖点选择偏差细分
        completion = normalize_task_completion(task_raw)
        if completion == "missing":
            e, reason = 2, "达人侧 missing"
        elif completion == "partial":
            # 敷衍档 E=1.5（用户 2026-06-11 裁决：形式上有但敷衍/几乎无效，介于"做了"与"缺失"之间）
            if stage_id == "S6" and _WEAK_CTA_RE.search(f"{gap_text} {creator_text}"):
                e, reason = 1.5, "弱 CTA 敷衍档（形式上有但几乎无效）"
            else:
                e, reason = 1, "达人侧 partial"
        else:
            c_rank = _SUPPORT_RANK.get(str(stage.get("creator_support_status") or ""), 0)
            b_rank = _SUPPORT_RANK.get(str(stage.get("benchmark_support_status") or ""), 0)
            if c_rank < b_rank:
                e, reason = 1, "呈现支撑弱于标杆"
            elif (
                _hits(bench_text, cat["painpoints"])
                and not _hits(creator_text, cat["painpoints"])
                and _hits(creator_text, cat["low_priority"])
            ):
                # 原则③：卖点选择偏差查表判定——标杆命中高优卖点、达人只讲低优卖点
                e, reason = 1, "卖点选择偏差（查表）"
            elif str(stage.get("severity") or "").lower() in {"medium", "large"}:
                # 过渡代理：双方均完成时 E=0/1 边界需要"谁更好"的方向事实，现有 raw 缺该字段，
                # 暂用模型自报 severity 二值化（≥medium 即"模型看到了达人侧差距"）。
                # 管线落地后由结构化 gap_direction（parity|creator_deficit|creator_advantage）替换。
                e, reason = 1, "方向代理：模型自报有差距（待 gap_direction 字段）"
            else:
                return {**trace, "E": 0, "severity": "highlight", "reason": "双方完成且无可推导差距"}

    # 商业杠杆：品类×阶段权重 × 消费者痛点命中（V2.1 初值，待拟合）
    w = cat["W"][stage_id]
    lever_text = f"{gap_text} {bench_text}"
    l_consumer = 1.2 if _hits(lever_text, cat["painpoints"]) else 0.8
    score = e * w * l_consumer

    # 硬性红线：S1/S6 核心功能缺失（E=2）强制 large
    if e == 2 and stage_id in {"S1", "S6"}:
        severity = "large"
        reason += "；S1/S6 缺失红线"
    elif score > TH_MEDIUM:
        severity = "large"
    elif score > TH_SMALL:
        severity = "medium"
    else:
        severity = "small"
    return {**trace, "E": e, "L": f"W{w}×C{l_consumer}", "S": round(score, 2),
            "severity": severity, "reason": reason}


def _derive_direction(text: str) -> str:
    if _BOTH_MISSING_RE.search(text):
        return "both_missing"
    if _CREATOR_ADV_RE.search(text):
        return "creator_advantage"
    if _CREATOR_MISSING_RE.search(text):
        return "creator_missing"
    if normalize_task_completion(text) == "missing":
        return "creator_missing"
    return "both_present"


# 与人工标签比对口径：highlight（达人≥标杆）与 na（均未涉及）按 small 计
_COMPARE_MAP = {"highlight": "small", "na": "small"}
_ABBR = {"large": "l", "medium": "m", "small": "s", "highlight": "h", "na": "n", "missing": "?"}


def run_round(runs_dir: Path, subdir: str, repeat: int, verbose: bool) -> tuple[int, int, int]:
    """跑一轮：返回 (一致数, 总阶段数, large↔small 对跳数)。"""
    agree = total = flips = 0
    for name, labels in LABELS.items():
        run = runs_dir / name / subdir if subdir else runs_dir / name
        cat = CATEGORY[name]
        print(f"\n  {name}（{cat['name']}）")
        for sid in STAGES:
            derived: list[str] = []
            traces: list[dict] = []
            for i in range(1, repeat + 1):
                result = load_raw(run, i)
                if not result:
                    continue
                stage = stage_map(result).get(sid)
                if not stage:
                    derived.append("missing")
                    continue
                d = derive(sid, stage, cat)
                derived.append(d["severity"])
                traces.append(d)
            mode, freq = mode_and_freq(derived)
            comparable = _COMPARE_MAP.get(mode, mode)
            label = labels[sid]
            hit = comparable == label
            agree += hit
            total += 1
            effective = {_COMPARE_MAP.get(v, v) for v in derived}
            flip = "large" in effective and "small" in effective
            flips += flip
            seq = ",".join(_ABBR.get(v, "?") for v in derived)
            mark = "✓" if hit else ("✗✗" if {comparable, label} == {"large", "small"} else "✗")
            print(f"    {sid}: [{seq}] 众数={mode}({freq}/{len(derived)}) 标签={label} {mark}{' ⚠对跳' if flip else ''}")
            if verbose or not hit:
                t = traces[0] if traces else {}
                print(f"        溯源: dir={t.get('direction')} E={t.get('E')} L={t.get('L', '-')} "
                      f"S={t.get('S', '-')} | {t.get('reason')} | task='{t.get('task_raw')}'"
                      + (f" | {t.get('endorsed')}" if t.get("endorsed") else ""))
    return agree, total, flips


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--runs-dir", default=str(ROOT / "runs"))
    parser.add_argument("--verbose", action="store_true", help="打印全部阶段溯源（默认只打不一致的）")
    args = parser.parse_args()
    runs_dir = Path(args.runs_dir)

    summary = []
    for round_name, subdir in [("round1", "gate_round1"), ("round2", "")]:
        print(f"\n═══ {round_name}（{'gate_round1/' if subdir else '样本根目录'}，确定性推导）═══")
        agree, total, flips = run_round(runs_dir, subdir, args.repeat, args.verbose)
        summary.append((round_name, agree, total, flips))

    print("\n═══ 汇总 ═══")
    print("  对照基线：raw severity（模型直接判）一致率 round1=11/18、round2=9/18；对跳 round1=4、round2=6")
    for round_name, agree, total, flips in summary:
        print(f"  {round_name} 确定性推导: 一致率 {agree}/{total}，large↔small 对跳 {flips}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
